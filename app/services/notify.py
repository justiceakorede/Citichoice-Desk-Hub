"""Customer notifications: a weekly WhatsApp/email report and a low-balance alert.

Sent from YOUR WhatsApp number (so a customer's empty wallet cannot stop their own alert) and by SMTP email. Each tenant has its own
switches (tenants.notify): report on/off, low-balance on/off, the numbers and emails that agreed to receive them.

WhatsApp needs two Meta-approved UTILITY templates in your own WhatsApp Business account (texts in docs/NOTIFICATIONS.md):
  weekly_report       {{1}} business  {{2}} period  {{3}} messages  {{4}} cost  {{5}} balance
  low_balance_alert   {{1}} business  {{2}} balance  {{3}} advice

Safe to run as often as you like (hourly): a week's report is sent once, and an alert repeats only when the level worsens or after 72h.
[UNVERIFIED] never run against Meta or an SMTP server.
"""
import datetime as dt
import smtplib
import ssl
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import Tenant, UsageRecord
from . import tenants as ts

LAGOS = ZoneInfo("Africa/Lagos")
LEVELS = {"ok": 0, "low": 1, "critical": 2, "zero": 3, "debt": 4}
ADVICE = {"low": "Top up soon so messages keep flowing.", "critical": "Top up now to avoid messages being stopped.",
          "zero": "Your credit is empty, so WhatsApp messages that need credit are not being sent. Top up now.",
          "debt": "You owe credit for messages already delivered. Your next top-up clears it first."}


def conf(t: Tenant) -> dict:
    n = dict(t.notify or {})
    n.setdefault("report_enabled", False)
    n.setdefault("low_balance_enabled", False)
    n.setdefault("whatsapp", [])
    n.setdefault("email", [])
    n.setdefault("state", {})
    return n


def _digits(v: str) -> str:
    return "".join(c for c in str(v) if c.isdigit())


def set_conf(t: Tenant, *, report=None, low=None, whatsapp=None, email=None) -> None:
    n = conf(t)
    if report is not None:
        n["report_enabled"] = bool(report)
    if low is not None:
        n["low_balance_enabled"] = bool(low)
    if whatsapp is not None:
        n["whatsapp"] = sorted({d for d in (_digits(x) for x in whatsapp) if 8 <= len(d) <= 15})
    if email is not None:
        n["email"] = sorted({e.strip().lower() for e in email if "@" in e and len(e) < 200})
    t.notify = n


