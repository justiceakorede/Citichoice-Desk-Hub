"""Bills WhatsApp usage from Meta's own daily pricing analytics, so no webhook of yours has to change.

How it works
  * For each connected WhatsApp channel that has a WABA id and a phone number, ask Meta (Graph API `pricing_analytics`) how many
    messages of each pricing category went to each country on a UTC day.
  * A day is billed only once it is over by META_BILLING_LAG_HOURS (default 36h) so Meta's figures have settled, and never before
    META_BILLING_START. Nothing earlier is ever charged: history from before the hub existed stays unbilled.
  * Each (phone number, day, category, country) becomes ONE usage row whose key is fixed, so running this again, or twice at once,
    can never charge a day twice. The customer price per message comes from the hub's own rate cards, FX and markup at that day.
  * The wallet is debited exactly like any other usage: funds pay what they can and the rest becomes a receivable.

Meta's figures are daily, so the customer's balance falls once a day (for the day before yesterday), not per message.

[UNVERIFIED] The Graph `pricing_analytics` field syntax and response shape below were written from Meta's documentation from memory
and never run against a live WABA. `python -m app.cli meta-billing-sync --dry-run` prints what it parsed without charging anything.
"""
import datetime as dt
import json
import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Tenant, TenantChannel, UsageRecord
from . import pricing, reservations
from . import tenants as ts
from . import wallet as wl

PROVIDER = "meta_whatsapp"
_CAT = {"MARKETING": "marketing", "MARKETING_LITE": "marketing", "UTILITY": "utility", "AUTHENTICATION": "authentication",
        "AUTHENTICATION_INTERNATIONAL": "authentication", "SERVICE": "service"}   # referral conversions etc. are not billed


class MetaError(Exception):
    pass


@dataclass
class Point:
    phone: str          # digits only
    category: str
    country: str
    volume: int


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def fetch_day(waba_id: str, day: dt.date) -> list[Point]:
    s = get_settings()
    if not s.meta_access_token:
        raise MetaError("META_ACCESS_TOKEN is not set")
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    end = start + 86400
    fields = (f'pricing_analytics.start({start}).end({end}).granularity(DAILY).phone_numbers([])'
              f'.dimensions(["PRICING_CATEGORY","COUNTRY","PHONE"])')
    try:
        r = httpx.get(f"https://graph.facebook.com/{s.meta_graph_version}/{waba_id}", params={"fields": fields},
                      headers={"Authorization": f"Bearer {s.meta_access_token}"}, timeout=30)
    except httpx.HTTPError as e:
        raise MetaError(f"unreachable:{type(e).__name__}") from e
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise MetaError(f"meta_http_{r.status_code}: {msg[:200]}")
    return parse_points(r.json())


def parse_points(payload: dict) -> list[Point]:
    out = []
    for block in (payload.get("pricing_analytics") or {}).get("data", []) or []:
        for dp in block.get("data_points", []) or []:
            cat = _CAT.get(str(dp.get("pricing_category", "")).upper())
            vol = int(dp.get("volume") or 0)
            if cat and vol > 0:
                out.append(Point(_digits(dp.get("phone_number")), cat, str(dp.get("country") or "").upper(), vol))
    return out


def eligible_days(now: dt.datetime, days_back: int) -> list[dt.date]:
    s = get_settings()
    if not s.meta_billing_start:
        raise MetaError("META_BILLING_START is not set (YYYY-MM-DD). Set it to the first day you want billed; earlier days are never charged.")
    first = dt.date.fromisoformat(s.meta_billing_start)
    cutoff = now - dt.timedelta(hours=s.meta_billing_lag_hours)
    last = (cutoff - dt.timedelta(days=1)).date()            # the latest day that ended at least `lag` hours ago
    lo = max(first, last - dt.timedelta(days=max(0, days_back - 1)))
    return [lo + dt.timedelta(days=i) for i in range((last - lo).days + 1)] if last >= lo else []


