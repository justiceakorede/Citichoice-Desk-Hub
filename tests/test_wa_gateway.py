import datetime as dt

import httpx
import pytest

from app import db as database
from app.config import get_settings
from app.models import RateCard, TenantChannel, UsageRecord, Wallet
from app.routers import wa_gateway as router_mod
from .conftest import H, INTERNAL, assert_money_invariants, credit, mk_tenant

SECRET, PID = "gwsecret", "555000111"
PATH = f"/wa/{SECRET}/v13.0/{PID}/messages"
TEXT = {"messaging_product": "whatsapp", "to": "2348012345678", "type": "text", "text": {"body": "hi"}}


@pytest.fixture()
def gwenv(client, cw, tenant, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "wa_gateway_secret", SECRET)
    with database.SessionLocal() as db:
        db.add(RateCard(category="service", market="*", currency="USD", rate_micro=6_700, effective_from=dt.date(2020, 1, 1), source="test"))
        db.add(TenantChannel(tenant_id=tenant, channel_type="whatsapp", display_name="WA", external_id=PID, status="connected"))
        db.commit()
    calls = []

    def fake(request, path, raw):
        calls.append(path)
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(calls)}"}]})
    monkeypatch.setattr(router_mod, "_forward", fake)
    return calls


def wallet(tid):
    with database.SessionLocal() as s:
        w = s.get(Wallet, tid)
        return w.balance_micro, w.receivable_micro, w.reserved_micro


def test_empty_wallet_stops_the_send_and_meta_is_never_called(client, tenant, gwenv):
    r = client.post(PATH, json=TEXT)
    assert r.status_code == 402 and "credit is empty" in r.json()["error"]["message"]
    assert gwenv == []                                  # nothing reached Meta
    assert wallet(tenant) == (0, 0, 0)


def test_funded_send_is_charged_immediately_and_stops_at_zero(client, tenant, gwenv):
    credit(client, tenant, "100", "g-1")                # NGN 100 buys a few messages, not many
    sent = 0
    for _ in range(50):
        r = client.post(PATH, json=TEXT)
        if r.status_code != 200:
            break
        sent += 1
    assert 0 < sent < 50 and len(gwenv) == sent         # the gateway cut the sending off by itself
    bal, owed, held = wallet(tenant)
    assert owed == 0 and held == 0 and bal >= 0         # never negative, never a receivable
    assert client.post(PATH, json=TEXT).status_code == 402
    with database.SessionLocal() as s:
        assert s.query(UsageRecord).count() == sent
    assert_money_invariants()


def test_meta_rejection_releases_the_hold(client, tenant, gwenv, monkeypatch):
    credit(client, tenant, "100", "g-2")
    monkeypatch.setattr(router_mod, "_forward", lambda *a: httpx.Response(400, json={"error": {"message": "bad"}}))
    assert client.post(PATH, json=TEXT).status_code == 400
    assert wallet(tenant) == (100 * 1_000_000, 0, 0)


TPL = {"messaging_product": "whatsapp", "to": "2348012345678", "type": "template", "template": {"name": "promo", "language": {"code": "en"}}}


def _tpl_env(monkeypatch, category):
    from app.services import wa_gateway as svc
    svc._TPL_CACHE.clear()
    with database.SessionLocal() as db:
        ch = db.query(TenantChannel).filter_by(external_id=PID).one()
        ch.external_account_id = "waba1"
        db.commit()
    monkeypatch.setattr(svc, "template_category", lambda waba, token, body: category)


def test_template_is_charged_at_its_real_category(client, tenant, gwenv, monkeypatch):
    credit(client, tenant, "2000", "g-4")
    _tpl_env(monkeypatch, "utility")
    assert client.post(PATH, json=TPL).status_code == 200
    with database.SessionLocal() as s:
        assert [u.category for u in s.query(UsageRecord).all()] == ["utility"]
    assert wallet(tenant)[2] == 0


def test_unresolvable_template_is_gated_but_left_to_the_true_up(client, tenant, gwenv, monkeypatch):
    credit(client, tenant, "2000", "g-5")
    _tpl_env(monkeypatch, None)
    assert client.post(PATH, json=TPL).status_code == 200
    with database.SessionLocal() as s:
        assert s.query(UsageRecord).count() == 0          # not charged here: the daily Meta true-up charges it, once
    assert wallet(tenant) == (2000 * 1_000_000, 0, 0)     # hold released, nothing double counted


def test_template_with_no_credit_never_reaches_meta(client, tenant, gwenv, monkeypatch):
    _tpl_env(monkeypatch, "marketing")
    assert client.post(PATH, json=TPL).status_code == 402 and gwenv == []


def test_read_receipts_and_typing_pass_free_even_with_no_credit(client, tenant, gwenv):
    r = client.post(PATH, json={"messaging_product": "whatsapp", "status": "read", "message_id": "w", "typing_indicator": {"type": "text"}})
    assert r.status_code == 200 and len(gwenv) == 1


def test_wrong_secret_is_404_and_suspended_tenant_is_blocked(client, tenant, gwenv):
    assert client.post(f"/wa/nope/v13.0/{PID}/messages", json=TEXT).status_code == 404
    credit(client, tenant, "100", "g-3")
    assert client.post(f"/admin/tenants/{tenant}/suspend", headers=H("chd_admin_key"), json={"mode": "pause_outbound", "reason": "t"}).status_code == 200
    r = client.post(PATH, json=TEXT)
    assert r.status_code == 402 and gwenv == []
