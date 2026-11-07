"""Maker-checker for manual money changes. One person proposes with a reason and evidence; a different person approves
or rejects; approval posts the entry in the same transaction. Nobody edits a balance directly, and a machine API key
cannot do any of this."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import ADJUSTMENT_KINDS, AdjustmentRequest, Tenant, UsageRecord, UsageReversal, Wallet, now
from . import fx
from . import tenants as ts
from . import wallet as wl


class AdjustmentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def propose(db: Session, tenant: Tenant, *, kind: str, amount, usage_external_id: str | None, reason: str,
            evidence_ref: str, actor: str) -> AdjustmentRequest:
    if kind not in ADJUSTMENT_KINDS:
        raise AdjustmentError("bad_kind", f"kind must be one of {ADJUSTMENT_KINDS}")
    reason, evidence_ref = (reason or "").strip(), (evidence_ref or "").strip()
    if len(reason) < 5 or len(evidence_ref) < 3:
        raise AdjustmentError("reason_required", "a reason (5+ characters) and an evidence reference (ticket, invoice or email id) are required")
    w = db.get(Wallet, tenant.id)
    usage_id = None
    if kind == "usage_reversal":
        u = db.scalar(select(UsageRecord).where(UsageRecord.tenant_id == tenant.id, UsageRecord.external_id == (usage_external_id or "")))
        if u is None or u.charge_micro <= 0:
            raise AdjustmentError("no_such_charge", "no charged message with that id for this tenant")
        if db.get(UsageReversal, u.id) or db.scalar(select(AdjustmentRequest).where(
                AdjustmentRequest.target_usage_id == u.id, AdjustmentRequest.status == "pending")):
            raise AdjustmentError("already_reversed", "that charge has already been reversed or has a reversal waiting for approval")
        micro, usage_id = u.charge_micro, u.id
    else:
        try:
            micro = money.input_to_micro(amount, w.currency)
        except (money.MoneyError, TypeError, ArithmeticError) as e:
            raise AdjustmentError("bad_amount", str(e))
        if micro <= 0:
            raise AdjustmentError("bad_amount", "amount must be positive; the kind decides the direction")
    r = AdjustmentRequest(tenant_id=tenant.id, currency=w.currency, kind=kind, amount_micro=micro, target_usage_id=usage_id,
                          reason=reason[:300], evidence_ref=evidence_ref[:200], status="pending", requested_by=actor)
    db.add(r)
    db.flush()
    ts.audit(db, actor, "adjustment.request", tenant.id, {"id": r.id, "kind": kind, "amount_micro": micro, "currency": w.currency,
                                                          "reason": r.reason, "evidence": r.evidence_ref})
    return r


def self_approval_limit_micro(db: Session, currency: str) -> int:
    limit = get_settings().adjustment_self_approval_limit_usd
    if limit <= 0:
        return 0
    try:
        rate, _ = fx.rate_between(db, "USD", currency, now())
    except fx.FxError:
        return 0                                   # no trustworthy rate: no self-approval
    return money.scale(money.MICRO, money.dmul(limit, rate))


def _execute(db: Session, r: AdjustmentRequest) -> None:
    key, ref = f"adj:{r.id}", f"{r.reason} [{r.evidence_ref}]"
    try:
        if r.kind == "wallet_credit":
            wl.credit(db, r.tenant_id, r.amount_micro, key, kind="adjustment", reference=ref, currency=r.currency)
        elif r.kind == "wallet_debit":
            wl.debit(db, r.tenant_id, r.amount_micro, key, kind="adjustment", reference=ref, currency=r.currency)
        elif r.kind == "manual_charge":
            wl.charge(db, r.tenant_id, r.amount_micro, key, reference=ref, currency=r.currency)
        elif r.kind == "receivable_writeoff":
            wl.writeoff(db, r.tenant_id, r.amount_micro, key, reference=ref, currency=r.currency)
        elif r.kind == "usage_reversal":
            if db.get(UsageReversal, r.target_usage_id):
                raise AdjustmentError("already_reversed", "that charge was already reversed")
            wl.credit(db, r.tenant_id, r.amount_micro, key, kind="adjustment", reference=f"reversal of a usage charge: {ref}", currency=r.currency)
            db.add(UsageReversal(usage_id=r.target_usage_id, request_id=r.id, amount_micro=r.amount_micro))
    except wl.InsufficientFunds:
        raise AdjustmentError("insufficient_funds", "a debit can only take funds the tenant has (and not those held for sends)")
    except wl.NothingToWriteOff:
        raise AdjustmentError("exceeds_debt", "that is more than the tenant currently owes")
    except wl.CurrencyMismatch as e:
        raise AdjustmentError("currency_mismatch", str(e))


def decide(db: Session, request_id: str, actor: str, approve: bool, note: str | None = None) -> AdjustmentRequest:
    r = db.execute(select(AdjustmentRequest).where(AdjustmentRequest.id == request_id).with_for_update()
                   .execution_options(populate_existing=True)).scalar_one_or_none()
    if r is None:
        raise AdjustmentError("not_found", "no such request")
    if r.status != "pending":
        raise AdjustmentError("not_pending", f"this request is already {r.status}")
    if approve:
        own = actor == r.requested_by
        if own and r.amount_micro > self_approval_limit_micro(db, r.currency):
            raise AdjustmentError("needs_second_approver", "a different person must approve this request")
        _execute(db, r)
        r.status, r.self_approved = "posted", own
    else:
        r.status = "rejected"
    r.decided_by, r.decided_at, r.decision_note = actor, now(), (note or "")[:300] or None
    ts.audit(db, actor, "adjustment.approve" if approve else "adjustment.reject", r.tenant_id,
             {"id": r.id, "kind": r.kind, "amount_micro": r.amount_micro, "currency": r.currency, "requested_by": r.requested_by,
              "self_approved": r.self_approved, "note": r.decision_note})
    db.flush()
    return r
