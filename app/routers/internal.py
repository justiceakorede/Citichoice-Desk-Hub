import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..db import get_db
from ..models import AiUsage, ApiKey, MessageEvent, Reservation, Tenant, TenantChannel, UsageRecord, Wallet, now
from ..schemas import AiReply, ChannelStatusReport, Counts, ReserveBody, SendCheck, UsageEvent
from ..security import bound_tenant_ids, internal_key, resolve_tenant_id
from ..services import ai_allowance, fx, pricing, reservations
from ..services import wallet as wl
from ..services.periods import billing_period
from .admin import channel_out

router = APIRouter(prefix="/internal", dependencies=[Depends(internal_key)])


def _tenant(db: Session, key: ApiKey, supplied: str | None, allow_closed: bool = False) -> Tenant:
    """The credential decides the tenant. A supplied id can only choose among the tenants it is bound to.
    `allow_closed`: charges the provider already made must still be recorded for a closed tenant."""
    t = db.get(Tenant, resolve_tenant_id(db, key, supplied))
    if not t or (t.status == "terminated" and not allow_closed):
        raise HTTPException(404, {"code": "tenant_not_found", "message": "tenant not found"})
    return t


def _price_error(e: pricing.PricingError):
    return HTTPException(422, {"code": e.code, "message": str(e)})


