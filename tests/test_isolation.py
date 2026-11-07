"""Tenant A's credentials must not read or change anything of tenant B."""
import pytest
from sqlalchemy import func, select

from app import db as database
from app.config import get_settings
from .conftest import ADMIN as _A  # noqa
from app.models import AiUsage, Tenant, UsageRecord, Wallet
from .conftest import ADMIN, INTERNAL, H, bind_key, mk_tenant

TOP = 10_000 * 1_000_000      # the NGN 10,000 each tenant is credited, in micro-units
KEY_A, KEY_B, KEY_NONE, KEY_BOTH = "chd_key_a", "chd_key_b", "chd_unbound", "chd_both"


@pytest.fixture()
def customer_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "customer_api_enabled", True)


@pytest.fixture()
def two(client, cw):
    a = mk_tenant(client, cw, service="sa", name="Alpha")
    b = mk_tenant(client, cw, service="sb", name="Beta")
    for t in (a, b):
        client.post(f"/admin/wallets/{t}/credit", headers=H(ADMIN), json={"amount": "10000", "currency": "NGN", "idempotency_key": f"seed-{t}"})
    bind_key(KEY_A, [a]); bind_key(KEY_B, [b]); bind_key(KEY_NONE, []); bind_key(KEY_BOTH, [a, b])
    chb = client.post(f"/admin/tenants/{b}/channels", headers=H(ADMIN), json={"channel_type": "whatsapp", "display_name": "B line", "external_id": "pn-b"}).json()["id"]
    return a, b, chb


def balance(t):
    with database.SessionLocal() as s:
        return s.get(Wallet, t).balance_micro


def test_credential_for_a_cannot_touch_b(client, two):
    a, b, chb = two
    ev = {"category": "utility", "country": "NG"}
    # send authorisation
    assert client.post("/internal/send-check", headers=H(KEY_A), json={"tenant_id": b, "category": "utility"}).status_code == 404
    # usage / wallet debit
    r = client.post("/internal/usage", headers=H(KEY_A), json={"tenant_id": b, "external_id": "x1", **ev})
    assert r.status_code == 404 and balance(b) == TOP
    with database.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(UsageRecord).where(UsageRecord.tenant_id == b)) == 0
    # AI allowance
    assert client.post("/internal/ai-reply", headers=H(KEY_A), json={"tenant_id": b, "key": "k"}).status_code == 404
    with database.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AiUsage).where(AiUsage.tenant_id == b)) == 0
    # resource counts
    assert client.post(f"/internal/tenants/{b}/counts", headers=H(KEY_A), json={"seats_used": 4}).status_code == 404
    with database.SessionLocal() as s:
        assert s.get(Tenant, b).seats_used == 0
    # channels: list and mutate
    assert client.get(f"/internal/channels?tenant_id={b}", headers=H(KEY_A)).status_code == 404
    assert client.get("/internal/channels", headers=H(KEY_A)).json() == []          # derived tenant is A, which has none
    assert client.patch(f"/internal/channels/{chb}", headers=H(KEY_A), json={"status": "error"}).status_code == 404
    assert client.get(f"/admin/tenants/{b}/channels", headers=H(ADMIN)).json()[0]["status"] == "pending"


def test_tenant_comes_from_the_credential_not_the_body(client, two):
    a, b, _ = two
    r = client.post("/internal/usage", headers=H(KEY_A), json={"external_id": "y1", "category": "utility", "country": "NG"})
    assert r.status_code == 200 and balance(a) < TOP and balance(b) == TOP
    r = client.post("/internal/usage", headers=H(KEY_B), json={"external_id": "y2", "category": "utility", "country": "NG"})
    assert r.status_code == 200 and balance(b) < TOP


def test_event_id_belonging_to_another_tenant_is_not_reused_or_leaked(client, two):
    a, b, _ = two
    client.post("/internal/usage", headers=H(KEY_A), json={"external_id": "shared", "category": "utility", "country": "NG"})
    before = balance(b)
    r = client.post("/internal/usage", headers=H(KEY_B), json={"external_id": "shared", "category": "utility", "country": "NG"})
    assert r.status_code == 409 and balance(b) == before


