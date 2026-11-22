"""Add Funds and automatic recharge of the WhatsApp wallet, paid through WiseCP.

Money never moves here by guesswork: the hub asks WiseCP to issue a top-up invoice on the customer's Desk service, WiseCP collects it
(the customer's chosen gateway, or their Account Balance for automatic recharge), and the wallet is credited ONLY by the existing,
idempotent invoice crediting (`wisecp.process_invoice`: reads the invoice back from WiseCP, needs status `paid`, credits the line's
subtotal excluding tax, once). This module only creates invoices and watches them.

Safeguards for automatic recharge: one open recharge per tenant (unique index), a minimum gap, a daily and a monthly cap, a hard stop
after repeated failures (the customer is told), an unpaid invoice is cancelled after a timeout, and the customer must have opted in.
"""
import datetime as dt
import json
import logging
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..models import AutoRecharge, NotificationLog, RechargeAttempt, Tenant, Wallet, now
from . import fx
from . import notify as nt
from . import tenants as ts
from . import wallet as wl
from . import wisecp as wc

log = logging.getLogger("hub.recharge")
OPEN = ("creating", "awaiting_payment")


class RechargeError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------------------------------------------- amounts
def _parse_amount(db: Session, w: Wallet, value, field: str) -> tuple[Decimal, int]:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise RechargeError("bad_amount", f"{field} is not a number")
    if d <= 0:
        raise RechargeError("bad_amount", f"{field} must be above zero")
    exp = money.exponent_of(w.currency)
    d = d.quantize(Decimal(1).scaleb(-exp), rounding=ROUND_DOWN)
    if d <= 0:
        raise RechargeError("bad_amount", f"{field} is below the smallest unit of {w.currency}")
    return d, money.input_to_micro(d, w.currency)


def bounds(db: Session, w: Wallet) -> tuple[Decimal | None, Decimal | None]:
    """The smallest and largest single top-up in the wallet's currency (from USD limits). None when no fresh rate is known."""
    s = get_settings()
    try:
        rate, _ = fx.rate_between(db, "USD", w.currency, now())
    except fx.FxError:
        return None, None
    exp = Decimal(1).scaleb(-money.exponent_of(w.currency))
    return (s.recharge_min_usd * rate).quantize(exp, rounding=ROUND_DOWN), (s.recharge_max_usd * rate).quantize(exp, rounding=ROUND_DOWN)


def _check_amount(db: Session, w: Wallet, d: Decimal, field: str = "amount") -> None:
    lo, hi = bounds(db, w)
    if lo is not None and d < lo:
        raise RechargeError("amount_too_small", f"The smallest top-up is {money.format_micro(money.input_to_micro(lo, w.currency), w.currency)}")
    if hi is not None and d > hi:
        raise RechargeError("amount_too_large", f"The largest top-up is {money.format_micro(money.input_to_micro(hi, w.currency), w.currency)}")


def presets(w: Wallet) -> list[int]:
    try:
        return [int(x) for x in (json.loads(get_settings().wallet_presets_json).get(w.currency) or [])]
    except (ValueError, AttributeError):
        return []


def _fmt(micro: int, cur: str) -> str:
    return money.format_micro(micro, cur)


# --------------------------------------------------------------------------------------------------------------- views / settings
def _month_start(at: dt.datetime) -> dt.datetime:
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _used_this_month(db: Session, tid: str, at: dt.datetime) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(RechargeAttempt.amount_micro), 0)).where(
        RechargeAttempt.tenant_id == tid, RechargeAttempt.kind == "auto", RechargeAttempt.created_at >= _month_start(at),
        RechargeAttempt.status.in_(("creating", "awaiting_payment", "credited")))) or 0)


