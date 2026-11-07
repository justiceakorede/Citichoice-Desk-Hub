"""Rate cards, FX and pricing snapshots. The WiseCP provider tests use a MOCK WiseCP built from the documented response
shape; nothing here has run against the live WiseCP."""
import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db as database
from app.config import get_settings
from app.models import AuditLog, FxRate, PricingSnapshot, RateCard, UsageRecord, now
from app.services import fx, rates
from .conftest import ADMIN, INTERNAL, H, bind_key, credit, fx_row, mk_tenant

D = dt.date


def add_card(client, **kw):
    body = {"category": "utility", "market": "NG", "currency": "USD", "rate": "0.0045", "effective_from": "2026-01-01",
            "source": "https://example/meta-rate-card", **kw}
    return client.post("/admin/rate-cards", headers=H(ADMIN), json=body)


def use(client, key, ext, country="NG", cat="utility", **kw):
    return client.post("/internal/usage", headers=H(key), json={"external_id": ext, "category": cat, "country": country, **kw})


# ------------------------------------------------------------------------------------------------ rate card rules
def test_market_specific_rate_beats_the_default_and_the_default_covers_the_rest(client, cw, tenant):
    credit(client, tenant, "10000", "t-1")
    assert add_card(client, rate="0.0045", effective_from="2026-01-01").status_code == 200       # NG utility
    ng = use(client, INTERNAL, "ng1", "NG").json()
    other = use(client, INTERNAL, "gh1", "GH").json()                                             # falls back to the '*' card (0.0067)
    assert ng["provider_cost_micro"] == 4_500 * 1600 and other["provider_cost_micro"] == 6_700 * 1600


def test_a_new_version_closes_the_old_one_and_old_usage_keeps_its_price(client, cw, tenant):
    credit(client, tenant, "10000", "t-2")
    add_card(client, rate="0.0045", effective_from="2026-01-01")
    before = use(client, INTERNAL, "v1", "NG", occurred_at=now().isoformat()).json()
    r = add_card(client, rate="0.0050", effective_from=(now().date() + dt.timedelta(days=1)).isoformat())
    assert r.status_code == 200 and r.json()["version"] == 2
    cards = client.get("/admin/rate-cards?market=NG", headers=H(ADMIN)).json()
    assert cards[0]["effective_to"] == cards[1]["effective_from"] and cards[1]["effective_to"] is None
    today = use(client, INTERNAL, "v2", "NG").json()
    assert today["provider_cost_micro"] == before["provider_cost_micro"]                        # today still on version 1
    tomorrow = use(client, INTERNAL, "v3", "NG", occurred_at=(now() + dt.timedelta(days=1)).isoformat()).json()
    assert tomorrow["provider_cost_micro"] == 5_000 * 1600


def test_overlaps_and_backdating_are_refused(client):
    assert add_card(client, effective_from="2026-03-01").status_code == 200
    for start in ("2026-03-01", "2026-02-01", "2025-01-01"):
        r = add_card(client, effective_from=start, rate="0.0099")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "overlap", start
    assert add_card(client, effective_from="2026-04-01", effective_to="2026-05-01").status_code == 409      # would cut a hole in the open window


def test_a_closed_window_means_no_rate_after_it_and_no_silent_zero(client, cw, tenant):
    credit(client, tenant, "100", "t-3")
    # replace the seeded '*' utility card with one that has already ended
    with database.SessionLocal() as s:
        s.execute(text("update rate_cards set effective_to = '2025-01-01' where category = 'utility'"))
        s.commit()
    r = use(client, INTERNAL, "x1", "NG")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_rate_card"
    with database.SessionLocal() as s:
        assert s.scalars(select(UsageRecord)).all() == []


def test_future_dated_and_withdrawn_cards_are_not_used(client, cw, tenant):
    credit(client, tenant, "100", "t-4")
    future = (now().date() + dt.timedelta(days=30)).isoformat()
    cid = add_card(client, market="GH", rate="0.0100", effective_from=future).json()["id"]
    assert use(client, INTERNAL, "f1", "GH").json()["provider_cost_micro"] == 6_700 * 1600     # default card, not the future one
    cid2 = add_card(client, market="KE", rate="0.0100", effective_from="2026-01-01").json()["id"]
    client.post(f"/admin/rate-cards/{cid2}/deactivate", headers=H(ADMIN))
    assert use(client, INTERNAL, "f2", "KE").json()["provider_cost_micro"] == 6_700 * 1600