@router.post("/send-check")
def send_check(body: SendCheck, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """Call before every outbound send. Fails safe: no rate card or exchange rate means no billable send."""
    t = _tenant(db, key, body.tenant_id)
    w = db.get(Wallet, t.id)
    if t.status != "active":
        return {"allow": False, "reason": "tenant_suspended"}
    if body.category == "service" and get_settings().service_messages_free:     # free service window: allowed even with an empty wallet
        return {"allow": True, "reason": "service_window"}
    at = now()
    try:
        snap = pricing.price(db, t, w, body.category, body.country, at)
    except pricing.PricingError as e:
        return {"allow": False, "reason": e.code}
    est = snap.unit_charge_micro * body.count
    cap = wl.capacity_micro(w)
    if est > cap:
        return {"allow": False, "reason": "credit_limit_reached" if (w.credit_limit_micro or w.receivable_micro) else "insufficient_funds",
                "needed_micro": est, "capacity_micro": cap, "balance_micro": w.balance_micro, "currency": w.currency,
                "advisory": True}
    if body.category == "marketing":
        floor = _marketing_floor(db, w, at)
        if isinstance(floor, str):
            return {"allow": False, "reason": floor}
        if w.balance_micro - w.reserved_micro - est < floor:
            return {"allow": False, "reason": "below_marketing_minimum", "balance_micro": w.balance_micro,
                    "currency": w.currency, "minimum_micro": floor}
    # Advisory only: nothing is held, so two callers can both be told yes. Use POST /internal/reservations for a race-free approval.
    return {"allow": True, "reason": "ok", "estimated_micro": est, "capacity_micro": cap, "balance_micro": w.balance_micro,
            "currency": w.currency, "advisory": True}


def _marketing_floor(db: Session, w: Wallet, at):
    """Marketing must leave a minimum of the tenant's OWN funds (never credit). Returns micro-units, or a reason string."""
    try:
        fx_rate, _ = fx.rate_between(db, "USD", w.currency, at)
    except fx.FxError as e:
        return e.code
    return money.scale(money.MICRO, money.dmul(get_settings().marketing_min_balance_usd, fx_rate))


@router.post("/reservations")
def reserve_funds(body: ReserveBody, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """Race-free approval for a send the hub controls: the amount is held atomically against funds plus granted credit.
    Repeating the same send_id returns the same hold. Release it if the send never happens; it is settled automatically
    when the delivered-usage event carries the same send_id."""
    t = _tenant(db, key, body.tenant_id)
    if t.status != "active":
        return {"allowed": False, "reason": "tenant_suspended"}
    if body.category == "service" and get_settings().service_messages_free:
        return {"allowed": True, "reservation_id": None, "amount_micro": 0, "reason": "service_window"}
    w = db.get(Wallet, t.id)
    at = now()
    try:
        snap = pricing.price(db, t, w, body.category, body.country, at, persist=True)
    except pricing.PricingError as e:
        return {"allowed": False, "reason": e.code}
    floor = 0
    if body.category == "marketing":
        floor = _marketing_floor(db, w, at)
        if isinstance(floor, str):
            return {"allowed": False, "reason": floor}
    amount = snap.unit_charge_micro * body.count
    r, created, why = reservations.reserve(db, t.id, body.send_id, amount, snapshot_id=snap.id, funded_floor_micro=floor)
    db.commit()
    if r is None:
        return {"allowed": False, "reason": why, "needed_micro": amount, "currency": w.currency}
    return {"allowed": True, "reservation_id": r.id, "state": r.state, "created": created, "amount_micro": r.amount_micro,
            "currency": r.currency, "expires_at": r.expires_at.isoformat()}


@router.post("/reservations/{rid}/release")
def release_reservation(rid: str, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """The send did not happen (or failed before the provider accepted it): give the held amount back. Safe to repeat."""
    from fastapi import HTTPException as _H
    r = db.get(Reservation, rid)
    if r is None or r.tenant_id not in bound_tenant_ids(db, key):
        raise _H(404, {"code": "reservation_not_found", "message": "reservation not found"})
    changed = reservations.close(db, r.tenant_id, rid, "released")
    db.commit()
    return {"released": bool(changed), "state": db.get(Reservation, rid).state}


@router.post("/usage")
def ingest_usage(ev: UsageEvent, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """Record one delivered message and debit the wallet. Safe to call twice for the same external_id."""
    t = _tenant(db, key, ev.tenant_id, allow_closed=True)
    w = db.get(Wallet, t.id)
    when = now()
    if ev.occurred_at:
        try:
            when = dt.datetime.fromisoformat(ev.occurred_at.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            raise HTTPException(422, "bad occurred_at")

    # 1) Record the lifecycle event. Identity is (provider, message id, status); a message id belongs to ONE tenant.
    seen = db.scalar(select(MessageEvent).where(MessageEvent.provider == ev.provider, MessageEvent.external_id == ev.external_id,
                                                MessageEvent.status == ev.status))
    other = seen or db.scalar(select(MessageEvent).where(MessageEvent.provider == ev.provider, MessageEvent.external_id == ev.external_id).limit(1))
    if other is not None and other.tenant_id != t.id:
        raise HTTPException(409, {"code": "event_conflict", "message": "message id already recorded for another tenant"})
    duplicate = seen is not None
    if not duplicate:
        try:
            with db.begin_nested():
                db.add(MessageEvent(tenant_id=t.id, provider=ev.provider, external_id=ev.external_id, status=ev.status, occurred_at=when))
                db.flush()
        except IntegrityError:
            duplicate = True                                              # a concurrent copy of this event won the race

    # 2) Only the billable status touches money. Anything else is just remembered.
    if ev.status != get_settings().billable_status:
        db.commit()
        return {"recorded": not duplicate, "billed": False, "status": ev.status, "balance_micro": w.balance_micro}

    # A repeated billable event is harmless: usage is unique on (provider, message id), so it can be charged only once.
    existing = db.scalar(select(UsageRecord).where(UsageRecord.provider == ev.provider, UsageRecord.external_id == ev.external_id))
    if existing:
        if existing.tenant_id != t.id:
            raise HTTPException(409, {"code": "event_conflict", "message": "message id already recorded for another tenant"})
        return {"recorded": False, "billed": existing.charge_micro > 0, "charge_micro": existing.charge_micro, "balance_micro": w.balance_micro}

    billable = ev.billed and (ev.category != "service" or not get_settings().service_messages_free)
    snap = None
    cost = fee = buf = charge = 0
    if billable:
        try:
            snap = pricing.price(db, t, w, ev.category, ev.country, when, persist=True)
        except pricing.PricingError as e:
            raise _price_error(e)
        cost, buf, fee, charge = snap.unit_cost_micro, snap.buffer_micro, snap.fee_micro, snap.unit_charge_micro

    try:
        with db.begin_nested():
            rec = UsageRecord(tenant_id=t.id, provider=ev.provider, external_id=ev.external_id, channel=ev.channel, category=ev.category,
                              country=(ev.country or "").upper() or None, billed=billable, customer_currency=w.currency,
                              provider_currency=snap.provider_currency if snap else get_settings().provider_currency,
                              pricing_snapshot_id=snap.id if snap else None, provider_cost_micro=cost, fx_buffer_micro=buf,
                              service_fee_micro=fee, charge_micro=charge, created_at=when)
            db.add(rec)
            db.flush()
    except IntegrityError:
        db.rollback()
        return {"recorded": False}
    posting = None
    if charge:
        # The provider has already made this charge, so it is ALWAYS recorded: funds pay what they can and the rest
        # becomes a receivable. Whether new sends are allowed is decided separately, by the credit limit.
        posting = reservations.charge_message(db, t.id, charge, f"usage:{ev.provider}:{ev.external_id}", send_id=ev.send_id,
                                              reference=ev.external_id, currency=w.currency,
                                              meta={"category": ev.category, "country": ev.country, "pricing_snapshot_id": snap.id})
    db.commit()
    w = db.get(Wallet, t.id)
    return {"recorded": True, "billed": bool(charge), "currency": w.currency, "provider_cost_micro": cost, "fx_buffer_micro": buf,
            "service_fee_micro": fee, "charge_micro": charge, "paid_from_funds_micro": posting.paid_micro if posting else 0,
            "added_to_receivable_micro": posting.accrued_micro if posting else 0,
            "pricing_snapshot_id": snap.id if snap else None, "balance_micro": w.balance_micro, "receivable_micro": w.receivable_micro}


@router.post("/ai-reply")
def ai_reply(body: AiReply, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """Claim the allowance for ONE logical AI reply, before sending it. Repeating the same `key` counts once, so a retried
    n8n workflow cannot double-count. If generation fails before anything is sent, release the claim."""
    t = _tenant(db, key, body.tenant_id)
    if t.status != "active":
        return {"allowed": False, "reason": "tenant_suspended"}
    out = ai_allowance.claim(db, t, body.key, body.count)
    db.commit()
    return out


@router.post("/ai-reply/{ckey}/release")
def ai_reply_release(ckey: str, tenant_id: str | None = None, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    t = _tenant(db, key, tenant_id)
    changed = ai_allowance.release(db, t.id, ckey)
    if changed is None:
        raise HTTPException(404, {"code": "unknown_key", "message": "no such AI reply key"})
    db.commit()
    return {"released": bool(changed)}


@router.post("/tenants/{tid}/counts")
def counts(tid: str, body: Counts, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    """n8n reports seats in use so the WISECP card can show used/total. Channel usage is counted by the hub."""
    t = _tenant(db, key, tid)
    if body.seats_used is not None:
        t.seats_used = body.seats_used
    db.commit()
    return {"ok": True}


@router.get("/channels")
def list_channels(tenant_id: str | None = None, key: ApiKey = Depends(internal_key), db: Session = Depends(get_db)):
    t = _tenant(db, key, tenant_id)
    return [channel_out(c) for c in db.scalars(select(TenantChannel).where(
        TenantChannel.tenant_id == t.id, TenantChannel.status != "removed"))]


@router.patch("/channels/{cid}")
def report_channel_status(cid: str, body: ChannelStatusReport, key: ApiKey = Depends(internal_key),
                          db: Session = Depends(get_db)):
    c = db.get(TenantChannel, cid)
    if not c or c.status == "removed" or c.tenant_id not in bound_tenant_ids(db, key):
        raise HTTPException(404, {"code": "channel_not_found", "message": "channel not found"})
    c.status = body.status
    if body.verification_status is not None:
        c.verification_status = body.verification_status
    db.commit()
    return channel_out(c)
