"""What a customer sees: conversations, messages (total and per channel) and WhatsApp wallet figures.

Two sources, kept separate on purpose:
  * Chatwoot reports: conversations and messages, for every channel (the hub does not see inbound traffic itself).
  * The hub's own usage records: WhatsApp messages Meta delivered and billed, and what they cost the wallet. These are exact.
A failing Chatwoot call degrades that figure to `available: false` with a reason; it never fails the whole response.
"""
import datetime as dt
import time

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import Tenant, TenantChannel, UsageRecord, now
from . import tenants as ts
from .chatwoot_reports import ChatwootReports, ReportError
from .periods import period_start_utc

_token_cache: dict[str, tuple[float, str]] = {}
RANGES = ("month", "7d", "30d", "90d")
_cache: dict[tuple, tuple[float, dict]] = {}          # per process; the hub runs one or a few workers, so this only trims load


def range_bounds(name: str, at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    at = at or now()
    if name == "month":
        return period_start_utc(at), at
    days = {"7d": 7, "30d": 30, "90d": 90}[name]
    return at - dt.timedelta(days=days), at


def _descriptors(db: Session, t: Tenant, rep: ChatwootReports) -> list[dict]:
    """Channels to show: the ones staff registered, plus any Chatwoot inbox nobody registered (found live), so a customer sees
    every inbox they actually have. Discovery needs the reporter token; without it only registered channels appear."""
    reg = list(db.scalars(select(TenantChannel).where(TenantChannel.tenant_id == t.id, TenantChannel.status != "removed")
                          .order_by(TenantChannel.created_at)))
    out = [{"id": c.id, "inbox_id": c.chatwoot_inbox_id, "type": c.channel_type, "name": c.display_name, "phone_number": c.phone_number,
            "status": c.status} for c in reg]
    known = {c.chatwoot_inbox_id for c in reg if c.chatwoot_inbox_id}
    if t.chatwoot_account_id and rep.configured:
        key = ("inboxes", t.id, t.chatwoot_account_id)
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            found = hit[1]["found"]
        else:
            try:
                found = rep.inboxes(t.chatwoot_account_id)
                _cache[key] = (time.monotonic() + get_settings().stats_cache_seconds, {"found": found})
            except ReportError:
                found = []
        out += [{"id": f"inbox-{i['id']}", "inbox_id": i["id"], "type": i["type"], "name": i["name"], "phone_number": i["phone_number"],
                 "status": "connected"} for i in found if i["id"] not in known]
    return out


def _chatwoot_part(t: Tenant, chans: list[dict], since, until, rep: ChatwootReports, key: tuple) -> dict:
    ttl = get_settings().stats_cache_seconds
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    out: dict = {"account": {"available": False, "reason": "no_workspace"}, "channels": {}}
    if t.chatwoot_account_id:
        try:
            out["account"] = {"available": True, **rep.summary(t.chatwoot_account_id, since, until)}
        except ReportError as e:
            out["account"] = {"available": False, "reason": str(e)}
        for c in chans:
            if not c["inbox_id"]:
                out["channels"][c["id"]] = {"available": False, "reason": "no_inbox"}
                continue
            try:
                out["channels"][c["id"]] = {"available": True, **rep.summary(t.chatwoot_account_id, since, until, c["inbox_id"])}
            except ReportError as e:
                out["channels"][c["id"]] = {"available": False, "reason": str(e)}
    if out["account"].get("available") and ttl > 0:       # never cache a failure
        _cache[key] = (time.monotonic() + ttl, out)
    return out


def _whatsapp_billing(db: Session, t: Tenant, since, until, currency: str) -> dict:
    rows = db.execute(
        select(UsageRecord.category, func.count(), func.coalesce(func.sum(UsageRecord.charge_micro), 0))
        .where(UsageRecord.tenant_id == t.id, UsageRecord.channel == "whatsapp",
               UsageRecord.created_at >= since, UsageRecord.created_at <= until)
        .group_by(UsageRecord.category)).all()
    by_cat = {c: {"delivered": int(n), "charged_micro": int(ch), "charged": money.format_micro(int(ch), currency)} for c, n, ch in rows}
    total_ch = sum(v["charged_micro"] for v in by_cat.values())
    return {"delivered_messages": sum(v["delivered"] for v in by_cat.values()), "charged_micro": total_ch,
            "charged": money.format_micro(total_ch, currency), "by_category": by_cat}


def tenant_stats(db: Session, t: Tenant, ranges: list[str], rep: ChatwootReports) -> dict:
    chans = _descriptors(db, t, rep)
    cv = ts.customer_view(db, t)
    cur = cv["wallet"]["currency"]
    w = cv["wallet"]
    # Customer-safe wallet: formatted figures only. No credit limit, holds or raw micro-units leave the hub here.
    wallet = {"currency": cur, "balance": w["balance"], "available": money.format_micro(w["available_micro"], cur),
              "receivable": w["receivable"], "owes": w["receivable_micro"] > 0,
              "spend_30d": money.format_micro(w["spend_30d_micro"], cur), "alert": w["alert"]}
    out_ranges = {}
    for name in ranges:
        since, until = range_bounds(name)
        cw = _chatwoot_part(t, chans, since, until, rep, (t.id, t.chatwoot_account_id, name, tuple(c['id'] for c in chans)))
        per_channel = []
        for c in chans:
            m = cw["channels"].get(c["id"], {"available": False, "reason": "no_workspace"})
            per_channel.append({"id": c["id"], "type": c["type"], "name": c["name"], "phone_number": c["phone_number"], "status": c["status"], **m})
        out_ranges[name] = {"since": since.isoformat(), "until": until.isoformat(), "totals": cw["account"], "channels": per_channel,
                            "whatsapp": _whatsapp_billing(db, t, since, until, cur)}
    return {"tenant_id": t.id, "generated_at": now().isoformat(), "currency": cur, "wallet": wallet,
            "message_prices": message_prices(db, t), "ai_replies": cv["ai_replies"], "seats": cv["seats"], "channels_plan": cv["channels"], "status": t.status,
            "ranges": out_ranges}


def reports_for(t: Tenant, base: ChatwootReports, cw) -> ChatwootReports:
    """Reports are read with CHATWOOT_REPORT_TOKEN when set; otherwise as the tenant's own owner user, whose token the Platform
    App can fetch. That needs no extra user in the customer's workspace, so no extra seat and nothing the customer can remove."""
    if getattr(base, "token", "") or not t.chatwoot_user_id or cw is None:
        return base
    hit = _token_cache.get(t.id)
    if hit and hit[0] > time.monotonic():
        tok = hit[1]
    else:
        try:
            tok = cw.user_token(t.chatwoot_user_id)
        except Exception:                                  # ProvisioningError or transport: stats degrade to "not available"
            return base
        if tok:
            _token_cache[t.id] = (time.monotonic() + 600, tok)
    return base.with_token(tok) if tok else base


def _unit(micro: int, cur: str) -> str:
    """A per-message price needs more decimals than a wallet balance does."""
    v = (Decimal(micro) / Decimal(1_000_000)).quantize(Decimal("0.0001"))
    txt = f"{v:f}".rstrip("0")
    if len(txt.split(".")[1]) < 2:
        txt += "0" * (2 - len(txt.split(".")[1]))
    return f"{cur} {txt}"


def message_prices(db: Session, t: Tenant) -> dict:
    """What one WhatsApp message of each category costs this customer, from the hub's own rate cards, FX and their markup."""
    from . import pricing, rates
    from . import wallet as wl
    w = wl.get_wallet(db, t.id)
    out = {}
    for cat in rates.CATEGORIES:
        try:
            snap = pricing.price(db, t, w, cat, None, now())
        except pricing.PricingError:
            continue
        out[cat] = {"per_message": _unit(snap.unit_charge_micro, w.currency), "free": snap.unit_charge_micro == 0}
    return out