def sync(db: Session, now: dt.datetime, days_back: int = 7, dry_run: bool = False, fetcher=fetch_day, actor: str = "cli:meta-billing") -> dict:
    """Trues up each (customer, day, category) to Meta's figure. Messages the hub already charged at send time (n8n) are counted first,
    so only the difference is charged. If the hub charged MORE than Meta reports it keeps the charge (no refunds are issued)."""
    from sqlalchemy import func
    channels = db.scalars(select(TenantChannel).where(TenantChannel.channel_type == "whatsapp", TenantChannel.status.in_(("connected", "pending")),
                                                      TenantChannel.external_account_id.is_not(None))).all()
    report = {"days": [], "charged_rows": 0, "messages": 0, "already_charged": 0, "over_charged": 0, "unmatched": 0, "errors": []}
    by_waba: dict[str, list[TenantChannel]] = {}
    for c in channels:
        by_waba.setdefault(c.external_account_id, []).append(c)
    days = eligible_days(now, days_back)
    report["days"] = [d.isoformat() for d in days]
    agg: dict[tuple, dict[str, int]] = {}
    for waba, chans in by_waba.items():
        for day in days:
            try:
                points = fetcher(waba, day)
            except MetaError as e:
                report["errors"].append(f"{waba} {day}: {e}")
                continue
            for pt in points:
                ch = next((c for c in chans if c.phone_number and _digits(c.phone_number) == pt.phone), None)
                if ch is None and len(chans) == 1 and not pt.phone:
                    ch = chans[0]
                t = db.get(Tenant, ch.tenant_id) if ch else None
                if t is None or t.status == "terminated":
                    report["unmatched"] += 1
                    continue
                bucket = agg.setdefault((t.id, day, pt.category), {})
                bucket[pt.country] = bucket.get(pt.country, 0) + pt.volume
    for (tid, day, cat), countries in agg.items():
        t = db.get(Tenant, tid)
        start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        have = int(db.scalar(select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
            UsageRecord.tenant_id == tid, UsageRecord.channel == "whatsapp", UsageRecord.category == cat,
            UsageRecord.created_at >= start, UsageRecord.created_at < start + dt.timedelta(days=1))) or 0)
        meta_total = sum(countries.values())
        report["already_charged"] += min(have, meta_total)
        delta = meta_total - have
        if delta <= 0:
            report["over_charged"] += -delta
            continue
        when = start + dt.timedelta(hours=23, minutes=59, seconds=59)
        w = wl.get_wallet(db, t.id)
        for country, vol in sorted(countries.items(), key=lambda kv: -kv[1]):       # the biggest market absorbs the difference first
            n = min(delta, vol)
            if n <= 0:
                break
            delta -= n
            key = f"pa:{t.id}:{day.isoformat()}:{cat}:{country}:{meta_total}"
            if db.scalar(select(UsageRecord.id).where(UsageRecord.provider == PROVIDER, UsageRecord.external_id == key)):
                continue
            try:
                snap = pricing.price(db, t, w, cat, country or None, when, persist=not dry_run)
            except pricing.PricingError as e:
                report["errors"].append(f"{t.slug} {day} {cat}/{country}: no price ({e})")
                continue
            report["charged_rows"] += 1
            report["messages"] += n
            if dry_run:
                continue
            cost, buf, fee, charge = snap.unit_cost_micro * n, snap.buffer_micro * n, snap.fee_micro * n, snap.unit_charge_micro * n
            try:
                with db.begin_nested():
                    db.add(UsageRecord(tenant_id=t.id, provider=PROVIDER, external_id=key, channel="whatsapp", category=cat,
                                       country=country or None, billed=charge > 0, quantity=n, customer_currency=w.currency,
                                       provider_currency=snap.provider_currency, pricing_snapshot_id=snap.id, provider_cost_micro=cost,
                                       fx_buffer_micro=buf, service_fee_micro=fee, charge_micro=charge, created_at=when))
                    db.flush()
            except IntegrityError:
                db.rollback()
                continue
            if charge:
                reservations.charge_message(db, t.id, charge, f"usage:{PROVIDER}:{key}", send_id=None, reference=key, currency=w.currency,
                                            meta={"category": cat, "country": country, "messages": n, "pricing_snapshot_id": snap.id, "day": day.isoformat(), "true_up": True})
            db.commit()
    if not dry_run:
        ts.audit(db, actor, "meta.billing_sync", None, {k: report[k] for k in ("days", "charged_rows", "messages", "already_charged", "over_charged", "unmatched")}
                 | {"errors": report["errors"][:5]}, outcome="failure" if report["errors"] else "success")
        db.commit()
    return report