def view(db: Session, t: Tenant) -> dict:
    w = wl.get_wallet(db, t.id)
    a = db.get(AutoRecharge, t.id)
    lo, hi = bounds(db, w)
    cur = w.currency
    rows = db.scalars(select(RechargeAttempt).where(RechargeAttempt.tenant_id == t.id).order_by(RechargeAttempt.created_at.desc()).limit(10)).all()
    return {
        "currency": cur, "balance": _fmt(w.balance_micro, cur), "balance_micro": w.balance_micro, "owed": _fmt(w.receivable_micro, cur),
        "presets": presets(w), "min_amount": str(lo) if lo is not None else None, "max_amount": str(hi) if hi is not None else None,
        "auto": {
            "enabled": bool(a and a.enabled), "paused_reason": a.paused_reason if a else None,
            "threshold": str(money.micro_to_decimal(a.threshold_micro).normalize()) if a and a.threshold_micro else "",
            "amount": str(money.micro_to_decimal(a.amount_micro).normalize()) if a and a.amount_micro else "",
            "monthly_cap": str(money.micro_to_decimal(a.monthly_cap_micro).normalize()) if a and a.monthly_cap_micro else "",
            "used_this_month": _fmt(_used_this_month(db, t.id, now()), cur), "failures": a.consecutive_failures if a else 0,
        },
        "recent": [{"when": r.created_at.isoformat(), "kind": r.kind, "amount": _fmt(r.amount_micro, cur), "status": r.status,
                    "invoice_id": r.invoice_id, "error": r.error} for r in rows],
    }


def save_settings(db: Session, t: Tenant, actor: str, *, enabled: bool, threshold=None, amount=None, monthly_cap=None, client_id=None) -> AutoRecharge:
    w = wl.get_wallet(db, t.id)
    a = db.get(AutoRecharge, t.id) or AutoRecharge(tenant_id=t.id)
    if enabled:
        if not (client_id and str(client_id).isdigit()):
            raise RechargeError("no_client", "The WiseCP customer id is required to switch automatic recharge on")
        if not (t.wisecp_service_id and str(t.wisecp_service_id).isdigit()):
            raise RechargeError("no_service", "This workspace has no WiseCP service to bill")
        thr_d, thr = _parse_amount(db, w, threshold, "threshold")
        amt_d, amt = _parse_amount(db, w, amount, "amount")
        cap_d, cap = _parse_amount(db, w, monthly_cap, "monthly limit")
        _check_amount(db, w, amt_d)
        if cap < amt:
            raise RechargeError("cap_below_amount", "The monthly limit must be at least one recharge")
        a.threshold_micro, a.amount_micro, a.monthly_cap_micro = thr, amt, cap
        a.wisecp_client_id = str(client_id)
        a.consecutive_failures, a.paused_reason = 0, None
    a.enabled = bool(enabled)
    a.updated_by = actor
    db.add(a)
    ts.audit(db, actor, "auto_recharge.save", t.id, {"enabled": a.enabled, "threshold_micro": a.threshold_micro, "amount_micro": a.amount_micro,
                                                      "monthly_cap_micro": a.monthly_cap_micro})
    db.flush()
    return a


# --------------------------------------------------------------------------------------------------------------- Add Funds
def create_manual(db: Session, t: Tenant, wisecp: wc.WisecpClient, actor: str, amount, client_id) -> dict:
    w = wl.get_wallet(db, t.id)
    if not (client_id and str(client_id).isdigit()):
        raise RechargeError("no_client", "The WiseCP customer id is required")
    if not (t.wisecp_service_id and str(t.wisecp_service_id).isdigit()):
        raise RechargeError("no_service", "This workspace has no WiseCP service to bill")
    d, micro = _parse_amount(db, w, amount, "amount")
    _check_amount(db, w, d)
    att = RechargeAttempt(tenant_id=t.id, kind="manual", currency=w.currency, amount_micro=micro, status="creating")
    db.add(att); db.flush()
    try:
        inv = wisecp.create_topup_invoice(str(client_id), t.wisecp_service_id, w.currency, d, get_settings().wisecp_topup_description or "WhatsApp Prepaid Wallet Top-Up",
                                          pay_from_balance=False)
    except wc.WisecpError as e:
        att.status, att.error, att.settled_at = "failed", str(e)[:300], now()
        ts.audit(db, actor, "recharge.manual_failed", t.id, {"error": e.code}, outcome="failed")
        db.commit()
        raise RechargeError("invoice_failed", "The top-up invoice could not be created. Please try again in a moment.") from e
    att.status, att.invoice_id = "awaiting_payment", inv.invoice_id
    ts.audit(db, actor, "recharge.manual_invoice", t.id, {"invoice": inv.invoice_id, "amount_micro": micro})
    db.commit()
    return {"invoice_id": inv.invoice_id, "attempt_id": att.id, "amount": _fmt(micro, w.currency)}