@pytest.mark.parametrize("kw,code", [({"rate": "0.0000001"}, "bad_amount"), ({"currency": "ZZZ"}, "unknown_currency"),
                                     ({"category": "bulk"}, None), ({"market": "NGA"}, None),
                                     ({"effective_to": "2025-12-31"}, "bad_window")])
def test_bad_rate_cards_refused(client, kw, code):
    r = add_card(client, **kw)
    assert r.status_code == 422
    if code:
        assert r.json()["detail"]["code"] == code


def test_rate_amounts_are_exact_micro_units(client):
    assert add_card(client, rate="0.004567").json()["rate_micro"] == 4_567


def test_rate_card_changes_are_audited(client):
    add_card(client)
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "rate.add")).one()
    assert row.detail["rate_micro"] == 4_500 and row.detail["version"] == 1


# ------------------------------------------------------------------------------------------------ FX selection
def test_fx_uses_the_rate_in_force_at_the_message_time(client, cw, tenant):
    credit(client, tenant, "10000", "t-5")
    with database.SessionLocal() as s:                                     # a second, newer observation 30 minutes ago
        s.add(fx_row("NGN", 1700, hours_ago=0.5, source="test2"))
        s.commit()
    old = use(client, INTERNAL, "o1", occurred_at=(now() - dt.timedelta(hours=0.75)).isoformat()).json()
    new = use(client, INTERNAL, "o2").json()
    assert old["provider_cost_micro"] == 6_700 * 1600 and new["provider_cost_micro"] == 6_700 * 1700


def test_stale_fx_is_refused_not_used(client, cw, tenant):
    credit(client, tenant, "100", "t-6")
    r = use(client, INTERNAL, "s1", occurred_at=(now() + dt.timedelta(hours=60)).isoformat())      # newest rate is >48h older than the event
    assert r.status_code == 422 and r.json()["detail"]["code"] == "fx_stale"


def test_missing_fx_pair_refused(client, cw):
    tid = mk_tenant(client, cw, service="svc-kwd", name="K", currency="KWD")
    bind_key("k", [tid])
    credit(client, tid, "10", "t-kwd", currency="KWD")
    assert use(client, "k", "m1").json()["detail"]["code"] == "no_fx_rate"


def test_inverse_and_cross_rates(client, cw):
    with database.SessionLocal() as s:
        s.add_all([fx_row("USD", 0.5, base="EUR"), fx_row("EUR", 4, base="KWD")])     # 1 EUR = 0.5 USD ; 1 KWD = 4 EUR
        s.commit()
        d = lambda a, b: fx.rate_between(s, a, b, now())
        rate, rows = d("USD", "EUR")
        assert str(rate) == "2.0000000000" and len(rows) == 1                          # inverse of EUR->USD
        with pytest.raises(fx.FxError):                                                # KWD -> EUR -> USD -> NGN needs two pivots: refused
            d("KWD", "NGN")
        rate, rows = d("EUR", "NGN")                                                   # EUR -> USD (inverse of USD/EUR? no: 1 EUR=0.5 USD) -> NGN via USD
        assert str(rate) == "800.0000000000" and len(rows) == 2


def test_big_jumps_are_quarantined_until_staff_approve(client):
    now_ = now()
    with database.SessionLocal() as s:
        res = fx.record_quotes(s, [fx.Quote("USD", "NGN", __import__("decimal").Decimal("2200"), "wisecp:currencies", now_)], "t")
        s.commit()
    assert res["quarantined"] == 1
    rows = client.get("/admin/fx?quote=NGN&status=quarantined", headers=H(ADMIN)).json()
    assert len(rows) == 1
    with database.SessionLocal() as s:
        rate, used = fx.rate_between(s, "USD", "NGN", now() + dt.timedelta(minutes=1))
        assert str(rate).startswith("1600")                                           # the quarantined 1900 is NOT in use
    ok = client.post(f"/admin/fx/{rows[0]['id']}/approve", headers=H(ADMIN))
    assert ok.status_code == 200 and ok.json()["approves_id"] == rows[0]["id"]
    assert client.post(f"/admin/fx/{rows[0]['id']}/approve", headers=H(ADMIN)).status_code == 409      # only once
    with database.SessionLocal() as s:
        rate, _ = fx.rate_between(s, "USD", "NGN", now() + dt.timedelta(minutes=1))
        assert str(rate).startswith("2200")


def test_unchanged_rates_are_not_stored_again(client):
    with database.SessionLocal() as s:
        q = fx.Quote("USD", "NGN", __import__("decimal").Decimal("1600"), "test", now())
        assert fx.record_quotes(s, [q], "t")["unchanged"] == 1


