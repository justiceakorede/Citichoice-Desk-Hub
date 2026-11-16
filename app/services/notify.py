"""Customer notifications (with editable templates, HTML email and an exact-content log): a weekly WhatsApp/email report and a low-balance alert.

Sent from YOUR WhatsApp number (so a customer's empty wallet cannot stop their own alert) and by SMTP email. Each tenant has its own
switches (tenants.notify): report on/off, low-balance on/off, the numbers and emails that agreed to receive them.

WhatsApp needs two Meta-approved UTILITY templates in your own WhatsApp Business account (texts in docs/NOTIFICATIONS.md):
  weekly_report       {{1}} business  {{2}} period  {{3}} messages  {{4}} cost  {{5}} balance
  low_balance_alert   {{1}} business  {{2}} balance  {{3}} advice

Safe to run as often as you like (hourly): a week's report is sent once, and an alert repeats only when the level worsens or after 72h.
[UNVERIFIED] never run against Meta or an SMTP server.
"""
import datetime as dt
import html as _html
import re
import smtplib
import ssl
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from decimal import Decimal

from .. import money
from ..config import get_settings
from ..models import Tenant, UsageRecord
from . import tenants as ts

LAGOS = ZoneInfo("Africa/Lagos")
LEVELS = {"ok": 0, "low": 1, "critical": 2, "zero": 3, "debt": 4}
ADVICE = {"low": "Top up soon so messages keep flowing.", "critical": "Top up now to avoid messages being stopped.",
          "zero": "Your credit is empty, so WhatsApp messages that need credit are not being sent. Top up now.",
          "debt": "You owe credit for messages already delivered. Your next top-up clears it first."}


