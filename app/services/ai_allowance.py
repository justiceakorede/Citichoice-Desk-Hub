"""The monthly AI-reply allowance. What counts: ONE AI-generated message that was (or is about to be) sent to a customer.
A model call, a tool call, a draft shown only to an agent, or a generation that failed before anything was sent are
not replies. The caller claims the allowance with a stable key before sending and RELEASES it if generation fails.
Everything is serialised on the tenant's usage row, so concurrent workflows cannot exceed the limit."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AiConsumption, AiUsage, Tenant, now
from .periods import billing_period


def _usage_row(db: Session, tenant_id: str, period: str) -> AiUsage:
    if not db.get(AiUsage, (tenant_id, period)):
        try:
            with db.begin_nested():
                db.add(AiUsage(tenant_id=tenant_id, period=period, replies=0))
                db.flush()
        except IntegrityError:
            pass
    return db.execute(select(AiUsage).where(AiUsage.tenant_id == tenant_id, AiUsage.period == period)
                      .with_for_update().execution_options(populate_existing=True)).scalar_one()


def claim(db: Session, t: Tenant, key: str, units: int) -> dict:
    period = billing_period()
    row = _usage_row(db, t.id, period)                     # the lock: everything for this tenant and month serialises here
    limit = t.ai_limit
    ex = db.scalar(select(AiConsumption).where(AiConsumption.tenant_id == t.id, AiConsumption.consumption_key == key))
    if ex is not None and ex.state == "claimed":
        used = db.get(AiUsage, (t.id, ex.period)).replies
        return {"allowed": True, "replayed": True, "reason": "already_claimed", "used": used, "limit": limit, "period": ex.period}
    if row.replies + units > limit:
        return {"allowed": False, "replayed": False, "reason": "allowance_reached", "used": row.replies, "limit": limit, "period": period}
    row.replies += units
    if ex is None:
        db.add(AiConsumption(tenant_id=t.id, period=period, consumption_key=key, kind="reply", units=units, state="claimed"))
    else:                                                  # a released key being tried again counts again, once
        ex.state, ex.closed_at, ex.period, ex.units = "claimed", None, period, units
    db.flush()
    return {"allowed": True, "replayed": False, "reason": "ok", "used": row.replies, "limit": limit, "period": period}


def release(db: Session, tenant_id: str, key: str) -> bool | None:
    """Generation failed before anything was sent: give the allowance back. None = unknown key; False = nothing to do."""
    ex = db.scalar(select(AiConsumption).where(AiConsumption.tenant_id == tenant_id, AiConsumption.consumption_key == key))
    if ex is None:
        return None
    row = _usage_row(db, tenant_id, ex.period)
    db.refresh(ex)
    if ex.state != "claimed" or ex.kind != "reply":
        return False
    ex.state, ex.closed_at = "released", now()
    row.replies = max(0, row.replies - ex.units)
    db.flush()
    return True


def adjust(db: Session, tenant_id: str, delta: int, key: str, reason: str) -> tuple[AiConsumption, bool, int]:
    """Staff correction in the CURRENT month. Repeating the key changes nothing. The month's count cannot go below 0."""
    period = billing_period()
    row = _usage_row(db, tenant_id, period)
    ex = db.scalar(select(AiConsumption).where(AiConsumption.tenant_id == tenant_id, AiConsumption.consumption_key == key))
    if ex is not None:
        return ex, False, row.replies
    if row.replies + delta < 0:
        raise ValueError("the month's count cannot go below zero")
    row.replies += delta
    c = AiConsumption(tenant_id=tenant_id, period=period, consumption_key=key, kind="adjustment", units=delta, state="claimed", reason=reason)
    db.add(c)
    db.flush()
    return c, True, row.replies