# --------------------------------------------------------------------------------------------------------------- notifications
def _tell(db: Session, t: Tenant, kind: str, subject: str, text: str) -> None:
    """Email the workspace's notification addresses. Never raises: a mail problem must not stop money handling."""
    try:
        for addr in nt.conf(t).get("email", []):
            ok, why = nt.send_email(addr, subject, text)
            db.add(NotificationLog(tenant_id=t.id, kind=kind, channel="email", recipient=addr, status="sent" if ok else "failed",
                                   detail=None if ok else why[:400], subject=subject, content_text=text))
    except Exception:
        log.exception("recharge notification failed")


# --------------------------------------------------------------------------------------------------------------- the engine
def _fail(db: Session, t: Tenant, a: AutoRecharge | None, att: RechargeAttempt, why: str) -> None:
    att.status, att.error, att.settled_at = "failed", why[:300], now()
    if att.kind != "auto" or a is None:
        return
    a.consecutive_failures += 1
    cur = att.currency
    if a.consecutive_failures >= get_settings().recharge_max_failures:
        a.paused_reason = f"failed_{a.consecutive_failures}_times"
        _tell(db, t, "recharge_paused", "Automatic WhatsApp recharge is paused",
              f"We could not top up your WhatsApp credit automatically {a.consecutive_failures} times in a row, so automatic recharge is paused.\n"
              f"Please make sure your account balance covers {_fmt(att.amount_micro, cur)} and that Automatic Payments is on, then switch automatic "
              f"recharge on again from your service page.")
    else:
        _tell(db, t, "recharge_failed", "WhatsApp credit top-up failed",
              f"We tried to top up your WhatsApp credit by {_fmt(att.amount_micro, cur)} from your account balance but it did not go through. "
              f"Please make sure your account balance has enough funds. We will try again shortly.")


def settle_open(db: Session, wisecp: wc.WisecpClient, at: dt.datetime) -> dict:
    s = get_settings()
    out = {"credited": 0, "failed": 0, "waiting": 0}
    for att in db.scalars(select(RechargeAttempt).where(RechargeAttempt.status == "awaiting_payment").order_by(RechargeAttempt.created_at)).all():
        t = db.get(Tenant, att.tenant_id)
        a = db.get(AutoRecharge, att.tenant_id)
        age = at - (att.created_at if att.created_at.tzinfo else att.created_at.replace(tzinfo=dt.timezone.utc))
        try:
            inv = wisecp.get_invoice(att.invoice_id)
        except wc.WisecpError as e:
            out["waiting"] += 1
            log.warning("recharge %s: cannot read invoice %s (%s); will retry", att.id, att.invoice_id, e.code)
            continue
        if inv.status == "paid":
            res = wc.process_invoice(db, inv, "recharge")
            kinds = {r.get("result") for r in res.get("results", [])}
            if kinds & {"credited", "already_credited"}:
                att.status, att.settled_at = "credited", at
                if a is not None and att.kind == "auto":
                    a.consecutive_failures = 0
                    _tell(db, t, "recharge_ok", "WhatsApp credit topped up",
                          f"Your WhatsApp credit was topped up by {_fmt(att.amount_micro, att.currency)}. Thank you.")
                out["credited"] += 1
            else:
                att.status, att.error = "review", "paid, but the credit is held for staff review"
                ts.audit(db, "recharge", "recharge.review", att.tenant_id, {"invoice": att.invoice_id, "results": res.get("results")}, outcome="held")
        elif inv.status in ("cancelled", "refund"):
            _fail(db, t, a, att, f"invoice {inv.status}"); out["failed"] += 1
        elif att.kind == "auto" and age > dt.timedelta(minutes=s.recharge_pay_timeout_minutes):
            try:
                wisecp.cancel_invoice(att.invoice_id)
            except wc.WisecpError:
                log.warning("recharge %s: invoice %s could not be cancelled", att.id, att.invoice_id)
            _fail(db, t, a, att, "not paid from the account balance in time"); out["failed"] += 1
        elif att.kind == "manual" and age > dt.timedelta(hours=s.recharge_manual_expiry_hours):
            att.status, att.settled_at = "expired", at
        else:
            out["waiting"] += 1
    db.commit()
    return out


