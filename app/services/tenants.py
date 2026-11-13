import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import money
from ..context import request_id_var
from ..models import AiUsage, AuditLog, LedgerEntry, ProvisioningJob, Tenant, UsageRecord, Wallet, now, uid
from . import channels as chs
from . import wallet as wl
from .periods import billing_period, period_start_utc
import datetime as dt


def slugify(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "workspace") + "-" + uid()[:4]


def audit(db: Session, actor: str, action: str, tenant_id: str | None = None, detail: dict | None = None,
          outcome: str = "success") -> None:
    db.add(AuditLog(actor=actor, action=action, tenant_id=tenant_id, detail=detail, outcome=outcome,
                    request_id=request_id_var.get()))


def summary(db: Session, t: Tenant) -> dict:
    w = db.get(Wallet, t.id)
    ms = period_start_utc()
    rows = db.execute(
        select(UsageRecord.category, func.count(), func.coalesce(func.sum(UsageRecord.provider_cost_micro), 0), func.coalesce(func.sum(UsageRecord.fx_buffer_micro), 0),
               func.coalesce(func.sum(UsageRecord.service_fee_micro), 0), func.coalesce(func.sum(UsageRecord.charge_micro), 0))
        .where(UsageRecord.tenant_id == t.id, UsageRecord.created_at >= ms)
        .group_by(UsageRecord.category)).all()
    by_cat = {c: dict(messages=n, provider_cost_micro=int(m), fx_buffer_micro=int(bf), service_fee_micro=int(f), charge_micro=int(ch))
              for c, n, m, bf, f, ch in rows}
    ai = db.get(AiUsage, (t.id, billing_period()))
    spend30 = db.scalar(select(func.coalesce(func.sum(-LedgerEntry.amount_micro), 0)).where(
        LedgerEntry.tenant_id == t.id, LedgerEntry.kind == "debit",
        LedgerEntry.created_at >= now() - dt.timedelta(days=30))) or 0
    spend30 = int(spend30)                 # PostgreSQL returns SUM(bigint) as Decimal; money stays integer
    bal = w.balance_micro
    alert = None
    if w.receivable_micro > 0: alert = "debt"
    elif bal <= 0: alert = "zero"
    elif spend30 and bal * 10 < spend30: alert = "critical"      # under 10% of the last 30 days' spend (integer maths)
    elif spend30 and bal * 5 < spend30: alert = "low"            # under 20%
    job = db.scalar(select(ProvisioningJob).where(ProvisioningJob.tenant_id == t.id,
                                                  ProvisioningJob.operation == "provision_tenant"))
    return dict(
        id=t.id, wisecp_service_id=t.wisecp_service_id, name=t.name, slug=t.slug, status=t.status,
        suspend_mode=t.suspend_mode, suspension_reason=t.suspension_reason, billing_currency=t.billing_currency, plan=t.plan, kb_level=t.kb_level,
        markup=str(t.markup),
        seats=dict(used=t.seats_used, limit=t.seats_limit),
        channels=dict(used=chs.messaging_count(db, t.id), limit=t.channels_limit),
        ai_replies=dict(used=ai.replies if ai else 0, limit=t.ai_limit, period=billing_period()),
        wallet=dict(currency=w.currency, balance_micro=bal, balance=money.format_micro(bal, w.currency),
                    receivable_micro=w.receivable_micro, receivable=money.format_micro(w.receivable_micro, w.currency),
                    reserved_micro=w.reserved_micro, credit_limit_micro=w.credit_limit_micro,
                    credit_limit=money.format_micro(w.credit_limit_micro, w.currency),
                    available_micro=max(0, bal - w.reserved_micro), capacity_micro=wl.capacity_micro(w),
                    spend_30d_micro=int(spend30), alert=alert),
        month_usage=by_cat, chatwoot_account_id=t.chatwoot_account_id,
        provisioning=dict(job_id=job.id, status=job.status, attempts=job.attempts, last_error=job.last_error) if job else None,
    )


CUSTOMER_HIDDEN = {"markup", "chatwoot_account_id", "wisecp_service_id", "provisioning", "suspension_reason"}


