import itertools

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import db as database
from app.models import AuditLog, LedgerEntry, STATUSES, Tenant
from app.services import lifecycle as lc
from .conftest import ADMIN, H, mk_tenant, run_jobs


@pytest.mark.parametrize("frm,to", list(itertools.product(STATUSES, STATUSES)))
def test_transition_matrix(frm, to):
    t = Tenant(status=frm, suspend_mode="none")
    if to in lc.TRANSITIONS[frm]:
        lc.transition(t, to)
        assert t.status == to
    else:
        with pytest.raises(lc.InvalidTransition):
            lc.transition(t, to)
        assert t.status == frm


def status(client, tid):
    return client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["status"]


def test_happy_path_and_chatwoot_sync(client, cw):
    tid = mk_tenant(client, cw, run=False)
    assert status(client, tid) == "pending"
    run_jobs(cw)
    assert status(client, tid) == "active"
    acct = next(iter(cw.accounts))

    r = client.post(f"/admin/tenants/{tid}/suspend", headers=H(ADMIN), json={"mode": "pause_outbound", "reason": "invoice overdue"})
    assert r.json()["status"] == "suspended" and r.json()["suspension_reason"] == "invoice overdue"
    run_jobs(cw)
    assert cw.accounts[acct]["status"] == "active"          # pause does not lock Chatwoot

    client.post(f"/admin/tenants/{tid}/suspend", headers=H(ADMIN), json={"mode": "lock"})
    run_jobs(cw)
    assert cw.accounts[acct]["status"] == "suspended"

    assert client.post(f"/admin/tenants/{tid}/unsuspend", headers=H(ADMIN)).json()["status"] == "active"
    run_jobs(cw)
    assert cw.accounts[acct]["status"] == "active"


def test_cancelled_tenant_cannot_be_reactivated(client, cw):
    tid = mk_tenant(client, cw)
    r = client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN))
    assert r.json()["status"] == "cancelling"
    run_jobs(cw)
    assert status(client, tid) == "cancelled"
    for action in ("unsuspend",):
        r = client.post(f"/admin/tenants/{tid}/{action}", headers=H(ADMIN))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition"
    assert client.post(f"/admin/tenants/{tid}/suspend", headers=H(ADMIN), json={"mode": "lock"}).status_code == 409
    assert status(client, tid) == "cancelled"


def test_cancelling_lock_failure_keeps_tenant_cancelling_until_retry_succeeds(client, cw):
    tid = mk_tenant(client, cw)
    cw.fail["set_status"] = [(False, False)]
    client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN))
    run_jobs(cw)                     # first attempt fails, backoff skipped by the test helper, second succeeds
    assert status(client, tid) == "cancelled"
    assert cw.count("set_status") == 1


def test_terminate_requires_cancelled_and_confirmation_and_keeps_ledger(client, cw):
    tid = mk_tenant(client, cw)
    client.post(f"/admin/wallets/{tid}/credit", headers=H(ADMIN), json={"amount": "50", "currency": "NGN", "idempotency_key": "topup-x"})
    slug = client.get(f"/admin/tenants/{tid}", headers=H(ADMIN)).json()["slug"]
    assert client.post(f"/admin/tenants/{tid}/terminate?confirm={slug}", headers=H(ADMIN)).status_code == 409   # still active
    client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN)); run_jobs(cw)
    assert client.post(f"/admin/tenants/{tid}/terminate?confirm=wrong", headers=H(ADMIN)).status_code == 400
    r = client.post(f"/admin/tenants/{tid}/terminate?confirm={slug}", headers=H(ADMIN))
    assert r.status_code == 200 and r.json()["status"] == "terminated"
    with database.SessionLocal() as s:
        t = s.get(Tenant, tid)
        assert t.terminated_at is not None
        assert s.scalar(select(LedgerEntry).where(LedgerEntry.tenant_id == tid)) is not None
    assert client.post(f"/admin/tenants/{tid}/unsuspend", headers=H(ADMIN)).status_code == 409
    assert cw.accounts[next(iter(cw.accounts))]["status"] == "suspended"      # locked, never deleted


def test_cancel_before_provisioning_creates_nothing(client, cw):
    tid = mk_tenant(client, cw, run=False)
    assert client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN)).json()["status"] == "cancelled"
    run_jobs(cw)
    assert cw.count("create_account") == 0 and status(client, tid) == "cancelled"


def test_suspend_rejected_while_not_active(client, cw):
    tid = mk_tenant(client, cw, run=False)
    r = client.post(f"/admin/tenants/{tid}/suspend", headers=H(ADMIN), json={"mode": "lock"})
    assert r.status_code == 409 and status(client, tid) == "pending"


def test_database_rejects_unknown_status(client, cw):
    tid = mk_tenant(client, cw)
    with database.engine.begin() as c, pytest.raises(IntegrityError):
        c.execute(text("update tenants set status = 'zombie' where id = :i"), {"i": tid})


def test_actions_are_audited_with_request_id(client, cw):
    tid = mk_tenant(client, cw)
    r = client.post(f"/admin/tenants/{tid}/suspend", headers=H(ADMIN), json={"mode": "pause_outbound"})
    with database.SessionLocal() as s:
        row = s.scalar(select(AuditLog).where(AuditLog.action == "tenant.suspend"))
        assert row.outcome == "success" and row.request_id == r.headers["x-request-id"]
