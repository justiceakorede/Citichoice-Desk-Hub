from sqlalchemy import select

from app import db as database
from app.models import AuditLog
from .conftest import ADMIN, INTERNAL, H, mk_tenant, run_jobs


def summ(client, tid):
    return client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()


def test_plan_defaults(client, cw):
    s = summ(client, mk_tenant(client, cw, plan="starter"))
    assert (s["seats"]["limit"], s["channels"]["limit"], s["ai_replies"]["limit"]) == (2, 1, 2000)
    assert s["markup"] == "1.500" and s["kb_level"] == "basic"


def test_overrides_addons_and_ai_packs(client, cw):
    tid = mk_tenant(client, cw, plan="growth", seats=8, markup="1.25", extra_ai_packs=2, extra_channels=1)
    s = summ(client, tid)
    assert s["seats"]["limit"] == 8 and s["channels"]["limit"] == 3
    assert s["ai_replies"]["limit"] == 6000 + 2 * 5000 and s["markup"] == "1.250"


def test_scale_requires_explicit_terms(client, cw):
    r = client.post("/admin/tenants", headers=H(ADMIN), json={"billing_currency": "NGN", "wisecp_service_id": "s9", "name": "Big", "owner_email": "a@b.co", "plan": "scale"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "entitlement_incomplete"
    tid = mk_tenant(client, cw, service="s10", name="Big", plan="scale", seats=50, channels=10, ai_replies=100000, markup="1.2")
    assert summ(client, tid)["seats"]["limit"] == 50


def test_unknown_and_inactive_plans_rejected(client, cw):
    r = client.post("/admin/tenants", headers=H(ADMIN), json={"billing_currency": "NGN", "wisecp_service_id": "s1", "name": "X", "owner_email": "a@b.co", "plan": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_plan"
    client.patch("/admin/plans/starter", headers=H(ADMIN), json={"active": False})
    r = client.post("/admin/tenants", headers=H(ADMIN), json={"billing_currency": "NGN", "wisecp_service_id": "s2", "name": "Y", "owner_email": "a@b.co", "plan": "starter"})
    assert r.status_code == 422


def test_plan_change_drops_old_overrides_and_audits_before_after(client, cw):
    tid = mk_tenant(client, cw, plan="growth", seats=99)
    r = client.patch(f"/admin/tenants/{tid}", headers=H(ADMIN), json={"plan": "business"})
    assert r.json()["seats"]["limit"] == 10 and r.json()["markup"] == "1.300"
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "tenant.entitlement_change")).first()
        assert row.detail["before"]["seats"] == 99 and row.detail["after"]["seats"] == 10
        assert row.detail["after"]["version"] == row.detail["before"]["version"] + 1


def test_change_queues_chatwoot_limit_sync(client, cw):
    tid = mk_tenant(client, cw)
    client.patch(f"/admin/tenants/{tid}", headers=H(ADMIN), json={"extra_seats": 3})
    ran = run_jobs(cw)
    assert ("sync_limits", "succeeded") in ran
    assert cw.accounts[next(iter(cw.accounts))]["limits"]["agents"] == 8


def test_plan_edit_propagates_to_tenants(client, cw):
    tid = mk_tenant(client, cw, plan="growth")
    client.patch("/admin/plans/growth", headers=H(ADMIN), json={"seats": 7})
    assert summ(client, tid)["seats"]["limit"] == 7
    run_jobs(cw)
    assert cw.accounts[next(iter(cw.accounts))]["limits"]["agents"] == 7


def test_invalid_values_rejected(client, cw):
    tid = mk_tenant(client, cw)
    assert client.patch(f"/admin/tenants/{tid}", headers=H(ADMIN), json={"extra_seats": -1}).status_code == 422
    assert client.patch(f"/admin/tenants/{tid}", headers=H(ADMIN), json={"markup": 0}).status_code == 422


def test_entitlement_markup_drives_usage_charge(client, cw, tenant):
    client.patch(f"/admin/tenants/{tenant}", headers=H(ADMIN), json={"markup": "2.0"})
    client.post(f"/admin/wallets/{tenant}/credit", headers=H(ADMIN), json={"amount": "10000", "currency": "NGN", "idempotency_key": "topup-m"})
    r = client.post("/internal/usage", headers=H(INTERNAL), json={"external_id": "w1", "category": "utility", "country": "NG"}).json()
    assert (r["provider_cost_micro"], r["fx_buffer_micro"], r["charge_micro"]) == (10_720_000, 321_600, 22_083_200)
