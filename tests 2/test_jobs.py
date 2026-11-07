import datetime as dt

from sqlalchemy import select, text

from app import db as database
from app.models import AuditLog, ProvisioningJob, Tenant, now
from app.services import jobs
from .conftest import ADMIN, H, make_due, mk_tenant, run_jobs


def tenant_row(tid):
    with database.SessionLocal() as s:
        t = s.get(Tenant, tid)
        return t.status, t.chatwoot_account_id, t.chatwoot_user_id


def job_row(tid, op="provision_tenant"):
    with database.SessionLocal() as s:
        j = s.scalar(select(ProvisioningJob).where(ProvisioningJob.tenant_id == tid, ProvisioningJob.operation == op))
        return j.id, j.status, j.attempts, j.last_error, dict(j.result or {})


def test_provisioning_succeeds_through_the_job(client, cw):
    tid = mk_tenant(client, cw, run=False)
    assert tenant_row(tid)[0] == "pending" and job_row(tid)[1] == "queued"
    run_jobs(cw)
    status, acct, user = tenant_row(tid)
    assert status == "active" and acct and user
    assert cw.count("create_account") == cw.count("create_user") == cw.count("add_user") == 1
    assert job_row(tid)[1] == "succeeded"


def test_create_twice_makes_one_job_and_one_account(client, cw):
    mk_tenant(client, cw, run=False)
    mk_tenant(client, cw, run=False)
    with database.SessionLocal() as s:
        assert len(s.scalars(select(ProvisioningJob)).all()) == 1
    run_jobs(cw)
    assert cw.count("create_account") == 1


def test_temporary_failure_backs_off_then_resumes_without_duplicates(client, cw):
    tid = mk_tenant(client, cw, run=False)
    cw.fail["create_user"] = [(False, False)]                 # account is created, then user creation fails cleanly
    with database.SessionLocal() as s:
        jobs.run_once(s, cw, "w")
    status, acct, user = tenant_row(tid)
    _, jstatus, attempts, err, steps = job_row(tid)
    assert (status, jstatus, attempts) == ("provisioning", "queued", 1) and acct and not user and "scripted" in err
    with database.SessionLocal() as s:
        assert jobs.claim_next(s, "w") is None                # not due yet: backoff is respected
    run_jobs(cw)
    assert tenant_row(tid)[0] == "active"
    assert cw.count("create_account") == 1 and cw.count("create_user") == 1


def test_ambiguous_account_creation_blocks_instead_of_risking_a_duplicate(client, cw):
    tid = mk_tenant(client, cw, run=False)
    cw.fail["create_account"] = [(True, True)]                # request timed out AFTER Chatwoot created it
    run_jobs(cw)
    assert tenant_row(tid)[0] == "provisioning_failed" and job_row(tid)[1] == "blocked"
    assert run_jobs(cw) == []                                 # nothing retries on its own
    jid = job_row(tid)[0]
    r = client.post(f"/admin/jobs/{jid}/retry", headers=H(ADMIN), json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "retry_refused"
    found = next(iter(cw.accounts))                           # operator finds the account in Chatwoot
    assert client.post(f"/admin/jobs/{jid}/retry", headers=H(ADMIN), json={"chatwoot_account_id": found}).status_code == 200
    run_jobs(cw)
    assert tenant_row(tid)[0] == "active" and tenant_row(tid)[1] == found
    assert len(cw.accounts) == 1 and cw.count("create_account") == 1


def test_operator_can_confirm_nothing_was_created(client, cw):
    tid = mk_tenant(client, cw, run=False)
    cw.fail["create_account"] = [(True, False)]               # timed out, and nothing was created
    run_jobs(cw)
    jid = job_row(tid)[0]
    assert client.post(f"/admin/jobs/{jid}/retry", headers=H(ADMIN), json={"confirm_not_created": True}).status_code == 200
    run_jobs(cw)
    assert tenant_row(tid)[0] == "active" and len(cw.accounts) == 1


def test_exhausted_retries_fail_the_tenant_audit_it_and_can_be_retried(client, cw):
    tid = mk_tenant(client, cw, run=False)
    cw.fail["create_account"] = [(False, False)] * 5
    run_jobs(cw)
    _, jstatus, attempts, err, _ = job_row(tid)
    assert (jstatus, attempts) == ("dead", 5) and tenant_row(tid)[0] == "provisioning_failed"
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "job.provision_tenant", AuditLog.outcome == "failure")).first()
        assert row and row.tenant_id == tid
    jid = job_row(tid)[0]
    assert client.post(f"/admin/jobs/{jid}/retry", headers=H(ADMIN), json={}).status_code == 200
    run_jobs(cw)
    assert tenant_row(tid)[0] == "active" and cw.count("create_account") == 1


def test_adopting_an_existing_chatwoot_account_skips_creation(client, cw):
    tid = mk_tenant(client, cw, chatwoot_account_id=555, create_owner_user=False)
    assert tenant_row(tid) == ("active", 555, None)
    assert cw.count("create_account") == 0 and cw.count("create_user") == 0


def test_cancelled_while_queued_is_a_noop(client, cw):
    tid = mk_tenant(client, cw, run=False)
    client.post(f"/admin/tenants/{tid}/cancel", headers=H(ADMIN))
    run_jobs(cw)
    assert tenant_row(tid)[0] == "cancelled" and len(cw.accounts) == 0


def test_expired_lease_is_reclaimed_and_live_lease_is_not(client, cw):
    tid = mk_tenant(client, cw, run=False)
    with database.SessionLocal() as s:
        j = jobs.claim_next(s, "crashed-worker")
        assert j.attempts == 1 and j.status == "running"
        assert jobs.claim_next(s, "other") is None            # lease still valid
        j.locked_until = now() - dt.timedelta(seconds=1)
        s.commit()
        again = jobs.claim_next(s, "rescuer")
        assert again.id == j.id and again.attempts == 2 and again.locked_by == "rescuer"


def test_worker_lost_on_final_attempt_goes_dead(client, cw):
    tid = mk_tenant(client, cw, run=False)
    with database.SessionLocal() as s:
        j = jobs.claim_next(s, "w")
        j.attempts, j.locked_until = j.max_attempts, now() - dt.timedelta(seconds=1)
        s.commit()
        assert jobs.claim_next(s, "w2") is None
    assert job_row(tid)[1] == "dead" and tenant_row(tid)[0] == "provisioning_failed"


def test_login_link_is_returned_once_and_never_stored(client, cw):
    tid = mk_tenant(client, cw)
    r = client.post(f"/admin/tenants/{tid}/invite", headers=H(ADMIN))
    assert r.json()["invite_link"].startswith("https://desk.test/login/")
    with database.engine.connect() as c:
        for table in ("audit_log", "provisioning_jobs", "tenants"):
            dump = str(c.execute(text(f"select * from {table}")).fetchall())
            assert "one-time-secret" not in dump and "desk.test" not in dump