def customer_view(db: Session, t: Tenant) -> dict:
    """What a tenant's own members may see. No internal pricing, no Chatwoot or WiseCP identifiers."""
    return {k: v for k, v in summary(db, t).items() if k not in CUSTOMER_HIDDEN}


DELETABLE = ("pending", "provisioning_failed", "cancelling", "cancelled", "terminated")


def delete_tenant(db, t, actor: str, confirm: str, purge_history: bool = False) -> None:
    """Manual, permanent removal of a tenant row and everything that hangs off it (wallet, channels, jobs, settings, key links).
    Refused when the tenant has money history (ledger, usage, adjustments, receivables): that is accounting and is kept.
    The audit trail is kept too. The Chatwoot account is NOT touched."""
    from sqlalchemy import delete, func, select
    from ..models import AdjustmentRequest, Base, LedgerEntry, ReceivableEntry, UsageRecord
    if confirm != t.slug:
        raise ValueError("confirmation_required: type the tenant slug to confirm")
    if t.status not in DELETABLE and not purge_history:
        raise ValueError(f"status_{t.status}: only {', '.join(DELETABLE)} tenants can be deleted normally. To remove a live tenant too, type DELETE-HISTORY (this erases its credit and money history).")
    counts = {}
    for model, name in ((LedgerEntry, "wallet ledger"), (UsageRecord, "usage"), (AdjustmentRequest, "adjustments"), (ReceivableEntry, "receivables")):
        n = db.scalar(select(func.count()).select_from(model).where(model.tenant_id == t.id)) or 0
        if n:
            counts[name] = n
    if counts and not purge_history:
        raise ValueError("has_history: this tenant has " + ", ".join(f"{v} {k} record(s)" for k, v in counts.items()) +
                         ", which are kept for accounting. Leave it cancelled, or confirm the permanent purge of its money history (test tenants only).")
    audit(db, actor, "tenant.delete", t.id, {"slug": t.slug, "name": t.name, "wisecp_service_id": t.wisecp_service_id, "status": t.status,
                                                         "purged_money_history": counts if purge_history else {}})
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    guarded = (("ledger_entries", "ledger_entries_immutable"), ("receivable_entries", "receivable_entries_immutable"))
    lifted = bool(purge_history and counts and db.get_bind().dialect.name == "postgresql")
    if lifted:   # the ledgers are append-only by database trigger; lift it for THIS transaction only (a rollback restores it)
        for tbl, trg in guarded:
            db.execute(text(f"ALTER TABLE {tbl} DISABLE TRIGGER {trg}"))
    steps = []
    for table in reversed(Base.metadata.sorted_tables):         # children first, but we also retry, so ordering mistakes cannot wedge it
        if table.name in ("audit_log", "tenants"):
            continue
        if "tenant_id" in table.c:
            steps.append(delete(table).where(table.c.tenant_id == t.id))
            continue
        for fk in table.foreign_keys:                            # rows that only reach the tenant through a parent (for example usage reversals)
            parent = fk.column.table
            if parent.name != "tenants" and "tenant_id" in parent.c:
                steps.append(delete(table).where(fk.parent.in_(select(fk.column).where(parent.c.tenant_id == t.id))))
                break
    steps.append(delete(Tenant.__table__).where(Tenant.__table__.c.id == t.id))
    errs: dict[str, str] = {}
    for _ in range(8):
        left = []
        for stmt in steps:
            try:
                with db.begin_nested():
                    db.execute(stmt)
                errs.pop(stmt.table.name, None)
            except DBAPIError as e:
                left.append(stmt)
                errs[stmt.table.name] = str(getattr(e, "orig", e)).splitlines()[0][:200]
        if not left:
            break
        steps = left
    else:
        blockers = "; ".join(f"{k}: {v}" for k, v in errs.items() if k != "tenants") or "; ".join(f"{k}: {v}" for k, v in errs.items())
        raise ValueError("delete_failed: the database refused part of the delete. " + blockers[:420])
    if lifted:
        for tbl, trg in guarded:
            db.execute(text(f"ALTER TABLE {tbl} ENABLE TRIGGER {trg}"))
    db.expunge(t)
    db.flush()
