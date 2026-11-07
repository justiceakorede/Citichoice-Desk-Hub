import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import db as database
from app.models import AuditLog, FxRate, LedgerEntry, Wallet
from .conftest import ADMIN, INTERNAL, H, bind_key, credit, fx_row, mk_tenant


def post_tenant(client, **kw):
    body = {"billing_currency": "NGN", "wisecp_service_id": "s-x", "name": "X", "owner_email": "x@example.com", "plan": "growth", **kw}
    return client.post("/admin/tenants", headers=H(ADMIN), json=body)


def test_unknown_and_disabled_currencies_cannot_open_a_wallet(client):
    assert post_tenant(client, billing_currency="ZZZ").json()["detail"]["code"] == "currency_not_enabled"
    assert post_tenant(client, billing_currency="EUR").json()["detail"]["code"] == "currency_not_enabled"      # real, but not switched on
    assert post_tenant(client, billing_currency="XAU").status_code == 422                                      # metal: no exponent
    assert post_tenant(client, billing_currency="ngn").status_code == 422                                      # must be upper-case ISO


def test_staff_can_enable_a_currency_and_it_is_audited(client):
    assert client.patch("/admin/currencies/EUR", headers=H(ADMIN), json={"billing_enabled": True}).json()["billing_enabled"]
    assert post_tenant(client, billing_currency="EUR").status_code == 200
    with database.SessionLocal() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "currency.billing")).one().detail["code"] == "EUR"
    assert client.patch("/admin/currencies/EUR", headers=H(ADMIN), json={"billing_enabled": False}).status_code == 409   # wallets exist
    assert client.patch("/admin/currencies/XAU", headers=H(ADMIN), json={"billing_enabled": True}).status_code == 422
    assert {c["code"] for c in client.get("/admin/currencies?enabled=true", headers=H(ADMIN)).json()} >= {"NGN", "USD", "EUR"}


def test_repeat_create_in_another_currency_is_refused_not_reinterpreted(client, cw):
    mk_tenant(client, cw, currency="NGN")
    r = post_tenant(client, wisecp_service_id="svc-1", billing_currency="USD")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "currency_mismatch"


def test_credit_must_match_the_wallet_currency(client, cw):
    tid = mk_tenant(client, cw, currency="NGN")
    r = credit(client, tid, "10", "k-usd-1", currency="USD")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "currency_mismatch"
    assert credit(client, tid, "10", "k-ngn-1").json()["balance"] == "NGN 10.00"


@pytest.mark.parametrize("currency,amount,ok", [("NGN", "100.123", False), ("NGN", "100.12", True),
                                                ("JPY", "1000.5", False), ("JPY", "1000", True),
                                                ("KWD", "1.2345", False), ("KWD", "1.234", True)])
def test_amounts_are_checked_against_the_currencys_own_decimals(client, cw, currency, amount, ok):
    tid = mk_tenant(client, cw, currency=currency)
    r = credit(client, tid, amount, "k-dec-1", currency=currency)
    assert (r.status_code == 200) is ok
    if not ok:
        assert r.json()["detail"]["code"] == "bad_amount"


def test_json_numbers_are_not_turned_into_binary_floats(client, cw):
    tid = mk_tenant(client, cw)
    r = client.post(f"/admin/wallets/{tid}/credit", headers={**H(ADMIN), "content-type": "application/json"},
                    content='{"amount": 0.1, "currency": "NGN", "idempotency_key": "k-float-1"}')
    assert r.status_code == 200 and r.json()["balance_micro"] == 100_000
    r = client.post(f"/admin/wallets/{tid}/credit", headers={**H(ADMIN), "content-type": "application/json"},
                    content='{"amount": 0.30000000000000004, "currency": "NGN", "idempotency_key": "k-float-2"}')
    assert r.status_code == 422                                        # more precision than NGN has: refused, not rounded


def test_database_refuses_a_ledger_entry_in_the_wrong_currency(client, cw):
    tid = mk_tenant(client, cw, currency="NGN")
    with pytest.raises(IntegrityError), database.engine.begin() as c:
        c.execute(text("insert into ledger_entries (id, tenant_id, kind, currency, amount_micro, balance_after_micro, idempotency_key, created_at) "
                       "values ('bad', :t, 'credit', 'USD', 1, 1, 'bad-key', current_timestamp)"), {"t": tid})


def usd_tenant(client, cw, key):
    tid = mk_tenant(client, cw, service="svc-usd", name="UsdCo", currency="USD")
    bind_key(key, [tid])
    credit(client, tid, "10", "usd-topup-1", currency="USD")
    return tid


