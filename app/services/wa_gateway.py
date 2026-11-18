"""Real-time WhatsApp credit enforcement at the one place every send passes: the call from Chatwoot to Meta.

Chatwoot is pointed at the hub with WHATSAPP_CLOUD_BASE_URL. Before a message is forwarded the hub takes an atomic hold on the
tenant's wallet; with no capacity the hub answers with a Meta-shaped error and Meta is never contacted (so nothing is billed by
Meta and Chatwoot shows the message as failed). When Meta accepts the message the hold becomes the charge, immediately, so the
balance is real-time for agents, the bot and campaigns alike.

Templates are charged at their real Meta category (looked up and cached). If that cannot be looked up, the send is still gated on the
dearest rate but is not charged here; the daily true-up charges it, so it is never charged twice.

Fail-safe rules: no rate card / FX rate => blocked. Meta rejects or times out => hold released (the daily true-up charges
anything Meta did bill). Settlement problems never hide Meta's answer from Chatwoot; the true-up covers them.
"""
import logging
import re
import time
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Tenant, TenantChannel, UsageRecord, Wallet, now
from . import pricing, reservations

log = logging.getLogger("hub.wa_gateway")
SEND_PATH = re.compile(r"^v\d+\.\d+/(\d+)/messages$")

# Longest-prefix match on the recipient's dial code (extend as needed). Unknown => None => the default "*" market rate.
_DIAL = {"234": "NG", "233": "GH", "254": "KE", "27": "ZA", "255": "TZ", "256": "UG", "44": "GB", "91": "IN", "971": "AE", "1": "US",
         "61": "AU", "49": "DE", "33": "FR", "39": "IT", "34": "ES", "55": "BR", "52": "MX", "20": "EG", "212": "MA", "237": "CM",
         "225": "CI", "221": "SN", "250": "RW", "260": "ZM", "263": "ZW", "86": "CN", "81": "JP", "65": "SG", "60": "MY", "62": "ID",
         "92": "PK", "880": "BD", "966": "SA", "90": "TR", "7": "RU", "31": "NL", "46": "SE", "47": "NO", "45": "DK", "351": "PT",
         "353": "IE", "41": "CH", "43": "AT", "32": "BE"}


def country_of(to: str | None) -> str | None:
    d = re.sub(r"\D", "", str(to or ""))
    for n in (3, 2, 1):
        if d[:n] in _DIAL:
            return _DIAL[d[:n]]
    return None


def is_message_send(body) -> bool:
    """Only real message sends cost money. Read receipts and typing indicators (`status`) pass through free."""
    return isinstance(body, dict) and body.get("messaging_product") == "whatsapp" and "status" not in body and "type" in body


_CAT = {"MARKETING": "marketing", "MARKETING_LITE": "marketing", "UTILITY": "utility", "AUTHENTICATION": "authentication"}
_TPL_CACHE: dict[tuple, tuple[float, str]] = {}
_TPL_TTL = 3600.0


