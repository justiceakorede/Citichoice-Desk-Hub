"""What a customer sees: conversations, messages (total and per channel) and WhatsApp wallet figures.

Two sources, kept separate on purpose:
  * Chatwoot reports: conversations and messages, for every channel (the hub does not see inbound traffic itself).
  * The hub's own usage records: WhatsApp messages Meta delivered and billed, and what they cost the wallet. These are exact.
A failing Chatwoot call degrades that figure to `available: false` with a reason; it never fails the whole response.
"""
import datetime as dt
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import Tenant, TenantChannel, UsageRecord, now
from . import tenants as ts
from .chatwoot_reports import ChatwootReports, ReportError
from .periods import period_start_utc

RANGES = ("month", "7d", "30d", "90d")
_cache: dict[tuple, tuple[float, dict]] = {}          # per process; the hub runs one or a few workers, so this only trims load


def range_bounds(name: str, at: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    at = at or now()
    if name == "month":
        return period_start_utc(at), at
    days = {"7d": 7, "30d": 30, "90d": 90}[name]
    return at - dt.timedelta(days=days), at


def _chatwoot_part(t: Tenant, channels: list[TenantChannel], since, until, rep: ChatwootReports, key: tuple) -> dict:
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
        for c in channels:
            if not c.chatwoot_inbox_id:
                out["channels"][c.id] = {"available": False, "reason": "no_inbox"}
                continue
            try:
                out["channels"][c.id] = {"available": True, **rep.summary(t.chatwoot_account_id, since, until, c.chatwoot_inbox_id)}
            except ReportError as e:
                out["channels"][c.id] = {"available": False, "reason": str(e)}
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
    channels = list(db.scalars(select(TenantChannel).where(TenantChannel.tenant_id == t.id, TenantChannel.status != "removed")
                               .order_by(TenantChannel.created_at)))
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
        cw = _chatwoot_part(t, channels, since, until, rep, (t.id, t.chatwoot_account_id, name, tuple(c.id for c in channels)))
        per_channel = []
        for c in channels:
            m = cw["channels"].get(c.id, {"available": False, "reason": "no_workspace"})
            per_channel.append({"id": c.id, "type": c.channel_type, "name": c.display_name, "phone_number": c.phone_number,
                                "status": c.status, **m})
        out_ranges[name] = {"since": since.isoformat(), "until": until.isoformat(), "totals": cw["account"], "channels": per_channel,
                            "whatsapp": _whatsapp_billing(db, t, since, until, cur)}
    return {"tenant_id": t.id, "generated_at": now().isoformat(), "currency": cur, "wallet": wallet,
            "ai_replies": cv["ai_replies"], "seats": cv["seats"], "channels_plan": cv["channels"], "status": t.status,
            "ranges": out_ranges}