FREQS = ("off", "daily", "weekly", "monthly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def conf(t: Tenant) -> dict:
    n = dict(t.notify or {})
    n.setdefault("report_enabled", False)
    n.setdefault("low_balance_enabled", False)
    n.setdefault("whatsapp", [])
    n.setdefault("email", [])
    n.setdefault("state", {})
    n.setdefault("whatsapp_freq", "weekly")      # how often each channel gets the report
    n.setdefault("email_freq", "weekly")
    n.setdefault("hour", 8)                      # Lagos time
    n.setdefault("weekday", 0)                   # for weekly reports: 0 = Monday
    n.setdefault("low_threshold_usd", 0)         # alert when available credit (minus anything owed) is below this many USD; 0 = automatic levels
    n.setdefault("low_repeat_hours", 24)         # while still below, repeat this often; 0 = once per dip
    return n


def _digits(v: str) -> str:
    return "".join(c for c in str(v) if c.isdigit())


def set_conf(t: Tenant, *, report=None, low=None, whatsapp=None, email=None, whatsapp_freq=None, email_freq=None, hour=None, weekday=None,
             low_threshold_usd=None, low_repeat_hours=None) -> None:
    n = conf(t)
    if report is not None:
        n["report_enabled"] = bool(report)
    if low is not None:
        n["low_balance_enabled"] = bool(low)
    if whatsapp is not None:
        n["whatsapp"] = sorted({d for d in (_digits(x) for x in whatsapp) if 8 <= len(d) <= 15})
    if email is not None:
        n["email"] = sorted({e.strip().lower() for e in email if "@" in e and len(e) < 200})
    for key, val in (("whatsapp_freq", whatsapp_freq), ("email_freq", email_freq)):
        if val is not None:
            if val not in FREQS:
                raise ValueError(f"{key} must be one of {', '.join(FREQS)}")
            n[key] = val
    if hour is not None:
        if not 0 <= int(hour) <= 23:
            raise ValueError("hour must be 0 to 23 (Lagos time)")
        n["hour"] = int(hour)
    if weekday is not None:
        wd = WEEKDAYS.index(weekday.lower()[:3]) if isinstance(weekday, str) and not weekday.isdigit() else int(weekday)
        if not 0 <= wd <= 6:
            raise ValueError("weekday must be mon to sun")
        n["weekday"] = wd
    if low_threshold_usd is not None:
        v = float(low_threshold_usd)
        if not 0 <= v <= 100000:
            raise ValueError("the low-balance threshold must be between 0 and 100000 USD (0 = automatic levels)")
        n["low_threshold_usd"] = v
    if low_repeat_hours is not None:
        if not 0 <= int(low_repeat_hours) <= 720:
            raise ValueError("repeat hours must be 0 (once) to 720")
        n["low_repeat_hours"] = int(low_repeat_hours)
    t.notify = n


# ---------------------------------------------------------------- senders
def send_whatsapp(to: str, template: str, params: list[str], language: str | None = None) -> tuple[bool, str]:
    s = get_settings()
    token = s.notify_wa_token or s.meta_access_token
    if not (s.notify_wa_phone_number_id and token):
        return False, "whatsapp_not_configured"
    clean = [" ".join(str(p).split())[:300] or "-" for p in params]        # Meta rejects newlines and empty parameters
    try:
        r = httpx.post(f"https://graph.facebook.com/{s.meta_graph_version}/{s.notify_wa_phone_number_id}/messages",
                       headers={"Authorization": f"Bearer {token}"}, timeout=20,
                       json={"messaging_product": "whatsapp", "to": to, "type": "template",
                             "template": {"name": template, "language": {"code": language or s.notify_wa_language},
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


def send_email(to: str, subject: str, text: str, html: str | None = None) -> tuple[bool, str]:
    s = get_settings()
    if not (s.smtp_host and s.smtp_from):
        return False, "smtp_not_configured"
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = s.smtp_from, to, subject
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")        # clients that can show HTML do; the rest get the text above
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(m)
    except (smtplib.SMTPException, OSError) as e:
        return False, f"smtp_error:{type(e).__name__}"
    return True, "sent"


# ---------------------------------------------------------------- templates
PLACEHOLDERS = {
    "business": "the customer's business name", "period": "the period covered, e.g. 03 Nov - 09 Nov 2026", "messages": "WhatsApp messages in the period",
    "cost": "what those messages cost", "balance": "WhatsApp credit left", "owed": "amount owed, or empty", "owed_text": "a sentence about money owed, or empty",
    "advice": "a short top-up suggestion that fits the situation", "advice_html": "the same as a coloured box (email only)", "threshold": "the low-balance level you set, e.g. USD 10",
    "breakdown_text": "per-type usage lines (plain text)", "breakdown_html": "per-type usage table (email only)", "topup_link": "your buy-credit link",
    "portal_name": "Citichoice Desk", "today": "today's date",
}
_PH = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")

_BTN = ('<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 6px"><tr><td style="background:#4f46e5;border-radius:8px">'
        '<a href="{{topup_link}}" style="display:inline-block;padding:12px 22px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px">Buy WhatsApp credit</a></td></tr></table>')


def _shell(inner: str) -> str:
    return ('<!doctype html><html><body style="margin:0;padding:0;background:#f4f6fa"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fa;padding:24px 12px"><tr><td align="center">'
            '<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#12161f">'
            '<tr><td style="background:#0f1420;padding:18px 24px;color:#ffffff;font-size:16px;font-weight:600">{{portal_name}}</td></tr>'
            '<tr><td style="padding:24px;font-size:15px;line-height:1.55">' + inner + '</td></tr>'
            '<tr><td style="padding:14px 24px;background:#f4f6fa;color:#667085;font-size:12px">Sent by {{portal_name}} on {{today}}. You receive this because you asked for WhatsApp usage updates.</td></tr></table></td></tr></table></body></html>')


DEFAULTS = {
    "report": {
        "email_subject": "Your WhatsApp summary for {{period}}",
        "email_text": ("Hi {{business}},\n\nYour {{portal_name}} WhatsApp summary for {{period}}:\n\n  Messages sent: {{messages}}\n  Total cost:    {{cost}}\n  Credit left:   {{balance}}\n"
                       "{{owed_text}}\n{{breakdown_text}}\n{{advice}}\n\nBuy more WhatsApp credit: {{topup_link}}\n"),
        "email_html": _shell('<p style="margin:0 0 4px;font-size:18px;font-weight:600">Hi {{business}},</p><p style="margin:0 0 18px;color:#667085">Your WhatsApp summary for {{period}}</p>'
                             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                             '<td style="width:33%;padding:12px;background:#f4f6fa;border-radius:8px"><div style="color:#667085;font-size:12px">Messages sent</div><div style="font-size:22px;font-weight:700">{{messages}}</div></td><td style="width:8px"></td>'
                             '<td style="width:33%;padding:12px;background:#f4f6fa;border-radius:8px"><div style="color:#667085;font-size:12px">Total cost</div><div style="font-size:22px;font-weight:700">{{cost}}</div></td><td style="width:8px"></td>'
                             '<td style="width:33%;padding:12px;background:#f4f6fa;border-radius:8px"><div style="color:#667085;font-size:12px">Credit left</div><div style="font-size:22px;font-weight:700">{{balance}}</div></td></tr></table>'
                             '{{breakdown_html}}{{advice_html}}' + _BTN),
        "wa_params": ["{{business}}", "{{period}}", "{{messages}}", "{{cost}}", "{{balance}}"],
        "wa_wording": "Hi {{1}}, your Citichoice Desk WhatsApp summary for {{2}}: {{3}} messages sent, total cost {{4}}. Credit left: {{5}}.",
    },
    "low_balance": {
        "email_subject": "Your WhatsApp credit needs attention",
        "email_text": "Hi {{business}},\n\nYour {{portal_name}} WhatsApp credit is {{balance}}.\n{{owed_text}}\n{{advice}}\n\nBuy more WhatsApp credit: {{topup_link}}\n",
        "email_html": _shell('<p style="margin:0 0 4px;font-size:18px;font-weight:600">Hi {{business}},</p><p style="margin:0 0 14px;color:#667085">A quick heads-up about your WhatsApp credit.</p>'
                             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:14px;background:#f4f6fa;border-radius:8px"><div style="color:#667085;font-size:12px">Credit left</div>'
                             '<div style="font-size:26px;font-weight:700">{{balance}}</div><div style="color:#667085;font-size:13px;margin-top:4px">{{owed_text}}</div></td></tr></table>{{advice_html}}' + _BTN),
        "wa_params": ["{{business}}", "{{balance}}", "{{advice}}"],
        "wa_wording": "Hi {{1}}, your Citichoice Desk WhatsApp credit is {{2}}. {{3}}",
    },
}


def render(text: str, ctx: dict, html: bool = False) -> str:
    def sub(m):
        v = ctx.get(m.group(1))
        if v is None:
            return m.group(0)
        v = str(v)
        return v if (not html or m.group(1).endswith("_html")) else _html.escape(v)
    return _PH.sub(sub, text or "")


def unknown_placeholders(*texts: str) -> set[str]:
    return {k for t in texts for k in _PH.findall(t or "") if k not in PLACEHOLDERS}


def get_template(db: Session, key: str) -> dict:
    from ..models import NotifyTemplate
    s = get_settings()
    row = db.get(NotifyTemplate, key)
    if row:
        return {"email_subject": row.email_subject, "email_text": row.email_text, "email_html": row.email_html, "wa_template": row.wa_template,
                "wa_language": row.wa_language, "wa_params": list(row.wa_params or []), "wa_wording": row.wa_wording, "customised": True}
    d = dict(DEFAULTS[key])
    d["wa_template"] = s.notify_wa_report_template if key == "report" else s.notify_wa_low_template
    d["wa_language"] = s.notify_wa_language
    d["customised"] = False
    return d


def _advice_html(text: str, level: str) -> str:
    if not text:
        return ""
    colour = {"zero": ("#fde4e1", "#a3261a"), "debt": ("#fde4e1", "#a3261a"), "critical": ("#fde4e1", "#a3261a")}.get(level, ("#fff1d6", "#8a5a00"))
    return f'<div style="margin:18px 0 0;padding:12px 14px;border-radius:8px;background:{colour[0]};color:{colour[1]};font-size:14px">{_html.escape(text)}</div>'


def common_ctx(t_name: str, balance: str, owed: str, advice: str, level: str, extra: dict | None = None) -> dict:
    s = get_settings()
    ctx = {"business": t_name, "balance": balance, "owed": owed, "owed_text": f"You owe {owed} for messages already delivered." if owed else "", "advice": advice,
           "advice_html": _advice_html(advice, level), "topup_link": s.notify_topup_url or "", "portal_name": "Citichoice Desk",
           "today": dt.datetime.now(LAGOS).strftime("%d %b %Y"), "threshold": "", "period": "", "messages": "", "cost": "", "breakdown_text": "", "breakdown_html": ""}
    ctx.update(extra or {})
    return ctx


def report_ctx(rep: dict, owed: str = "") -> dict:
    lines = [f"  {c.capitalize():<15}{n:>8,}   {cost}" for c, (n, cost) in sorted(rep["by_category"].items())]
    rows = "".join(f'<tr><td style="padding:6px 10px;border-bottom:1px solid #e6e9f0">{_html.escape(c.capitalize())}</td><td style="padding:6px 10px;border-bottom:1px solid #e6e9f0;text-align:right">{n:,}</td>'
                   f'<td style="padding:6px 10px;border-bottom:1px solid #e6e9f0;text-align:right">{_html.escape(cost)}</td></tr>' for c, (n, cost) in sorted(rep["by_category"].items()))
    table = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:18px;font-size:14px;border-collapse:collapse"><tr>'
             '<th align="left" style="padding:6px 10px;color:#667085;font-size:12px">Type</th><th align="right" style="padding:6px 10px;color:#667085;font-size:12px">Messages</th>'
             '<th align="right" style="padding:6px 10px;color:#667085;font-size:12px">Cost</th></tr>' + rows + '</table>') if rows else ""
    level = rep["alert"] or "ok"
    advice = ADVICE.get(level, "")
    return common_ctx(rep["business"], rep["balance"], owed, advice, level, {"period": rep["period"], "messages": f"{rep['messages']:,}", "cost": rep["cost"],
                                                                         "breakdown_text": ("By type:\n" + "\n".join(lines)) if lines else "", "breakdown_html": table})


def sample_report_ctx() -> dict:
    rep = {"business": "Sample Business", "period": "03 Nov - 09 Nov 2026", "messages": 1240, "cost": "NGN 12,300.00", "balance": "NGN 7,700.00", "alert": "low",
           "by_category": {"service": (900, "NGN 8,100.00"), "utility": (300, "NGN 3,900.00"), "marketing": (40, "NGN 300.00")}}
    return report_ctx(rep)


def sample_low_ctx() -> dict:
    return common_ctx("Sample Business", "NGN 1,150.00", "", ADVICE["low"], "low", {"threshold": "USD 10"})


def render_all(tpl: dict, ctx: dict) -> dict:
    params = [render(p, ctx) for p in tpl["wa_params"]]
    wording = tpl["wa_wording"]
    for i, v in enumerate(params, 1):
        wording = wording.replace("{{" + str(i) + "}}", " ".join(str(v).split()) or "-")
    return {"subject": render(tpl["email_subject"], ctx), "text": render(tpl["email_text"], ctx), "html": render(tpl["email_html"], ctx, html=True),
            "wa_params": params, "wa_wording": wording}


def build_report(db: Session, t: Tenant, start: dt.datetime, end: dt.datetime) -> dict:
    cv = ts.customer_view(db, t)
    cur = cv["wallet"]["currency"]
    rows = db.execute(select(UsageRecord.category, func.coalesce(func.sum(UsageRecord.quantity), 0), func.coalesce(func.sum(UsageRecord.charge_micro), 0))
                      .where(UsageRecord.tenant_id == t.id, UsageRecord.channel == "whatsapp", UsageRecord.created_at >= start, UsageRecord.created_at < end)
                      .group_by(UsageRecord.category)).all()
    msgs = sum(int(n) for _, n, _ in rows)
    cost = sum(int(c) for _, _, c in rows)
    sl, el = start.astimezone(LAGOS), (end - dt.timedelta(seconds=1)).astimezone(LAGOS)
    label = f"{sl:%d %b %Y}" if sl.date() == el.date() else (f"{sl:%d %b} - {el:%d %b %Y}" if sl.year == el.year else f"{sl:%d %b %Y} - {el:%d %b %Y}")
    return {"business": t.name, "period": label, "messages": msgs, "cost": money.format_micro(cost, cur),
            "balance": cv["wallet"]["balance"], "alert": cv["wallet"]["alert"], "by_category": {c: (int(n), money.format_micro(int(ch), cur)) for c, n, ch in rows}}


def schedule_point(now: dt.datetime, freq: str, weekday: int, hour: int):
    """The most recent moment this report was due (Lagos time) and the complete period it covers.
    daily: yesterday. weekly: the 7 whole days before the send day. monthly: the previous calendar month."""
    nl = now.astimezone(LAGOS)
    at = lambda d: d.replace(hour=hour, minute=0, second=0, microsecond=0)
    if freq == "daily":
        occ = at(nl)
        if occ > nl:
            occ -= dt.timedelta(days=1)
        end = occ.replace(hour=0)
        start = end - dt.timedelta(days=1)
    elif freq == "weekly":
        d = nl
        while d.weekday() != weekday:
            d -= dt.timedelta(days=1)
        occ = at(d)
        if occ > nl:
            occ -= dt.timedelta(days=7)
        end = occ.replace(hour=0)
        start = end - dt.timedelta(days=7)
    else:
        occ = at(nl.replace(day=1))
        if occ > nl:
            occ = at((occ - dt.timedelta(days=1)).replace(day=1))
        end = occ.replace(hour=0)
        start = (end - dt.timedelta(days=1)).replace(day=1)
    return occ, start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc), occ.strftime("%Y-%m-%d")


def on_demand_bounds(now: dt.datetime, period: str, date_from: str = "", date_to: str = "") -> tuple[dt.datetime, dt.datetime]:
    nl = now.astimezone(LAGOS)
    today = nl.replace(hour=0, minute=0, second=0, microsecond=0)
    utc = lambda d: d.astimezone(dt.timezone.utc)
    if period == "yesterday":
        return utc(today - dt.timedelta(days=1)), utc(today)
    if period == "last7":
        return utc(today - dt.timedelta(days=7)), utc(today)
    if period == "last30":
        return utc(today - dt.timedelta(days=30)), utc(today)
    if period == "mtd":
        return utc(today.replace(day=1)), now.astimezone(dt.timezone.utc)
    if period == "custom":
        try:
            a = dt.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=LAGOS)
            b = dt.datetime.strptime(date_to or date_from, "%Y-%m-%d").replace(tzinfo=LAGOS) + dt.timedelta(days=1)
        except ValueError:
            raise ValueError("dates must look like 2026-11-30")
        if b <= a or (b - a).days > 400:
            raise ValueError("the end date must be after the start date, within 400 days")
        return utc(a), min(utc(b), now.astimezone(dt.timezone.utc))
    raise ValueError("period must be yesterday, last7, last30, mtd or custom")


# ---------------------------------------------------------------- delivery + log
def _deliver(db: Session, t, kind: str, n: dict, tpl_key: str, ctx: dict, channels: tuple = ("whatsapp", "email"), note: str = "", tenant_id: str | None = None) -> list[str]:
    """Render the template, send, and log the EXACT content of every message (what the customer got, or what was refused)."""
    from ..models import NotificationLog
    tpl = get_template(db, tpl_key)
    r = render_all(tpl, ctx)
    tid = tenant_id or t.id
    out = []
    if "whatsapp" in channels:
        for num in n["whatsapp"]:
            ok, why = send_whatsapp(num, tpl["wa_template"], r["wa_params"], tpl["wa_language"])
            db.add(NotificationLog(tenant_id=tid, kind=kind, channel="whatsapp", recipient="+" + num, status="sent" if ok else "failed", detail=(note or None) if ok else why[:400],
                                   content_text=r["wa_wording"], wa_template=f"{tpl['wa_template']} ({tpl['wa_language']})", wa_params=r["wa_params"]))
            out.append(f"whatsapp:{num[-4:]}:{'ok' if ok else why}")
    if "email" in channels:
        for addr in n["email"]:
            ok, why = send_email(addr, r["subject"], r["text"], r["html"])
            db.add(NotificationLog(tenant_id=tid, kind=kind, channel="email", recipient=addr, status="sent" if ok else "failed", detail=(note or None) if ok else why[:400],
                                   subject=r["subject"], content_text=r["text"], content_html=r["html"]))
            out.append(f"email:{addr.split('@')[0][:2]}***:{'ok' if ok else why}")
    return out


def send_test(db: Session, key: str, actor: str, email: str = "", whatsapp: str = "") -> list[str]:
    """Send the CURRENT template with sample figures to one address, so you can see it before customers do."""
    ctx = sample_report_ctx() if key == "report" else sample_low_ctx()
    n = {"whatsapp": [d for d in [_digits(whatsapp)] if 8 <= len(d) <= 15], "email": [email.strip().lower()] if "@" in email else []}
    if not n["whatsapp"] and not n["email"]:
        raise ValueError("give an email address or a WhatsApp number to send the test to")
    out = _deliver(db, None, "test", n, key, ctx, tenant_id="test", note=f"test by {actor}")
    ts.audit(db, actor, "notify.template_test", None, {"template": key, "results": out})
    return out


def send_report_now(db: Session, t: Tenant, actor: str, period: str, channels: tuple, date_from: str = "", date_to: str = "",
                    extra_whatsapp: str = "", extra_email: str = "", now: dt.datetime | None = None) -> dict:
    """On demand: send this customer a report right now, for the period you choose. Does not touch the schedule."""
    now = now or dt.datetime.now(dt.timezone.utc)
    channels = tuple(c for c in channels if c in ("whatsapp", "email"))
    if not channels:
        raise ValueError("choose WhatsApp, email or both")
    start, end = on_demand_bounds(now, period, date_from, date_to)
    n = conf(t)
    n["whatsapp"] = sorted(set(n["whatsapp"]) | {d for d in (_digits(x) for x in extra_whatsapp.split(",")) if 8 <= len(d) <= 15})
    n["email"] = sorted(set(n["email"]) | {e.strip().lower() for e in extra_email.split(",") if "@" in e})
    if not (("whatsapp" in channels and n["whatsapp"]) or ("email" in channels and n["email"])):
        raise ValueError("no recipients for the chosen channels: add numbers or emails on the tenant page, or fill the one-off boxes")
    rep = build_report(db, t, start, end)
    owed = _owed_text(db, t)
    sent = _deliver(db, t, "report_on_demand", n, "report", report_ctx(rep, owed), channels, note=f"by {actor}")
    ts.audit(db, actor, "notify.report_on_demand", t.id, {"period": rep["period"], "channels": list(channels), "results": sent})
    return {"period": rep["period"], "messages": rep["messages"], "cost": rep["cost"], "results": sent}


def _owed_text(db: Session, t: Tenant) -> str:
    w = ts.customer_view(db, t)["wallet"]
    return w["receivable"] if w.get("receivable_micro", 0) > 0 else ""


def _usd(db: Session, micro: int, cur: str, now: dt.datetime) -> Decimal | None:
    from . import fx
    major = Decimal(micro) / Decimal(1_000_000)
    if cur == "USD":
        return major
    try:
        rate, _ = fx.rate_between(db, "USD", cur, now)
    except Exception:
        return None
    return major / rate if rate else None


def run(db: Session, now: dt.datetime, force_report: bool = False, dry_run: bool = False) -> dict:
    res = {"reports": [], "alerts": [], "skipped": []}
    for t in db.scalars(select(Tenant).where(Tenant.status.in_(("active", "suspended")))):
        n = conf(t)
        st = dict(n["state"])
        if n["report_enabled"]:
            for ch in ("whatsapp", "email"):
                freq = n[f"{ch}_freq"]
                if freq == "off" or not n[ch]:
                    continue
                occ, start, end, key = schedule_point(now, freq, int(n["weekday"]), int(n["hour"]))
                due = force_report or (0 <= (now - occ).total_seconds() < 86400 and st.get(f"{ch}_sent") != key)
                if not due:
                    continue
                rep = build_report(db, t, start, end)
                one = {**n, "whatsapp": n["whatsapp"] if ch == "whatsapp" else [], "email": n["email"] if ch == "email" else []}
                sent = ["dry-run"] if dry_run else _deliver(db, t, f"{freq}_report", one, "report", report_ctx(rep, _owed_text(db, t)), (ch,))
                res["reports"].append({"tenant": t.slug, "channel": ch, "frequency": freq, "period": rep["period"], "results": sent})
                if not dry_run and any(x.endswith(":ok") for x in sent):
                    st[f"{ch}_sent"] = key
        # ---- low balance: below your USD level (or the automatic levels), then repeat every N hours until it is above again
        why = None
        if not n["low_balance_enabled"]:
            why = "low-balance alerts are switched off for this customer"
        elif not (n["whatsapp"] or n["email"]):
            why = "no WhatsApp numbers or emails saved"
        else:
            cv = ts.customer_view(db, t)["wallet"]
            eff = int(cv["available_micro"]) - int(cv.get("receivable_micro", 0))
            thr = Decimal(str(n.get("low_threshold_usd") or 0))
            level = cv["alert"] or "ok"
            if thr > 0:
                usd = _usd(db, eff, cv["currency"], now)
                if usd is None:
                    why, below = "exchange rate unavailable (run fx-sync)", False
                else:
                    below = usd < thr
                    level = level if level != "ok" else "low"
                    if eff <= 0 and level == "low":
                        level = "zero"
            else:
                below = level in ADVICE
            if why is None:
                if not below:
                    if st.get("low_active"):
                        st["low_active"] = False
                    why = "credit is above the alert level"
                else:
                    last_at = st.get("low_at")
                    repeat = int(n.get("low_repeat_hours", 24))
                    waited = (now - dt.datetime.fromisoformat(last_at)) if last_at else None
                    due = (not st.get("low_active")) or (repeat > 0 and waited is not None and waited >= dt.timedelta(hours=repeat))
                    if not due:
                        left = (dt.timedelta(hours=repeat) - waited) if waited is not None and repeat > 0 else None
                        why = "already alerted; " + (f"next reminder in {int(left.total_seconds() // 3600)}h" if left else "no repeat is set")
                    else:
                        advice = ADVICE.get(level, ADVICE["low"])
                        ctx = common_ctx(t.name, cv["balance"], cv["receivable"] if int(cv.get("receivable_micro", 0)) > 0 else "", advice, level,
                                         {"threshold": f"USD {thr:g}" if thr > 0 else ""})
                        sent = ["dry-run"] if dry_run else _deliver(db, t, "low_balance", n, "low_balance", ctx)
                        res["alerts"].append({"tenant": t.slug, "level": level, "results": sent})
                        if not dry_run and any(x.endswith(":ok") for x in sent):
                            st["low_active"], st["low_level"], st["low_at"] = True, level, now.isoformat()
        if why:
            res["skipped"].append({"tenant": t.slug, "low_balance": why})
        if not dry_run and st != n["state"]:
            n["state"] = st
            t.notify = n
    if not dry_run:
        ts.audit(db, "notify-run", "notify.run", None, {"reports": len(res["reports"]), "alerts": len(res["alerts"]), "skipped": len(res["skipped"])})
        db.commit()
    return res
