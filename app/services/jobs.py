"""PostgreSQL-backed job queue for Chatwoot operations.

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED plus a lease, so concurrent workers never run the same
job and a crashed worker's job is picked up again after the lease expires. Handlers are resumable: each
external step is persisted before the next one starts."""
import datetime as dt

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ProvisioningJob, Tenant, now
from . import lifecycle
from . import tenants as ts
from .chatwoot import ChatwootClient, ProvisioningError

LEASE_SECONDS = 300


class JobBlocked(Exception):
    """Needs a human: continuing automatically could create a duplicate in Chatwoot."""


def enqueue(db: Session, tenant_id: str, operation: str, key: str, payload: dict | None = None) -> ProvisioningJob:
    existing = db.scalar(select(ProvisioningJob).where(ProvisioningJob.idempotency_key == key))
    if existing:
        return existing
    job = ProvisioningJob(tenant_id=tenant_id, operation=operation, idempotency_key=key, payload=payload or {}, result={})
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return db.scalar(select(ProvisioningJob).where(ProvisioningJob.idempotency_key == key))
    return job


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    return d.replace(tzinfo=dt.timezone.utc) if d is not None and d.tzinfo is None else d


def claim_next(db: Session, worker_id: str, lease: int = LEASE_SECONDS) -> ProvisioningJob | None:
    n = now()
    job = db.scalar(
        select(ProvisioningJob)
        .where(or_(and_(ProvisioningJob.status == "queued", ProvisioningJob.next_attempt_at <= n),
                   and_(ProvisioningJob.status == "running", ProvisioningJob.locked_until < n)))
        .order_by(ProvisioningJob.created_at).limit(1).with_for_update(skip_locked=True))
    if not job:
        return None
    if job.status == "running" and job.attempts >= job.max_attempts:   # lease expired on the last attempt
        _terminal(db, job, "dead", "worker lost after final attempt")
        db.commit()
        return None
    job.status, job.attempts, job.locked_by = "running", job.attempts + 1, worker_id
    job.locked_until = n + dt.timedelta(seconds=lease)
    job.started_at = job.started_at or n
    db.commit()
    return job


def _terminal(db: Session, job: ProvisioningJob, status: str, error: str) -> None:
    job.status, job.last_error, job.completed_at = status, error[:500], now()
    job.locked_by = job.locked_until = None
    t = db.get(Tenant, job.tenant_id)
    if job.operation == "provision_tenant" and t:
        if t.status == "pending":              # worker died before its first step
            lifecycle.transition(t, "provisioning")
        if t.status == "provisioning":
            lifecycle.transition(t, "provisioning_failed")
    ts.audit(db, "worker", f"job.{job.operation}", job.tenant_id, {"job": job.id, "status": status, "error": error[:300]}, "failure")


def _backoff(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 900))


def run_job(db: Session, cw: ChatwootClient, job: ProvisioningJob) -> str:
    t = db.get(Tenant, job.tenant_id)
    try:
        HANDLERS[job.operation](db, cw, job, t)
        job.status, job.last_error, job.completed_at = "succeeded", None, now()
        job.locked_by = job.locked_until = None
        ts.audit(db, "worker", f"job.{job.operation}", job.tenant_id, {"job": job.id, "attempts": job.attempts})
    except JobBlocked as e:
        db.rollback(); job = db.get(ProvisioningJob, job.id)
        _terminal(db, job, "blocked", str(e))
    except ProvisioningError as e:
        db.rollback(); job = db.get(ProvisioningJob, job.id)
        if job.attempts >= job.max_attempts:
            _terminal(db, job, "dead", str(e))
        else:
            job.status, job.last_error = "queued", str(e)[:500]
            job.next_attempt_at = now() + _backoff(job.attempts)
            job.locked_by = job.locked_until = None
    except Exception as e:      # never put the message in the record: it may carry data we must not store
        db.rollback(); job = db.get(ProvisioningJob, job.id)
        if job.attempts >= job.max_attempts:
            _terminal(db, job, "dead", f"unexpected {type(e).__name__}")
        else:
            job.status, job.last_error = "queued", f"unexpected {type(e).__name__}"
            job.next_attempt_at = now() + _backoff(job.attempts)
            job.locked_by = job.locked_until = None
    db.commit()
    return job.status


def run_once(db: Session, cw: ChatwootClient, worker_id: str = "worker") -> ProvisioningJob | None:
    job = claim_next(db, worker_id)
    if job:
        run_job(db, cw, job)
    return job