def template_category(waba_id: str | None, token: str | None, body: dict) -> str | None:
    """Meta's own category for the template being sent (marketing/utility/authentication), cached for an hour. None = could not be
    resolved; the caller then gates on the dearest rate but leaves the charge to the daily Meta true-up (so it can never be charged twice)."""
    tpl = body.get("template") or {}
    name, lang = tpl.get("name"), (tpl.get("language") or {}).get("code")
    if not (waba_id and token and name):
        return None
    key = (waba_id, name, lang)
    hit = _TPL_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    s = get_settings()
    try:
        r = httpx.get(f"{s.wa_gateway_upstream.rstrip('/')}/{s.meta_graph_version}/{waba_id}/message_templates",
                      params={"name": name, "fields": "name,language,category", "limit": 50},
                      headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
        rows = r.json().get("data") or [] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    row = next((x for x in rows if x.get("language") == lang), None) or (rows[0] if len(rows) == 1 else None)
    cat = _CAT.get(str((row or {}).get("category", "")).upper())
    if cat:
        _TPL_CACHE[key] = (time.time() + _TPL_TTL, cat)
    return cat


@dataclass
class Decision:
    allow: bool
    reason: str = "ok"
    tenant_id: str | None = None
    reservation_ref: str | None = None
    category: str = "service"
    country: str | None = None
    message: str = ""
    charge_now: bool = True       # False: category unknown at send time, so the hold is only a gate and the true-up does the charging


REASON_TEXT = {
    "insufficient_funds": "WhatsApp credit is empty. Top up to resume sending.",
    "credit_limit_reached": "WhatsApp credit limit reached. Top up to resume sending.",
    "tenant_suspended": "This workspace is suspended, so WhatsApp sending is paused.",
    "unmapped": "This WhatsApp number is not registered with billing.",
}


def authorize(db: Session, phone_number_id: str, body: dict, token: str | None = None) -> Decision:
    s = get_settings()
    ch = db.scalar(select(TenantChannel).where(TenantChannel.channel_type == "whatsapp", TenantChannel.external_id == phone_number_id,
                                               TenantChannel.status != "removed"))
    if ch is None:
        if s.wa_gateway_unmapped == "block":
            return Decision(False, "unmapped", message=REASON_TEXT["unmapped"])
        log.warning("wa gateway: send from an unmapped number, forwarded untouched")
        return Decision(True, "unmapped_allowed")
    t = db.get(Tenant, ch.tenant_id)
    if t is None or t.status != "active":
        return Decision(False, "tenant_suspended", ch.tenant_id, message=REASON_TEXT["tenant_suspended"])
    charge_now = True
    if body.get("type") == "template":
        cat = template_category(ch.external_account_id, token, body)
        if cat is None:
            cat, charge_now = s.wa_gateway_template_category, False
    else:
        cat = "service"
    country = country_of(body.get("to") or (body.get("recipient") if isinstance(body.get("recipient"), str) else None))
    if cat == "service" and s.service_messages_free:
        return Decision(True, "service_window", t.id, None, cat, country)
    w = db.get(Wallet, t.id)
    try:
        snap = pricing.price(db, t, w, cat, country, now(), persist=True)
    except pricing.PricingError as e:
        db.commit()
        return Decision(False, e.code, t.id, message="WhatsApp pricing is being updated. Please retry shortly.")
    ref = "gw:" + uuid.uuid4().hex
    r, _, why = reservations.reserve(db, t.id, ref, snap.unit_charge_micro, snapshot_id=snap.id)
    db.commit()                                   # the hold is visible at once; the lock is not kept during the network call
    if r is None:
        return Decision(False, why or "insufficient_funds", t.id, category=cat, country=country,
                        message=REASON_TEXT.get(why or "", REASON_TEXT["insufficient_funds"]))
    return Decision(True, "ok", t.id, ref, cat, country, charge_now=charge_now)


def release(db: Session, d: Decision) -> None:
    if d.tenant_id and d.reservation_ref:
        from ..models import Reservation
        r = db.scalar(select(Reservation).where(Reservation.tenant_id == d.tenant_id, Reservation.reference == d.reservation_ref))
        if r is not None:
            reservations.close(db, d.tenant_id, r.id, "released")
            db.commit()


def settle(db: Session, d: Decision, wamid: str) -> None:
    """Meta accepted the message: turn the hold into the charge, once (usage is unique on provider + message id)."""
    if not (d.tenant_id and d.reservation_ref):
        return
    if not d.charge_now:                           # unknown template category: Meta's daily figure charges it at the right rate
        release(db, d)
        return
    t = db.get(Tenant, d.tenant_id)
    w = db.get(Wallet, t.id)
    snap = pricing.price(db, t, w, d.category, d.country, now(), persist=True)
    if db.scalar(select(UsageRecord.id).where(UsageRecord.provider == "meta_whatsapp", UsageRecord.external_id == wamid)):
        release(db, d)
        return
    try:
        with db.begin_nested():
            db.add(UsageRecord(tenant_id=t.id, provider="meta_whatsapp", external_id=wamid, channel="whatsapp", category=d.category,
                               country=d.country, billed=True, customer_currency=w.currency, provider_currency=snap.provider_currency,
                               pricing_snapshot_id=snap.id, provider_cost_micro=snap.unit_cost_micro, fx_buffer_micro=snap.buffer_micro,
                               service_fee_micro=snap.fee_micro, charge_micro=snap.unit_charge_micro))
            db.flush()
    except IntegrityError:
        db.rollback()
        release(db, d)
        return
    reservations.charge_message(db, t.id, snap.unit_charge_micro, f"usage:meta_whatsapp:{wamid}", send_id=d.reservation_ref,
                                reference=wamid, currency=w.currency,
                                meta={"category": d.category, "country": d.country, "pricing_snapshot_id": snap.id, "via": "wa_gateway"})
    db.commit()
