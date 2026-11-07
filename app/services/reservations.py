"""Holds for sends the hub controls. A hold is taken atomically under the wallet's row lock, so two concurrent sends
can never both be approved against the same money. A send the hub cannot control (a Chatwoot native campaign, say)
cannot be held: its charge simply arrives later and is recorded (see wallet.charge)."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Reservation, now
from . import wallet as wl


def reserve(db: Session, tenant_id: str, reference: str, amount_micro: int, *, snapshot_id: str | None = None,
            funded_floor_micro: int = 0) -> tuple[Reservation | None, bool, str | None]:
    """Returns (reservation, created, refusal_reason). Idempotent on (tenant, reference).
    `funded_floor_micro`: marketing sends must leave at least this much of the tenant's OWN funds (never credit)."""
    w = wl.lock_wallet(db, tenant_id)
    existing = db.scalar(select(Reservation).where(Reservation.tenant_id == tenant_id, Reservation.reference == reference))
    if existing:
        return existing, False, None
    if funded_floor_micro and w.balance_micro - w.reserved_micro - amount_micro < funded_floor_micro:
        return None, False, "below_marketing_minimum"
    if amount_micro > wl.capacity_micro(w):
        return None, False, "credit_limit_reached" if (w.credit_limit_micro or w.receivable_micro) else "insufficient_funds"
    r = Reservation(tenant_id=tenant_id, currency=w.currency, reference=reference, amount_micro=amount_micro, state="held",
                    pricing_snapshot_id=snapshot_id,
                    expires_at=now() + dt.timedelta(hours=get_settings().reservation_ttl_hours))
    db.add(r)
    w.reserved_micro += amount_micro
    db.flush()
    return r, True, None


def close(db: Session, tenant_id: str, reservation_id: str, to_state: str = "released") -> bool | None:
    """Gives the held money back. None = no such reservation for this tenant; False = already closed; True = closed now."""
    r = db.get(Reservation, reservation_id)
    if r is None or r.tenant_id != tenant_id:
        return None
    w = wl.lock_wallet(db, tenant_id)
    db.refresh(r)                                    # re-read under the lock
    if r.state != "held":
        return False
    r.state, r.closed_at = to_state, now()
    w.reserved_micro = max(0, w.reserved_micro - r.amount_micro)
    db.flush()
    return True


def expire_due(db: Session, at: dt.datetime | None = None, limit: int = 500) -> int:
    at = at or now()
    ids = [(r.tenant_id, r.id) for r in db.scalars(select(Reservation).where(Reservation.state == "held", Reservation.expires_at < at)
                                                   .order_by(Reservation.expires_at).limit(limit))]
    n = 0
    for tenant_id, rid in ids:
        if close(db, tenant_id, rid, "expired"):
            n += 1
    return n


def charge_message(db: Session, tenant_id: str, amount_micro: int, key: str, *, send_id: str | None, reference: str,
                   meta: dict, currency: str) -> wl.Posting:
    """Posts the provider's charge. If it belongs to a live hold, the hold is converted in the same transaction;
    if the hold is gone (released, expired, or never existed) the charge is still recorded."""
    w = wl.lock_wallet(db, tenant_id)
    r = None
    if send_id:
        r = db.scalar(select(Reservation).where(Reservation.tenant_id == tenant_id, Reservation.reference == send_id))
        if r is not None:
            db.refresh(r)
    holding = r is not None and r.state == "held"
    p = wl.charge(db, tenant_id, amount_micro, key, reference=reference, meta=meta, currency=currency,
                  release_hold_micro=r.amount_micro if holding else 0)
    if holding and p.created:
        r.state, r.closed_at, r.settled_micro = "settled", now(), amount_micro
    return p