def test_unbound_internal_key_has_no_access(client, two):
    a, b, chb = two
    for tid in (None, a, b):
        body = {"category": "utility"} | ({"tenant_id": tid} if tid else {})
        assert client.post("/internal/send-check", headers=H(KEY_NONE), json=body).status_code in (403, 404)
    assert client.get("/internal/channels", headers=H(KEY_NONE)).status_code == 403
    assert client.patch(f"/internal/channels/{chb}", headers=H(KEY_NONE), json={"status": "error"}).status_code == 404


def test_multi_tenant_key_must_say_which_tenant(client, two):
    a, b, _ = two
    assert client.post("/internal/send-check", headers=H(KEY_BOTH), json={"category": "service"}).status_code == 422
    assert client.post("/internal/send-check", headers=H(KEY_BOTH), json={"tenant_id": a, "category": "service"}).status_code == 200


def test_scopes_do_not_cross(client, two):
    a, b, _ = two
    assert client.get("/admin/tenants", headers=H(KEY_A)).status_code == 403
    assert client.post(f"/admin/wallets/{b}/credit", headers=H(KEY_A), json={"amount": "0.01", "currency": "NGN", "idempotency_key": "evil-1"}).status_code == 403
    assert client.post(f"/admin/tenants/{b}/suspend", headers=H(KEY_A), json={"mode": "lock"}).status_code == 403
    assert client.patch(f"/admin/tenants/{b}", headers=H(KEY_A), json={"plan": "business"}).status_code == 403
    assert client.post("/internal/ai-reply", headers=H(ADMIN), json={"tenant_id": a, "key": "k"}).status_code == 403
    assert balance(b) == TOP


def test_customer_members_see_only_their_tenant(client, two, customer_on):
    a, b, _ = two
    client.post(f"/admin/tenants/{a}/members", headers=H(ADMIN), json={"email": "ann@alpha.example.com", "role": "owner"})
    client.post(f"/admin/tenants/{b}/members", headers=H(ADMIN), json={"email": "bob@beta.example.com", "role": "owner"})
    ann = {"x-dev-subject": "sub-ann", "x-dev-email": "ann@alpha.example.com"}
    mine = client.get("/customer/tenants", headers=ann).json()
    assert [t["id"] for t in mine] == [a]
    leaked = set(mine[0]) & {"markup", "chatwoot_account_id", "wisecp_service_id", "provisioning"}
    assert not leaked
    assert client.get(f"/customer/tenants/{a}", headers=ann).status_code == 200
    assert client.get(f"/customer/tenants/{b}", headers=ann).status_code == 404
    assert client.get("/customer/tenants/does-not-exist", headers=ann).status_code == 404   # same answer as "not yours"
    assert client.get("/customer/tenants").status_code == 401


def test_customer_access_ends_when_membership_is_revoked(client, two, customer_on):
    a, _, _ = two
    mid = client.post(f"/admin/tenants/{a}/members", headers=H(ADMIN), json={"email": "ann@alpha.example.com"}).json()["id"]
    ann = {"x-dev-subject": "sub-ann", "x-dev-email": "ann@alpha.example.com"}
    assert len(client.get("/customer/tenants", headers=ann).json()) == 1
    client.delete(f"/admin/tenants/{a}/members/{mid}", headers=H(ADMIN))
    assert client.get("/customer/tenants", headers=ann).json() == []
    assert client.get(f"/customer/tenants/{a}", headers=ann).status_code == 404


def test_a_subject_cannot_claim_an_invite_for_someone_elses_email(client, two, customer_on):
    a, _, _ = two
    client.post(f"/admin/tenants/{a}/members", headers=H(ADMIN), json={"email": "ann@alpha.example.com"})
    thief = {"x-dev-subject": "sub-eve", "x-dev-email": "eve@evil.example.com"}
    assert client.get("/customer/tenants", headers=thief).json() == []


def test_dev_identity_is_refused_in_production(client, two, monkeypatch, customer_on):
    monkeypatch.setattr(get_settings(), "env", "production")
    r = client.get("/customer/tenants", headers={"x-dev-subject": "sub-ann", "x-dev-email": "ann@alpha.example.com"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "sso_not_configured"