def _mark(db: Session, job: ProvisioningJob, **steps) -> None:
    job.result = {**(job.result or {}), **steps}
    db.commit()


def h_provision(db: Session, cw: ChatwootClient, job: ProvisioningJob, t: Tenant) -> None:
    if t.status in ("pending", "provisioning_failed"):
        lifecycle.transition(t, "provisioning")
        db.commit()
    if t.status != "provisioning":        # active already, or cancelled while queued: nothing to do
        return
    res = job.result or {}
    if not t.chatwoot_account_id:
        if res.get("account") == "started":
            raise JobBlocked("a previous attempt may have created the Chatwoot account; check Chatwoot, "
                             "then retry with chatwoot_account_id to adopt it")
        _mark(db, job, account="started")
        try:
            t.chatwoot_account_id = cw.create_account(f"{t.name} [{t.slug}]",
                                                      {"agents": t.seats_limit, "inboxes": t.channels_limit + 1})
        except ProvisioningError as e:
            if e.ambiguous:
                raise JobBlocked(f"account creation outcome unknown ({e}); check Chatwoot, then retry with chatwoot_account_id")
            _mark(db, job, account=None)
            raise
        db.commit()
        _mark(db, job, account="done")
    if (job.payload or {}).get("create_owner_user", True) and not t.chatwoot_user_id:
        if (job.result or {}).get("user") == "started":
            raise JobBlocked("a previous attempt may have created the owner user; check Chatwoot, "
                             "then retry with chatwoot_user_id to adopt it")
        _mark(db, job, user="started")
        try:
            t.chatwoot_user_id = cw.create_user(t.owner_name or t.name, t.owner_email)
        except ProvisioningError as e:
            if e.ambiguous:
                raise JobBlocked(f"owner user creation outcome unknown ({e}); check Chatwoot, then retry with chatwoot_user_id")
            _mark(db, job, user=None)
            raise
        db.commit()
        _mark(db, job, user="done")
    if t.chatwoot_user_id and (job.result or {}).get("membership") != "done":
        cw.add_user(t.chatwoot_account_id, t.chatwoot_user_id)
        _mark(db, job, membership="done")
    lifecycle.transition(t, "active")


def h_sync_limits(db: Session, cw: ChatwootClient, job: ProvisioningJob, t: Tenant) -> None:
    if t.chatwoot_account_id:
        cw.update_account(t.chatwoot_account_id, limits={"agents": t.seats_limit, "inboxes": t.channels_limit + 1})


def h_sync_status(db: Session, cw: ChatwootClient, job: ProvisioningJob, t: Tenant) -> None:
    """Applies the tenant's *current* desired state, so out-of-order or repeated jobs are harmless."""
    if t.chatwoot_account_id:
        cw.set_status(t.chatwoot_account_id, "suspended" if lifecycle.chatwoot_locked(t) else "active")
    if t.status == "cancelling":
        lifecycle.transition(t, "cancelled")


HANDLERS = {"provision_tenant": h_provision, "sync_limits": h_sync_limits, "sync_chatwoot_status": h_sync_status}


def requeue(db: Session, job: ProvisioningJob, adopt_account: int | None = None, adopt_user: int | None = None,
            confirm_not_created: bool = False) -> None:
    if job.status not in ("dead", "blocked"):
        raise ValueError("only dead or blocked jobs can be retried")
    t = db.get(Tenant, job.tenant_id)
    res = dict(job.result or {})
    if adopt_account:
        t.chatwoot_account_id = adopt_account
        res["account"] = "adopted"
    elif res.get("account") == "started" and not t.chatwoot_account_id:
        if not confirm_not_created:
            raise ValueError("account creation outcome is unknown: pass chatwoot_account_id to adopt it, "
                             "or confirm_not_created=true after checking Chatwoot")
        res["account"] = None
    if adopt_user:
        t.chatwoot_user_id = adopt_user
        res["user"] = "adopted"
    elif res.get("user") == "started" and not t.chatwoot_user_id:
        if not confirm_not_created:
            raise ValueError("user creation outcome is unknown: pass chatwoot_user_id to adopt it, "
                             "or confirm_not_created=true after checking Chatwoot")
        res["user"] = None
    job.result, job.status, job.attempts, job.last_error = res, "queued", 0, None
    job.next_attempt_at = now()