def start_due(db: Session, wisecp: wc.WisecpClient, at: dt.datetime) -> int:
    s = get_settings()
    started = 0
    for a in db.scalars(select(AutoRecharge).where(AutoRecharge.enabled.is_(True), AutoRecharge.paused_reason.is_(None))).all():
        t = db.get(Tenant, a.tenant_id)
        if t is None or t.status != "active" or not t.wisecp_service_id:
            continue
        w = wl.get_wallet(db, t.id)
        if w.balance_micro - w.receivable_micro - w.reserved_micro >= a.threshold_micro or a.amount_micro <= 0:
            continue
        last = db.scalar(select(func.max(RechargeAttempt.created_at)).where(RechargeAttempt.tenant_id == t.id, RechargeAttempt.kind == "auto"))
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=dt.timezone.utc)
            if at - last < dt.timedelta(minutes=s.recharge_min_gap_minutes):
                continue
        today = db.scalar(select(func.count()).select_from(RechargeAttempt).where(
            RechargeAttempt.tenant_id == t.id, RechargeAttempt.kind == "auto", RechargeAttempt.created_at >= at - dt.timedelta(days=1)))
        if (today or 0) >= s.recharge_daily_limit:
            continue
        if _used_this_month(db, t.id, at) + a.amount_micro > a.monthly_cap_micro:
            if a.paused_reason is None:
                _tell(db, t, "recharge_cap", "Monthly automatic recharge limit reached",
                      f"Your WhatsApp credit is low, but your monthly automatic recharge limit of {_fmt(a.monthly_cap_micro, w.currency)} has been reached. "
                      f"Top up manually or raise the limit from your service page.")
                a.paused_reason = "monthly_cap_reached"
                ts.audit(db, "recharge", "recharge.cap_reached", t.id, {})
                db.commit()
            continue
        att = RechargeAttempt(tenant_id=t.id, kind="auto", currency=w.currency, amount_micro=a.amount_micro, status="creating")
        db.add(att)
        try:
            db.flush()
        except IntegrityError:                    # another worker already has an open recharge for this tenant
            db.rollback()
            continue
        db.commit()                               # the open attempt is visible (and blocks a second one) before WiseCP is called
        try:
            inv = wisecp.create_topup_invoice(a.wisecp_client_id or "", t.wisecp_service_id, w.currency, money.micro_to_decimal(a.amount_micro),
                                              s.wisecp_topup_description or "WhatsApp Prepaid Wallet Top-Up", pay_from_balance=True)
            att.status, att.invoice_id = "awaiting_payment", inv.invoice_id
            ts.audit(db, "recharge", "recharge.invoice", t.id, {"invoice": inv.invoice_id, "amount_micro": a.amount_micro})
            started += 1
        except wc.WisecpError as e:
            _fail(db, t, a, att, f"{e.code}: {e}")
            ts.audit(db, "recharge", "recharge.failed", t.id, {"error": e.code}, outcome="failed")
        db.commit()
    return started


def run(db: Session, wisecp: wc.WisecpClient, at: dt.datetime | None = None) -> dict:
    at = at or now()
    res = settle_open(db, wisecp, at)
    res["started"] = start_due(db, wisecp, at)
    return res