# ------------------------------------------------------------------------------------------------ snapshots
def snap_of(rec_id):
    with database.SessionLocal() as s:
        r = s.get(UsageRecord, rec_id)
        return r, s.get(PricingSnapshot, r.pricing_snapshot_id)


def test_every_billed_message_points_at_a_reproducible_snapshot(client, cw, tenant):
    from app.services import pricing
    from decimal import Decimal
    credit(client, tenant, "10000", "t-7")
    use(client, INTERNAL, "r1")
    with database.SessionLocal() as s:
        rec = s.scalars(select(UsageRecord)).one()
        snap = s.get(PricingSnapshot, rec.pricing_snapshot_id)
        assert snap.rate_source == "rate_card" and snap.rate_card_id and snap.fx_rate_ids and snap.fx_source == "test"
        assert (snap.unit_cost_micro, snap.buffer_micro, snap.unit_charge_micro) == (rec.provider_cost_micro, rec.fx_buffer_micro, rec.charge_micro)
        # recompute from the frozen inputs alone: same answer
        assert pricing.compute(snap.rate_micro, Decimal(snap.fx_rate), Decimal(snap.fx_buffer_pct), Decimal(snap.markup)) == \
            (snap.unit_cost_micro, snap.buffer_micro, snap.fee_micro, snap.unit_charge_micro)
        assert rec.provider_cost_micro + rec.fx_buffer_micro + rec.service_fee_micro == rec.charge_micro


def test_identically_priced_messages_share_one_snapshot(client, cw, tenant):
    credit(client, tenant, "10000", "t-8")
    for i in range(5):
        use(client, INTERNAL, f"sh{i}")
    with database.SessionLocal() as s:
        assert len(s.scalars(select(PricingSnapshot)).all()) == 1 and len(s.scalars(select(UsageRecord)).all()) == 5


def test_later_fx_and_rate_changes_never_alter_finalised_usage(client, cw, tenant):
    credit(client, tenant, "10000", "t-9")
    first = use(client, INTERNAL, "h1").json()
    with database.SessionLocal() as s:
        before = [(r.provider_cost_micro, r.fx_buffer_micro, r.service_fee_micro, r.charge_micro, r.pricing_snapshot_id) for r in s.scalars(select(UsageRecord))]
        s.add(fx_row("NGN", 1650, hours_ago=0.1, source="daily-refresh"))            # tomorrow's rate refresh
        s.commit()
    add_card(client, market="*", rate="0.0100", effective_from=(now().date() + dt.timedelta(days=1)).isoformat())
    client.patch(f"/admin/tenants/{tenant}", headers=H(ADMIN), json={"markup": "3.0"})
    with database.SessionLocal() as s:
        after = [(r.provider_cost_micro, r.fx_buffer_micro, r.service_fee_micro, r.charge_micro, r.pricing_snapshot_id) for r in s.scalars(select(UsageRecord))]
    assert before == after
    second = use(client, INTERNAL, "h2").json()
    assert second["charge_micro"] != first["charge_micro"] and second["pricing_snapshot_id"] != first["pricing_snapshot_id"]


def test_snapshots_fx_and_rate_amounts_cannot_be_changed_or_deleted(client, cw, tenant):
    credit(client, tenant, "100", "t-10")
    use(client, INTERNAL, "i1")
    attempts = ["update pricing_snapshots set unit_charge_micro = 1", "delete from pricing_snapshots",
                "update fx_rates set rate = 1", "delete from fx_rates",
                "update rate_cards set rate_micro = 1", "update rate_cards set effective_from = '2000-01-01'", "delete from rate_cards"]
    for sql in attempts:
        with pytest.raises((IntegrityError, DBAPIError)), database.engine.begin() as c:
            c.execute(text(sql))
    with database.engine.begin() as c:                                                  # the only allowed edits on a rate card
        c.execute(text("update rate_cards set active = active"))


def test_zero_charge_for_free_messages_and_no_snapshot_needed(client, cw, tenant):
    r = use(client, INTERNAL, "free1", cat="service").json()
    assert r["recorded"] and r["charge_micro"] == 0 and r["pricing_snapshot_id"] is None


