import pytest
from .conftest import ADMIN, INTERNAL, H, mk_tenant, run_jobs


def test_create_is_idempotent(client, cw, tenant):
    r = client.post("/admin/tenants", headers=H(ADMIN), json={
        "billing_currency": "NGN", "wisecp_service_id": "svc-1", "name": "BennyBoom", "owner_email": "b@example.com", "plan": "growth"})
    assert r.status_code == 200 and r.json()["created"] is False
    run_jobs(cw)
    assert cw.count("create_account") == 1


def test_topup_idempotent(client, tenant):
    for _ in range(2):
        r = client.post(f"/admin/wallets/{tenant}/credit", headers=H(ADMIN),
                        json={"amount": "10000", "currency": "NGN", "idempotency_key": "wisecp-addon-9"})
    assert r.json()["balance_micro"] == 10_000_000_000 and r.json()["credited"] is False


def test_usage_debit_once_and_math(client, tenant):
    client.post(f"/admin/wallets/{tenant}/credit", headers=H(ADMIN), json={"amount": "10000", "currency": "NGN", "idempotency_key": "topup-1"})
    ev = {"tenant_id": tenant, "external_id": "wamid.1", "category": "utility", "country": "NG"}
    a = client.post("/internal/usage", headers=H(INTERNAL), json=ev).json()
    b = client.post("/internal/usage", headers=H(INTERNAL), json=ev).json()
    assert a["recorded"] and not b["recorded"]
    # 0.0067 USD x 1600 x 1.03 = 11.0416 NGN = 11_041_600 micro; x 1.4 markup = 15_458_240 micro. Nothing is rounded to kobo.
    # the provider's ACTUAL cost (0.0067 USD x 1600) is separate from the 3% FX buffer and the markup
    assert (a["provider_cost_micro"], a["fx_buffer_micro"], a["charge_micro"]) == (10_720_000, 321_600, 15_458_240)
    assert a["service_fee_micro"] == 15_458_240 - 10_720_000 - 321_600
    assert a["balance_micro"] == 10_000_000_000 - 15_458_240


def test_missing_rate_fails_safe(client, tenant):
    ev = {"tenant_id": tenant, "external_id": "x", "category": "authentication"}
    assert client.post("/internal/usage", headers=H(INTERNAL), json=ev).status_code == 422


def test_send_check_rules(client, tenant):
    chk = lambda cat: client.post("/internal/send-check", headers=H(INTERNAL), json={"tenant_id": tenant, "category": cat}).json()
    assert chk("service")["allow"] is True
    assert chk("utility")["reason"] == "insufficient_funds"
    client.post(f"/admin/wallets/{tenant}/credit", headers=H(ADMIN), json={"amount": "4000", "currency": "NGN", "idempotency_key": "topup-2"})
    assert chk("utility")["allow"] is True
    assert chk("marketing")["reason"] == "below_marketing_minimum"


def test_scopes_enforced(client, tenant):
    assert client.get("/admin/tenants", headers=H(INTERNAL)).status_code == 403  # internal key on admin route
    assert client.post("/internal/ai-reply", headers=H(ADMIN), json={"tenant_id": tenant, "key": "k"}).status_code == 403
    assert client.get("/admin/tenants").status_code == 401


def test_ai_allowance(client, tenant):
    for i in range(6):                                                    # one claim is capped at 1,000 units
        r = client.post("/internal/ai-reply", headers=H(INTERNAL), json={"tenant_id": tenant, "key": f"k1-{i}", "count": 1000}).json()
    assert r["allowed"] and r["used"] == 6000
    r = client.post("/internal/ai-reply", headers=H(INTERNAL), json={"tenant_id": tenant, "key": "k2"}).json()
    assert not r["allowed"]


def test_suspend_blocks_send_and_ai(client, tenant):
    assert client.post(f"/admin/tenants/{tenant}/suspend", headers=H(ADMIN), json={"mode": "pause_outbound"}).status_code == 200
    assert client.post("/internal/ai-reply", headers=H(INTERNAL), json={"tenant_id": tenant, "key": "k1"}).json()["allowed"] is False
    assert client.post("/internal/send-check", headers=H(INTERNAL), json={"tenant_id": tenant, "category": "service"}).json()["allow"] is False


def test_ui_renders_for_signed_in_admin(client, staff_client, tenant):
    r = staff_client.get("/ui")
    assert r.status_code == 200 and "BennyBoom" in r.text and "Sign out" in r.text
    assert staff_client.get(f"/ui/tenants/{tenant}").status_code == 200
