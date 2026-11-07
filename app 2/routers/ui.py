"""Staff admin UI (HTTP Basic) and the customer's read-only usage page (signed link)."""
import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jinja2 import DictLoader, Environment, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import money
from ..auth import staff as staff_auth
from ..config import get_settings
from ..db import get_db
from ..models import AdjustmentRequest, AuditLog, FxRate, LedgerEntry, RateCard, Tenant, now, uid
from ..services import tenants as ts
from ..services import wallet as wl
from ..services import adjustments as adj_svc, fx as fx_svc, jobs, lifecycle as lc, rates as rates_svc

router = APIRouter()

BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title><style>
body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#14171c}
header{background:#14171c;color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}
header a{color:#cfd6e4;text-decoration:none} main{max-width:1000px;margin:20px auto;padding:0 16px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eceef2} th{background:#f0f2f6;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card{background:#fff;border-radius:8px;padding:16px;margin:0 0 16px}.row{display:flex;gap:16px;flex-wrap:wrap}.row>.card{flex:1;min-width:200px}
.big{font-size:26px;font-weight:600}.pill{padding:2px 8px;border-radius:99px;font-size:12px;background:#e4e8ef}
.active{background:#d7f3df}.suspended,.cancelled{background:#fde0de}.zero,.critical{color:#b3261e}.low{color:#9a6700}
input,select,button{font:inherit;padding:7px 9px;border:1px solid #c9cfda;border-radius:6px} button{background:#14171c;color:#fff;cursor:pointer}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}.muted{color:#667}
</style></head><body><header><b>Citichoice Desk Hub</b>{% if staff %}<a href="/ui">Tenants</a><a href="/ui/rates">Rates &amp; FX</a><a href="/ui/adjustments">Adjustments</a><a href="/ui/audit">Audit</a><span style="margin-left:auto;color:#9aa4b8">{{ me }}</span><form method="post" action="/auth/logout" style="margin:0"><input type="hidden" name="csrf" value="{{ me_csrf }}"><button>Sign out</button></form>{% endif %}</header>
<main>{% block body %}{% endblock %}</main></body></html>"""

T_LIST = """{% extends "base" %}{% block body %}<h2>Tenants</h2><table><tr><th>Name</th><th>Plan</th><th>Status</th><th>Wallet</th><th>AI</th><th>Seats</th><th>Channels</th></tr>
{% for s in rows %}<tr><td><a href="/ui/tenants/{{ s.id }}">{{ s.name }}</a></td><td>{{ s.plan }}</td><td><span class="pill {{ s.status }}">{{ s.status }}{% if s.suspend_mode!='none' %} / {{ s.suspend_mode }}{% endif %}</span></td>
<td class="{{ s.wallet.alert or '' }}">{{ fmt(s.wallet.balance_micro, s.wallet.currency) }}</td><td>{{ s.ai_replies.used }}/{{ s.ai_replies.limit }}</td><td>{{ s.seats.used }}/{{ s.seats.limit }}</td><td>{{ s.channels.used }}/{{ s.channels.limit }}</td></tr>{% endfor %}</table>{% endblock %}"""

T_TENANT = """{% extends "base" %}{% block body %}<h2>{{ t.name }} <span class="pill {{ s.status }}">{{ s.status }}</span></h2>
<p class="muted">{{ t.owner_email }} &middot; WISECP service {{ t.wisecp_service_id }} &middot; plan {{ t.plan }} &middot; markup {{ t.markup }}x &middot; Chatwoot account {{ t.chatwoot_account_id or '-' }}</p>
<div class="row"><div class="card"><div class="muted">Wallet funds</div><div class="big {{ s.wallet.alert or '' }}">{{ fmt(s.wallet.balance_micro, s.wallet.currency) }}</div>
<div class="muted">Held for sends: {{ fmt(s.wallet.reserved_micro, s.wallet.currency) }} &middot; Credit limit: {{ s.wallet.credit_limit }}</div>
{% if s.wallet.receivable_micro %}<div class="critical"><b>Owes: {{ s.wallet.receivable }}</b></div>{% endif %}</div>
<div class="card"><div class="muted">AI replies this month</div><div class="big">{{ s.ai_replies.used }} / {{ s.ai_replies.limit }}</div></div>
<div class="card"><div class="muted">Seats / channels</div><div class="big">{{ s.seats.used }}/{{ s.seats.limit }} &middot; {{ s.channels.used }}/{{ s.channels.limit }}</div></div></div>
<div class="card"><b>Propose a money adjustment</b> <span class="muted">(a different staff member must approve it)</span>
<form class="inline" method="post" action="/ui/tenants/{{ t.id }}/adjustments"><input type="hidden" name="csrf" value="{{ csrf }}">
<select name="kind"><option value="wallet_credit">Credit funds</option><option value="wallet_debit">Take funds back</option><option value="receivable_writeoff">Write off debt</option><option value="manual_charge">Add a missed charge</option><option value="usage_reversal">Reverse one message charge</option></select>
<input name="amount" type="text" inputmode="decimal" placeholder="Amount in {{ s.wallet.currency }}"><input name="usage_id" placeholder="Message id (reversal only)">
<input name="reason" placeholder="Reason" required style="min-width:200px"><input name="evidence" placeholder="Evidence: ticket/invoice/email id" required style="min-width:200px"><button>Submit for approval</button></form></div>
<div class="card">
{% if t.status in ('active','suspended') %}<form class="inline" method="post" action="/ui/tenants/{{ t.id }}/{{ 'unsuspend' if t.status=='suspended' else 'suspend' }}"><input type="hidden" name="csrf" value="{{ csrf }}">
{% if t.status=='active' %}<select name="mode"><option value="pause_outbound">Pause outbound + AI</option><option value="lock">Lock workspace</option></select><button>Suspend</button>{% else %}<button>Unsuspend</button>{% endif %}</form>{% else %}<p class="muted">Status {{ t.status }}: lifecycle actions are handled through the admin API.</p>{% endif %}</div>
<div class="card"><b>This month</b><table><tr><th>Category</th><th>Messages</th><th>Provider cost</th><th>FX buffer</th><th>Service fee</th><th>Charged</th></tr>
{% for c,v in s.month_usage.items() %}<tr><td>{{ c }}</td><td>{{ v.messages }}</td><td>{{ fmt(v.provider_cost_micro, s.wallet.currency) }}</td><td>{{ fmt(v.fx_buffer_micro, s.wallet.currency) }}</td><td>{{ fmt(v.service_fee_micro, s.wallet.currency) }}</td><td>{{ fmt(v.charge_micro, s.wallet.currency) }}</td></tr>{% endfor %}</table></div>
<div class="card"><b>Ledger</b><table><tr><th>When</th><th>Type</th><th>Amount</th><th>Balance</th><th>Reference</th></tr>
{% for e in ledger %}<tr><td>{{ e.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ e.kind }}</td><td>{{ fmt(e.amount_micro, e.currency) }}</td><td>{{ fmt(e.balance_after_micro, e.currency) }}</td><td>{{ e.reference or '' }}</td></tr>{% endfor %}</table></div>{% endblock %}"""

T_RATES = """{% extends "base" %}{% block body %}<h2>Provider rate cards (what Meta charges per message)</h2>
<div class="card"><form class="inline" method="post" action="/ui/rates"><input type="hidden" name="csrf" value="{{ csrf }}">
<select name="category"><option>marketing</option><option>utility</option><option>authentication</option><option>service</option></select>
<input name="market" value="*" size="3" title="ISO country or *"><input name="currency" value="USD" size="4">
<input name="rate" type="text" inputmode="decimal" placeholder="Per message, e.g. 0.0067" required>
<input name="effective_from" type="date" required><input name="source" placeholder="Source (Meta rate card URL or file)" required style="min-width:240px"><button>Add new version</button></form>
<p class="muted">A new version closes the previous one on its start date. History is never edited and backdating is refused. Copy figures from Meta's current rate card, never from memory.</p></div>
<table><tr><th>Category</th><th>Market</th><th>Currency</th><th>Rate</th><th>From</th><th>To</th><th>Ver</th><th>Source</th></tr>
{% for r in rates %}<tr><td>{{ r.category }}</td><td>{{ r.market }}</td><td>{{ r.currency }}</td><td>{{ r.rate }}</td><td>{{ r.effective_from }}</td><td>{{ r.effective_to or 'open' }}</td><td>{{ r.version }}</td><td class="muted">{{ r.source }}</td></tr>{% endfor %}</table>
<h2>Exchange rates (1 base = rate quote)</h2><div class="card"><form class="inline" method="post" action="/ui/fx"><input type="hidden" name="csrf" value="{{ csrf }}">
<input name="base" value="USD" size="4"><input name="quote" size="4" placeholder="NGN" required><input name="rate" type="text" inputmode="decimal" placeholder="Rate" required><button>Add manual rate</button></form>
<p class="muted">Normally rates come from WiseCP (fx-sync). A move over the quarantine threshold is held below until you approve it.</p></div>
<table><tr><th>When</th><th>Pair</th><th>Rate</th><th>Source</th><th>Status</th><th></th></tr>{% for f in fx %}<tr><td>{{ f.effective_from.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ f.base_currency }}/{{ f.quote_currency }}</td><td>{{ f.rate }}</td><td class="muted">{{ f.source }}</td><td>{{ f.status }}</td>
<td>{% if f.status=='quarantined' and f.id not in approved %}<form method="post" action="/ui/fx/{{ f.id }}/approve" style="margin:0"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Approve</button></form>{% endif %}</td></tr>{% endfor %}</table>{% endblock %}"""

T_ADJ = """{% extends "base" %}{% block body %}<h2>Money adjustments</h2>
<p class="muted">Every manual change to a wallet or a debt needs a reason, evidence, and a second person's approval. Posted entries are never edited.</p>
<table><tr><th>Requested</th><th>Tenant</th><th>What</th><th>Amount</th><th>Reason / evidence</th><th>By</th><th>Status</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.requested_at.strftime('%Y-%m-%d %H:%M') }}</td><td><a href="/ui/tenants/{{ r.tenant_id }}">{{ names[r.tenant_id] }}</a></td><td>{{ kinds[r.kind] }}</td><td>{{ fmt(r.amount_micro, r.currency) }}</td>
<td>{{ r.reason }}<div class="muted">{{ r.evidence_ref }}</div></td><td>{{ r.requested_by }}</td>
<td>{{ r.status }}{% if r.decided_by %}<div class="muted">{{ r.decided_by }}{% if r.self_approved %} (self-approved, within the small-amount limit){% endif %}</div>{% endif %}</td>
<td>{% if r.status == 'pending' %}<form method="post" action="/ui/adjustments/{{ r.id }}/approve" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Approve</button></form>
<form method="post" action="/ui/adjustments/{{ r.id }}/reject" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf }}"><input name="note" placeholder="Why rejected"><button>Reject</button></form>{% endif %}</td></tr>{% endfor %}</table>{% endblock %}"""

T_AUDIT = """{% extends "base" %}{% block body %}<h2>Audit log</h2><table><tr><th>When</th><th>Actor</th><th>Action</th><th>Tenant</th><th>Detail</th></tr>
{% for a in rows %}<tr><td>{{ a.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ a.actor }}</td><td>{{ a.action }}</td><td>{{ a.tenant_id or '' }}</td><td class="muted">{{ a.detail }}</td></tr>{% endfor %}</table>{% endblock %}"""

T_USAGE = """{% extends "base" %}{% block body %}<h2>{{ t.name }}: messaging wallet</h2>
<div class="row"><div class="card"><div class="muted">Balance</div><div class="big {{ s.wallet.alert or '' }}">{{ fmt(s.wallet.balance_micro, s.wallet.currency) }}</div>
{% if topup %}<p><a href="{{ topup }}"><button>Top up</button></a></p>{% endif %}</div>
<div class="card"><div class="muted">AI replies this month</div><div class="big">{{ s.ai_replies.used }} / {{ s.ai_replies.limit }}</div></div></div>
{% if s.status!='active' %}<div class="card">Your workspace is suspended. Pay the open invoice to restore it.</div>{% endif %}
<div class="card"><b>This month</b><table><tr><th>Category</th><th>Messages</th><th>Provider cost</th><th>FX buffer</th><th>Service fee</th><th>Total</th></tr>
{% for c,v in s.month_usage.items() %}<tr><td>{{ c }}</td><td>{{ v.messages }}</td><td>{{ fmt(v.provider_cost_micro, s.wallet.currency) }}</td><td>{{ fmt(v.fx_buffer_micro, s.wallet.currency) }}</td><td>{{ fmt(v.service_fee_micro, s.wallet.currency) }}</td><td>{{ fmt(v.charge_micro, s.wallet.currency) }}</td></tr>{% endfor %}</table></div>
<div class="card"><b>Recent activity</b><table><tr><th>When</th><th>Type</th><th>Amount</th><th>Balance</th></tr>
{% for e in ledger %}<tr><td>{{ e.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ e.kind }}</td><td>{{ fmt(e.amount_micro, e.currency) }}</td><td>{{ fmt(e.balance_after_micro, e.currency) }}</td></tr>{% endfor %}</table></div>{% endblock %}"""

env = Environment(loader=DictLoader({"base": BASE, "list": T_LIST, "tenant": T_TENANT, "rates": T_RATES,
                                     "audit": T_AUDIT, "usage": T_USAGE, "adj": T_ADJ}),
                  autoescape=select_autoescape(default=True, default_for_string=True))
env.globals["fmt"] = money.format_micro


def render(name: str, user=None, **ctx) -> HTMLResponse:
    me = dict(me=str(user), me_csrf=user.csrf) if user is not None else {}
    return HTMLResponse(env.get_template(name).render(title="Citichoice Desk Hub", **me, **ctx))


staff = staff_auth.staff_session          # every /ui route: valid session AND the administrator role


def csrf_token(user) -> str:
    return user.csrf


def check_csrf(user, token: str) -> None:
    staff_auth.check_csrf(user, token)


def customer_api_only():
    if not get_settings().customer_api_enabled:
        raise HTTPException(404)


@router.get("/ui", response_class=HTMLResponse)
def ui_home(user: str = Depends(staff), db: Session = Depends(get_db)):
    rows = [ts.summary(db, t) for t in db.scalars(select(Tenant).where(Tenant.status != "deleted").order_by(Tenant.name))]
    return render("list", user, staff=True, rows=rows)


@router.get("/ui/tenants/{tid}", response_class=HTMLResponse)
def ui_tenant(tid: str, user: str = Depends(staff), db: Session = Depends(get_db)):
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    led = db.scalars(select(LedgerEntry).where(LedgerEntry.tenant_id == tid).order_by(LedgerEntry.created_at.desc()).limit(50)).all()
    return render("tenant", user, staff=True, t=t, s=ts.summary(db, t), ledger=led, csrf=csrf_token(user), key=uuid.uuid4().hex)


KIND_LABELS = {"wallet_credit": "Credit funds", "wallet_debit": "Take funds back", "receivable_writeoff": "Write off debt",
               "manual_charge": "Add a missed charge", "usage_reversal": "Reverse one message charge"}


@router.get("/ui/adjustments", response_class=HTMLResponse)
def ui_adjustments(user: str = Depends(staff), db: Session = Depends(get_db)):
    rows = db.scalars(select(AdjustmentRequest).order_by((AdjustmentRequest.status == "pending").desc(), AdjustmentRequest.requested_at.desc()).limit(200)).all()
    names = {t.id: t.name for t in db.scalars(select(Tenant).where(Tenant.id.in_({r.tenant_id for r in rows})))} if rows else {}
    return render("adj", user, staff=True, rows=rows, names=names, kinds=KIND_LABELS, csrf=csrf_token(user))


def _adj_error(e: adj_svc.AdjustmentError):
    return HTTPException(409 if e.code in ("not_pending", "needs_second_approver", "insufficient_funds", "exceeds_debt", "already_reversed",
                                           "currency_mismatch") else 422, {"code": e.code, "message": str(e)})


@router.post("/ui/tenants/{tid}/adjustments")
def ui_propose_adjustment(tid: str, kind: str = Form(...), amount: str = Form(""), usage_id: str = Form(""), reason: str = Form(""),
                          evidence: str = Form(""), csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    try:
        adj_svc.propose(db, t, kind=kind, amount=amount.strip() or None, usage_external_id=usage_id.strip() or None,
                        reason=reason, evidence_ref=evidence, actor=str(user))
    except adj_svc.AdjustmentError as e:
        raise _adj_error(e)
    db.commit()
    return RedirectResponse("/ui/adjustments", status_code=303)


@router.post("/ui/adjustments/{rid}/approve")
def ui_approve_adjustment(rid: str, csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    try:
        adj_svc.decide(db, rid, str(user), True)
    except adj_svc.AdjustmentError as e:
        db.rollback()
        raise _adj_error(e)
    db.commit()
    return RedirectResponse("/ui/adjustments", status_code=303)


@router.post("/ui/adjustments/{rid}/reject")
def ui_reject_adjustment(rid: str, note: str = Form(""), csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    try:
        adj_svc.decide(db, rid, str(user), False, note)
    except adj_svc.AdjustmentError as e:
        db.rollback()
        raise _adj_error(e)
    db.commit()
    return RedirectResponse("/ui/adjustments", status_code=303)


@router.post("/ui/tenants/{tid}/suspend")
def ui_suspend(tid: str, mode: str = Form("pause_outbound"), csrf: str = Form(...), user: str = Depends(staff),
               db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    if mode not in ("pause_outbound", "lock"):
        raise HTTPException(422)
    was = lc.chatwoot_locked(t)
    try:
        lc.suspend(t, mode, f"set by {user}")
    except lc.InvalidTransition as e:
        raise HTTPException(409, str(e))
    ts.audit(db, user, "tenant.ui_suspend", tid, {"mode": mode})
    if lc.chatwoot_locked(t) != was:
        jobs.enqueue(db, tid, "sync_chatwoot_status", f"cw-status:{tid}:{uid()}")
    db.commit()
    return RedirectResponse(f"/ui/tenants/{tid}", status_code=303)


@router.post("/ui/tenants/{tid}/unsuspend")
def ui_unsuspend(tid: str, csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    was = lc.chatwoot_locked(t)
    try:
        lc.unsuspend(t)
    except lc.InvalidTransition as e:
        raise HTTPException(409, str(e))
    ts.audit(db, user, "tenant.ui_unsuspend", tid)
    if was:
        jobs.enqueue(db, tid, "sync_chatwoot_status", f"cw-status:{tid}:{uid()}")
    db.commit()
    return RedirectResponse(f"/ui/tenants/{tid}", status_code=303)


@router.get("/ui/rates", response_class=HTMLResponse)
def ui_rates(user: str = Depends(staff), db: Session = Depends(get_db)):
    fx_rows = db.scalars(select(FxRate).order_by(FxRate.effective_from.desc()).limit(200)).all()
    approved = {f.approves_id for f in fx_rows if f.approves_id}
    return render("rates", user, staff=True, csrf=csrf_token(user), approved=approved, fx=fx_rows,
                  rates=db.scalars(select(RateCard).order_by(RateCard.category, RateCard.market, RateCard.effective_from.desc())).all())


@router.post("/ui/rates")
def ui_add_rate(category: str = Form(...), market: str = Form("*"), currency: str = Form(...), rate: str = Form(...),
                effective_from: str = Form(...), source: str = Form(...), csrf: str = Form(...),
                user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    try:
        card = rates_svc.add_rate(db, category=category, market=market, currency=currency, rate=Decimal(rate.strip()),
                                  effective_from=dt.date.fromisoformat(effective_from), source=source, actor=str(user))
    except (rates_svc.RateError, ArithmeticError, ValueError) as e:
        raise HTTPException(422, str(e))
    ts.audit(db, user, "rate.add", None, {"id": card.id, "category": card.category, "market": card.market,
                                          "currency": card.currency, "rate_micro": card.rate_micro, "version": card.version})
    db.commit()
    return RedirectResponse("/ui/rates", status_code=303)


@router.post("/ui/fx")
def ui_add_fx(base: str = Form("USD"), quote: str = Form(...), rate: str = Form(...), csrf: str = Form(...),
              user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    try:
        q = fx_svc.Quote(base.upper(), quote.upper(), Decimal(rate.strip()), "manual", now())
    except ArithmeticError:
        raise HTTPException(422, "bad rate")
    res = fx_svc.record_quotes(db, [q], str(user))
    ts.audit(db, user, "fx.add", None, {"pair": f"{q.base}/{q.quote}", "rate": str(q.rate), **res})
    db.commit()
    return RedirectResponse("/ui/rates", status_code=303)


@router.post("/ui/fx/{fx_id}/approve")
def ui_approve_fx(fx_id: str, csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    try:
        row = fx_svc.approve(db, fx_id, str(user))
    except fx_svc.FxError as e:
        raise HTTPException(409, str(e))
    ts.audit(db, user, "fx.approve", None, {"approved": fx_id, "new_row": row.id})
    db.commit()
    return RedirectResponse("/ui/rates", status_code=303)


@router.get("/ui/audit", response_class=HTMLResponse)
def ui_audit(user: str = Depends(staff), db: Session = Depends(get_db)):
    return render("audit", user, staff=True, rows=db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all())


@router.get("/u/{token}", response_class=HTMLResponse, dependencies=[Depends(customer_api_only)])
def usage_page(token: str, db: Session = Depends(get_db)):
    s = get_settings()
    try:
        tid = URLSafeTimedSerializer(s.secret_key, salt="usage").loads(token, max_age=86400)
    except BadSignature:
        raise HTTPException(404, "link expired")
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    led = db.scalars(select(LedgerEntry).where(LedgerEntry.tenant_id == tid).order_by(LedgerEntry.created_at.desc()).limit(30)).all()
    return render("usage", staff=False, t=t, s=ts.summary(db, t), ledger=led, topup=s.wisecp_topup_url)