def test_a_caller_cannot_supply_its_own_price(client, cw, tenant):
    """n8n reports what happened, never what it costs. A supplied price is refused, so nobody can undercharge."""
    credit(client, tenant, "100", "t-11")
    for extra in ({"provider_cost": "0.000001"}, {"charge_micro": 1}, {"meta_cost_usd": "0"}, {"markup": "0.1"}):
        r = use(client, INTERNAL, "ev1", **extra)
        assert r.status_code == 422, extra
    with database.SessionLocal() as s:
        assert s.scalars(select(UsageRecord)).all() == []


def test_pricing_preview_changes_nothing(client, cw, tenant):
    r = client.get(f"/admin/pricing/preview?tenant_id={tenant}&category=utility&country=NG", headers=H(ADMIN)).json()
    assert r["unit_charge_micro"] == 15_458_240 and r["rate_source"] == "rate_card"
    with database.SessionLocal() as s:
        assert s.scalars(select(PricingSnapshot)).all() == []


# ------------------------------------------------------------------------------------------------ WiseCP adapter (MOCK)
def wisecp(currencies, settings, status=200):
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer k-test"
        body = {"/api/v1/admin/financial/currencies": currencies, "/api/v1/admin/financial/currency-settings": settings}.get(req.url.path)
        return httpx.Response(status if body is not None else 404, text=json.dumps({"data": body}) if body is not None else "")
    return fx.WiseCpCurrencies("https://billing.test", "k-test", "USD", httpx.Client(transport=httpx.MockTransport(handler)))


GOOD = [{"id": 4, "code": "USD", "local": True, "rate": 1.0}, {"id": 108, "code": "NGN", "local": False, "rate": 1325.7182},
        {"id": 76, "code": "JPY", "local": False, "rate": 156.8426}]
FRESH = lambda: {"auto_rate_enabled": True, "last_run_at": (now() - dt.timedelta(hours=2)).isoformat()}


def test_wisecp_rates_are_read_as_exact_decimals(client):
    quotes = wisecp(GOOD, FRESH()).fetch()
    assert {(q.quote, str(q.rate)) for q in quotes} == {("NGN", "1325.7182"), ("JPY", "156.8426")}   # never a binary float
    assert all(q.base == "USD" and q.source == "wisecp:currencies" for q in quotes)


@pytest.mark.parametrize("cur,settings,code", [
    (GOOD, {"auto_rate_enabled": True, "last_run_at": (now() - dt.timedelta(days=5)).isoformat()}, "provider_stale"),
    (GOOD, {"auto_rate_enabled": True}, "provider_stale"),
    ([{"id": 4, "code": "USD", "local": True, "rate": 1.0}, {"id": 5, "code": "EUR", "local": True, "rate": 0.9}], FRESH(), "base_changed"),
    ([{"id": 108, "code": "NGN", "local": True, "rate": 1.0}, {"id": 4, "code": "USD", "local": False, "rate": 0.0007}], FRESH(), "base_changed"),
    ([{"id": 4, "code": "USD", "local": True, "rate": 1.0}], FRESH(), "provider_bad_payload"),
    ("not a list", FRESH(), "provider_bad_payload"),
])
def test_wisecp_adapter_fails_closed(client, cur, settings, code):
    with pytest.raises(fx.FxError) as e:
        wisecp(cur, settings).fetch()
    assert e.value.code == code


def test_wisecp_refusal_and_outage_are_reported(client):
    with pytest.raises(fx.FxError) as e:
        wisecp(GOOD, FRESH(), status=403).fetch()
    assert e.value.code == "provider_refused"
    down = fx.WiseCpCurrencies("https://billing.test", "k", "USD", httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))))
    with pytest.raises(fx.FxError) as e:
        down.fetch()
    assert e.value.code == "provider_unreachable"


def test_fx_sync_endpoint_stores_rates_and_audits_failures(client, monkeypatch):
    monkeypatch.setattr(fx, "get_fx_provider", lambda: wisecp(GOOD, FRESH()))
    r = client.post("/admin/fx/sync", headers=H(ADMIN))
    assert r.status_code == 200 and r.json()["stored"] == 2           # NGN (a 17% move, under the 25% threshold) and JPY (first sighting)
    monkeypatch.setattr(fx, "get_fx_provider", lambda: wisecp(GOOD, {"auto_rate_enabled": False}))
    r = client.post("/admin/fx/sync", headers=H(ADMIN))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "provider_stale"
    with database.SessionLocal() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "fx.sync", AuditLog.outcome == "failure")).first()


def test_fx_sync_not_configured_is_a_clear_error(client):
    r = client.post("/admin/fx/sync", headers=H(ADMIN))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "provider_not_configured"
