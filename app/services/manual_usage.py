"""Staff correction of WhatsApp usage: "these N messages were sent and not billed" (or "N were billed by mistake").

Prices the messages from the same rate cards, FX and markup as automatic billing, then:
  N > 0: debits the wallet (funds first, the rest becomes a receivable) and adds N messages and their cost to the customer's totals
  N < 0: gives the cost back to the wallet and removes N messages and their cost from the totals
Automatic billing is untouched and keeps adding on top. Everything is audited with the staff member's name and reason.
"""
from sqlalchemy.orm import Session

from .. import money
from ..models import Tenant, UsageRecord, now, uid
from . import pricing, rates, reservations
from . import tenants as ts
from . import wallet as wl


def record(db: Session, t: Tenant, actor: str, messages: int, category: str, country: str | None, reason: str) -> dict:
    if messages == 0 or abs(messages) > 100_000:
        raise ValueError("messages must be between -100,000 and 100,000 and not zero")
    if category not in rates.CATEGORIES:
        raise ValueError("unknown category")
    if not reason.strip():
        raise ValueError("a reason is required")
    country = (country or "").strip().upper() or None
    if country and (len(country) != 2 or not country.isalpha()):
        raise ValueError("country must be a 2-letter code such as NG, or blank for the default")
    w = wl.get_wallet(db, t.id)
    try:
        snap = pricing.price(db, t, w, category, country, now(), persist=True)
    except pricing.PricingError as e:
        raise ValueError(f"no price available ({e}). Check rate cards and run fx-sync.")
    n, sign = abs(messages), (1 if messages > 0 else -1)
    key = f"manual:{uid()}"
    charge = snap.unit_charge_micro * n
    db.add(UsageRecord(tenant_id=t.id, provider="manual", external_id=key, channel="whatsapp", category=category, country=country, billed=charge > 0,
                       quantity=sign * n, customer_currency=w.currency, provider_currency=snap.provider_currency, pricing_snapshot_id=snap.id,
                       provider_cost_micro=sign * snap.unit_cost_micro * n, fx_buffer_micro=sign * snap.buffer_micro * n,
                       service_fee_micro=sign * snap.fee_micro * n, charge_micro=sign * charge, created_at=now()))
    db.flush()
    meta = {"manual": True, "messages": n, "category": category, "country": country, "reason": reason.strip()[:200], "by": actor}
    if charge and sign > 0:
        reservations.charge_message(db, t.id, charge, f"usage:manual:{key}", send_id=None, reference=f"manual usage {n} x {category}", currency=w.currency, meta=meta)
    elif charge and sign < 0:
        wl.credit(db, t.id, charge, f"usage:manual:{key}", kind="adjustment", reference=f"manual usage reversal {n} x {category}", currency=w.currency, meta=meta)
    ts.audit(db, actor, "usage.manual", t.id, {"messages": messages, "category": category, "country": country, "amount": money.format_micro(sign * charge, w.currency),
                                               "reason": reason.strip()[:200]})
    return {"messages": messages, "amount": money.format_micro(sign * charge, w.currency)}