def test_usd_wallet_charges_sub_cent_amounts_exactly(client, cw):
    tid = usd_tenant(client, cw, "chd_usd")
    r = client.post("/internal/usage", headers=H("chd_usd"), json={"external_id": "u1", "category": "utility", "country": "NG"}).json()
    # 0.0067 USD x 1.03 buffer = 6,901 micro; x 1.4 markup = 9,661.4 -> 9,661. Not a whole cent.
    assert (r["currency"], r["provider_cost_micro"], r["fx_buffer_micro"], r["charge_micro"]) == ("USD", 6_700, 201, 9_661)
    assert r["balance_micro"] == 10_000_000 - 9_661


def test_a_thousand_sub_cent_messages_total_exactly(client, cw):
    tid = usd_tenant(client, cw, "chd_usd")
    for i in range(1000):
        client.post("/internal/usage", headers=H("chd_usd"), json={"external_id": f"m{i}", "category": "utility", "country": "NG"})
    with database.SessionLocal() as s:
        assert s.get(Wallet, tid).balance_micro == 10_000_000 - 1000 * 9_661
    w = client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["wallet"]
    assert w["balance"] == "USD 0.34"                       # 0.338990 shown to cents once
    assert w["alert"] == "critical"                         # under 10% of the last 30 days' spend, by integer maths
    # billing each message at a whole cent instead would have taken $10.00 and emptied the wallet


def test_yen_wallet_keeps_fractions_but_displays_whole_yen(client, cw):
    with database.SessionLocal() as s:
        s.add(fx_row("JPY", 150))
        s.commit()
    tid = mk_tenant(client, cw, service="svc-jpy", name="YenCo", currency="JPY")
    bind_key("chd_jpy", [tid])
    assert credit(client, tid, "5000", "jpy-topup-1", currency="JPY").json()["balance"] == "JPY 5,000"
    r = client.post("/internal/usage", headers=H("chd_jpy"), json={"external_id": "j1", "category": "utility", "country": "JP"}).json()
    assert r["charge_micro"] == 1_449_210                       # 1.44921 yen, kept exactly
    assert client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["wallet"]["balance"] == "JPY 4,999"
    led = client.get(f"/admin/wallets/{tid}/ledger", headers=H(ADMIN)).json()
    assert led[0]["amount"] == "-JPY 1" and led[0]["currency"] == "JPY" and led[0]["amount_micro"] == -1_449_210


def test_missing_fx_rate_for_the_wallet_currency_fails_safe(client, cw):
    tid = mk_tenant(client, cw, service="svc-kwd", name="KwdCo", currency="KWD")
    bind_key("chd_kwd", [tid])
    credit(client, tid, "100", "kwd-topup-1", currency="KWD")
    r = client.post("/internal/usage", headers=H("chd_kwd"), json={"external_id": "k1", "category": "utility", "country": "KW"})
    assert r.status_code == 422                                  # no KWD rate: never charge zero
    assert client.post("/internal/send-check", headers=H("chd_kwd"), json={"category": "marketing"}).json()["reason"] in ("no_rate_card", "no_fx_rate")


def test_marketing_floor_follows_the_wallet_currency(client, cw, tenant):
    # floor = USD 5 converted at 1600 NGN per USD = NGN 8,000
    credit(client, tenant, "8000.01", "floor-1")
    chk = lambda: client.post("/internal/send-check", headers=H(INTERNAL), json={"category": "marketing"}).json()
    r = chk()
    assert r["reason"] == "below_marketing_minimum" and r["minimum_micro"] == 8_000_000_000
    credit(client, tenant, "500", "floor-2")
    assert chk()["allow"] is True


def test_ledger_always_sums_to_the_wallet_balance(client, cw, tenant):
    credit(client, tenant, "10000", "sum-1")
    for i in range(25):
        client.post("/internal/usage", headers=H(INTERNAL), json={"external_id": f"s{i}", "category": "utility", "country": "NG"})
    from .conftest import adjust
    assert adjust(tenant, "wallet_debit", "12.34")[0].status_code == 303
    with database.SessionLocal() as s:
        total = sum(e.amount_micro for e in s.scalars(select(LedgerEntry).where(LedgerEntry.tenant_id == tenant)))
        assert total == s.get(Wallet, tenant).balance_micro
        assert {e.currency for e in s.scalars(select(LedgerEntry))} == {"NGN"}