# ---------------------------------------------------------------- senders
def send_whatsapp(to: str, template: str, params: list[str]) -> tuple[bool, str]:
    s = get_settings()
    token = s.notify_wa_token or s.meta_access_token
    if not (s.notify_wa_phone_number_id and token):
        return False, "whatsapp_not_configured"
    clean = [" ".join(str(p).split())[:300] or "-" for p in params]        # Meta rejects newlines and empty parameters
    try:
        r = httpx.post(f"https://graph.facebook.com/{s.meta_graph_version}/{s.notify_wa_phone_number_id}/messages",
                       headers={"Authorization": f"Bearer {token}"}, timeout=20,
                       json={"messaging_product": "whatsapp", "to": to, "type": "template",
                             "template": {"name": template, "language": {"code": s.notify_wa_language},
                                          "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in clean]}]}})
    except httpx.HTTPError as e:
        return False, f"unreachable:{type(e).__name__}"
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        return False, f"meta_http_{r.status_code}: {msg[:120]}"
    return True, "sent"


def send_email(to: str, subject: str, body: str) -> tuple[bool, str]:
    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        return False, "smtp_not_configured"
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = s.smtp_from, to, subject
    m.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(m)
    except (smtplib.SMTPException, OSError) as e:
        return False, f"smtp_error:{type(e).__name__}"
    return True, "sent"


# ---------------------------------------------------------------- weekly report
def week_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """The last COMPLETE Monday-to-Sunday week in Lagos time, as UTC bounds, plus its ISO key."""
    ln = now.astimezone(LAGOS)
    this_monday = (ln - dt.timedelta(days=ln.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start_l = this_monday - dt.timedelta(days=7)
    iso = start_l.isocalendar()
    return start_l.astimezone(dt.timezone.utc), this_monday.astimezone(dt.timezone.utc), f"{iso[0]}-W{iso[1]:02d}"


def build_report(db: Session, t: Tenant, start: dt.datetime, end: dt.datetime) -> dict:
    cv = ts.customer_view(db, t)
    cur = cv["wallet"]["currency"]
    rows = db.execute(select(UsageRecord.category, func.coalesce(func.sum(UsageRecord.quantity), 0), func.coalesce(func.sum(UsageRecord.charge_micro), 0))
                      .where(UsageRecord.tenant_id == t.id, UsageRecord.channel == "whatsapp", UsageRecord.created_at >= start, UsageRecord.created_at < end)
                      .group_by(UsageRecord.category)).all()
    msgs = sum(int(n) for _, n, _ in rows)
    cost = sum(int(c) for _, _, c in rows)
    sl, el = start.astimezone(LAGOS), (end - dt.timedelta(seconds=1)).astimezone(LAGOS)
    return {"business": t.name, "period": f"{sl:%d %b} - {el:%d %b %Y}", "messages": msgs, "cost": money.format_micro(cost, cur),
            "balance": cv["wallet"]["balance"], "alert": cv["wallet"]["alert"], "by_category": {c: (int(n), money.format_micro(int(ch), cur)) for c, n, ch in rows}}


def _report_email(rep: dict) -> tuple[str, str]:
    lines = [f"Hi {rep['business']},", "", f"Your Citichoice Desk WhatsApp summary for {rep['period']}:", "",
             f"  Messages sent: {rep['messages']:,}", f"  Total cost:    {rep['cost']}", f"  Credit left:   {rep['balance']}", ""]
    if rep["by_category"]:
        lines.append("By type:")
        lines += [f"  {c.capitalize():<15}{n:>8,}   {cost}" for c, (n, cost) in sorted(rep["by_category"].items())]
        lines.append("")
    if rep["alert"] in ADVICE:
        lines += [ADVICE[rep["alert"]], ""]
    lines.append("Buy more WhatsApp credit from your client area (Add-ons).")
    return f"Your WhatsApp summary for {rep['period']}", "\n".join(lines)


def _deliver(n: dict, wa_template: str, wa_params: list[str], subject: str, body: str) -> list[str]:
    out = []
    for num in n["whatsapp"]:
        ok, why = send_whatsapp(num, wa_template, wa_params)
        out.append(f"whatsapp:{num[-4:]}:{'ok' if ok else why}")
    for addr in n["email"]:
        ok, why = send_email(addr, subject, body)
        out.append(f"email:{addr.split('@')[0][:2]}***:{'ok' if ok else why}")
    return out


def run(db: Session, now: dt.datetime, force_report: bool = False, dry_run: bool = False) -> dict:
    s = get_settings()
    res = {"reports": [], "alerts": [], "skipped": 0}
    start, end, wkey = week_bounds(now)
    monday_8 = end.astimezone(LAGOS).replace(hour=8)
    report_due = force_report or now.astimezone(LAGOS) >= monday_8
    for t in db.scalars(select(Tenant).where(Tenant.status.in_(("active", "suspended")))):
        n = conf(t)
        st = dict(n["state"])
        if n["report_enabled"] and (n["whatsapp"] or n["email"]) and report_due and (force_report or st.get("report_week") != wkey):
            rep = build_report(db, t, start, end)
            subject, body = _report_email(rep)
            sent = ["dry-run"] if dry_run else _deliver(n, s.notify_wa_report_template,
                                                       [rep["business"], rep["period"], f"{rep['messages']:,}", rep["cost"], rep["balance"]], subject, body)
            res["reports"].append({"tenant": t.slug, "week": wkey, "results": sent})
            if not dry_run and any(x.endswith(":ok") for x in sent):
                st["report_week"] = wkey
        level = ts.customer_view(db, t)["wallet"]["alert"] or "ok"
        if n["low_balance_enabled"] and (n["whatsapp"] or n["email"]):
            last_level, last_at = st.get("low_level", "ok"), st.get("low_at")
            stale = not last_at or (now - dt.datetime.fromisoformat(last_at)) > dt.timedelta(hours=72)
            if level in ADVICE and (LEVELS[level] > LEVELS.get(last_level, 0) or (level == last_level and stale)):
                bal = ts.customer_view(db, t)["wallet"]["balance"]
                sent = ["dry-run"] if dry_run else _deliver(n, s.notify_wa_low_template, [t.name, bal, ADVICE[level]],
                                                           "Your WhatsApp credit needs attention", f"Hi {t.name},\n\nYour WhatsApp credit is {bal}. {ADVICE[level]}\n\nBuy more under Add-ons in your client area.")
                res["alerts"].append({"tenant": t.slug, "level": level, "results": sent})
                if not dry_run and any(x.endswith(":ok") for x in sent):
                    st["low_level"], st["low_at"] = level, now.isoformat()
            elif level == "ok" and last_level != "ok":
                st["low_level"] = "ok"
        if not dry_run and st != n["state"]:
            n["state"] = st
            t.notify = n
    if not dry_run:
        db.commit()
    return res
