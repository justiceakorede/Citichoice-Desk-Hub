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

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>{{ title }}</title><style>
:root{--bg:#f4f6fa;--card:#fff;--ink:#12161f;--muted:#667085;--line:#e6e9f0;--head:#0f1420;--accent:#4f46e5;--ok:#d9f5e3;--okt:#116b3a;--bad:#fde4e1;--badt:#a3261a;--warn:#fff1d6;--warnt:#8a5a00}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b24;--ink:#e8ecf4;--muted:#98a2b3;--line:#262d3a;--head:#090c12;--accent:#818cf8;--ok:#12331f;--okt:#7be0a6;--bad:#3a1713;--badt:#ff9d92;--warn:#3a2c0b;--warnt:#f5c867}}
*{box-sizing:border-box}body{font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
header{background:var(--head);color:#fff;padding:10px 16px;display:flex;gap:6px 18px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
header b{margin-right:8px}header a{color:#c4cce0;text-decoration:none;padding:6px 10px;border-radius:8px}header a:hover{background:#ffffff1a;color:#fff}
main{max-width:1180px;margin:20px auto;padding:0 14px 40px}h2{font-size:20px;margin:22px 0 10px}
.tablewrap,main>table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}td{white-space:normal}th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:transparent}
tr:last-child td{border-bottom:0}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 16px;box-shadow:0 1px 2px #0000000a}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.row>.card{margin:0}
.big{font-size:26px;font-weight:650;letter-spacing:-.01em}.muted{color:var(--muted)}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;background:var(--line);color:var(--ink)}
.active{background:var(--ok);color:var(--okt)}.suspended,.cancelled{background:var(--bad);color:var(--badt)}.pending{background:var(--warn);color:var(--warnt)}
.zero,.critical{color:var(--badt)}.low{color:var(--warnt)}
input,select,button,textarea{font:inherit;padding:8px 11px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);max-width:100%}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
button{background:var(--accent);border-color:var(--accent);color:#fff;cursor:pointer;font-weight:550}button:hover{filter:brightness(1.08)}button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;align-items:end}.grid2 label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}.grid2 input,.grid2 select{width:100%}
@media(max-width:640px){main{margin-top:12px}.big{font-size:22px}th,td{padding:8px}}
header .links{display:flex;gap:6px 14px;align-items:center;flex-wrap:wrap;flex:1}header .who{margin-left:auto;color:#9aa4b8}
.navtoggle{display:none}.burger{display:none;margin-left:auto;font-size:24px;line-height:1;cursor:pointer;padding:2px 10px;border-radius:8px;color:#fff}.burger:hover{background:#ffffff1a}
@media(max-width:820px){.burger{display:block}header .links{display:none;flex-direction:column;align-items:stretch;width:100%;flex:none;gap:2px;padding:6px 0}.navtoggle:checked ~ .links{display:flex}
header .links a{padding:10px 12px}header .who{margin:6px 12px}header .links form{margin:4px 12px !important}main{padding:0 10px 40px}.row{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.row{grid-template-columns:1fr}h2{font-size:18px}.card{padding:14px}}
.kpi{font-size:28px;font-weight:650;letter-spacing:-.02em}.kpi small{font-size:13px;font-weight:500;color:var(--muted)}
</style></head><body><header><b>Citichoice Desk Hub</b>{% if staff %}<input type="checkbox" id="navtoggle" class="navtoggle" aria-label="Menu"><label for="navtoggle" class="burger" title="Menu">&#9776;</label>
<nav class="links"><a href="/ui/overview">Overview</a><a href="/ui">Tenants</a><a href="/ui/plans">Plans</a><a href="/ui/rates">Rates &amp; FX</a><a href="/ui/adjustments">Adjustments</a><a href="/ui/audit">Audit</a>
<span class="who">{{ me }}</span><form method="post" action="/auth/logout" style="margin:0"><input type="hidden" name="csrf" value="{{ me_csrf }}"><button class="ghost" style="color:#fff;border-color:#ffffff40">Sign out</button></form></nav>{% endif %}</header>
<main>{% block body %}{% endblock %}</main></body></html>"""

T_OVERVIEW = """{% extends "base" %}{% block body %}<h2>Overview</h2>
<div class="row">
<div class="card"><div class="muted">Customers</div><div class="kpi">{{ counts.get('active', 0) }} <small>active</small></div><div class="muted">{{ counts.get('suspended', 0) }} suspended &middot; {{ counts.get('cancelling', 0) + counts.get('cancelled', 0) }} cancelled &middot; {{ counts.get('provisioning_failed', 0) + counts.get('pending', 0) }} need attention</div></div>
<div class="card"><div class="muted">Messages this month</div><div class="kpi">{{ '{:,}'.format(msgs) }}</div><div class="muted">billed WhatsApp messages (all customers)</div></div>
<div class="card"><div class="muted">Waiting for you</div><div class="kpi">{{ pending_adj + bad_jobs }}</div><div class="muted"><a href="/ui/adjustments">{{ pending_adj }} adjustment(s)</a> &middot; {{ bad_jobs }} stuck job(s)</div></div>
<div class="card"><div class="muted">Last runs</div><div>FX rates: <b>{{ fx_at }}</b></div><div>Meta billing: <b>{{ meta_at }}</b></div></div>
</div>
<h2>Customer wallets</h2><div class="tablewrap"><table><tr><th>Currency</th><th>Customers</th><th>Total credit</th><th>Total owed</th><th>Spent this month</th></tr>
{% for r in wallets %}<tr><td>{{ r.cur }}</td><td>{{ r.n }}</td><td>{{ r.balance }}</td><td>{{ r.owed }}</td><td>{{ r.spent }}</td></tr>{% else %}<tr><td colspan="5" class="muted">No wallets yet.</td></tr>{% endfor %}</table></div>
<h2>Needs attention</h2><div class="tablewrap"><table><tr><th>Customer</th><th>Status</th><th>Credit</th><th>Alert</th></tr>
{% for r in attention %}<tr><td><a href="/ui/tenants/{{ r.id }}">{{ r.name }}</a></td><td><span class="pill {{ r.status }}">{{ r.status }}</span></td><td>{{ r.balance }}</td><td class="{{ r.alert }}">{{ r.alert }}</td></tr>{% else %}<tr><td colspan="4" class="muted">Everyone is fine.</td></tr>{% endfor %}</table></div>{% endblock %}"""

T_PLANS = """{% extends "base" %}{% block body %}<h2>Plans</h2>
<p class="muted">What each plan allows. Changing a plan applies to every customer on it who has no custom override, and updates their Chatwoot limits within a minute. Leave a number blank only if every customer on that plan has an override.</p>
{% for p in plans %}<div class="card"><form method="post" action="/ui/plans/{{ p.code }}">
<input type="hidden" name="csrf" value="{{ csrf }}"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px"><b>{{ p.code }}</b><span class="muted">{{ counts.get(p.id, 0) }} customer(s) on this plan{% if not p.active %} &middot; <span class="pill suspended">inactive</span>{% endif %}</span></div>
<div class="grid2"><div><label>Display name</label><input name="name" value="{{ p.name }}" required></div>
<div><label>Seats (agents)</label><input name="seats" inputmode="numeric" value="{{ p.seats if p.seats is not none else '' }}"></div>
<div><label>Channels</label><input name="channels" inputmode="numeric" value="{{ p.channels if p.channels is not none else '' }}"></div>
<div><label>AI replies / month</label><input name="ai_replies" inputmode="numeric" value="{{ p.ai_replies if p.ai_replies is not none else '' }}"></div>
<div><label>Default markup (x)</label><input name="default_markup" inputmode="decimal" value="{{ p.default_markup if p.default_markup is not none else '' }}" placeholder="e.g. 1.25"></div>
<div><label>Knowledge base level</label><select name="kb_level">{% for k in ('basic','standard','advanced') %}<option {{ 'selected' if p.kb_level==k }}>{{ k }}</option>{% endfor %}</select></div>
<div><label>Status</label><select name="active"><option value="1" {{ 'selected' if p.active }}>Active</option><option value="0" {{ 'selected' if not p.active }}>Inactive</option></select></div>
<div><button>Save {{ p.code }}</button></div></div></form></div>{% endfor %}
<h2>New plan</h2><div class="card"><form method="post" action="/ui/plans"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="grid2"><div><label>Code (lowercase)</label><input name="code" pattern="[a-z0-9_-]{2,40}" required placeholder="enterprise"></div><div><label>Display name</label><input name="name" required></div>
<div><label>Seats</label><input name="seats" inputmode="numeric"></div><div><label>Channels</label><input name="channels" inputmode="numeric"></div><div><label>AI replies / month</label><input name="ai_replies" inputmode="numeric"></div>
<div><label>Default markup (x)</label><input name="default_markup" inputmode="decimal"></div><div><button>Create plan</button></div></div></form></div>{% endblock %}"""

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
{% for e in ledger %}<tr><td>{{ e.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ e.kind }}</td><td>{{ fmt(e.amount_micro, e.currency) }}</td><td>{{ fmt(e.balance_after_micro, e.currency) }}</td><td>{{ e.reference or '' }}</td></tr>{% endfor %}</table></div><h2>AI replies: manual correction</h2><div class="card"><form class="inline" method="post" action="/ui/tenants/{{ t.id }}/ai-adjust"><input type="hidden" name="csrf" value="{{ csrf }}">
<input name="delta" inputmode="numeric" placeholder="+50 or -20" required style="width:130px"><input name="reason" placeholder="Reason (kept in the audit)" required style="min-width:260px"><button>Apply</button></form>
<p class="muted">Adds to (or with a minus sign gives back) this month's AI-reply count. New replies keep counting normally on top of it.</p></div>
<h2>Customer notifications</h2><div class="card"><form method="post" action="/ui/tenants/{{ t.id }}/notify">{% set n = t.notify or {} %}
<input type="hidden" name="csrf" value="{{ csrf }}"><div class="grid2">
<div><label><input type="checkbox" name="report" value="1" {{ 'checked' if n.get('report_enabled') }}> Weekly report</label></div>
<div><label><input type="checkbox" name="low" value="1" {{ 'checked' if n.get('low_balance_enabled') }}> Low-balance alert</label></div>
<div><label>WhatsApp numbers (digits, comma separated)</label><input name="whatsapp" value="{{ (n.get('whatsapp') or [])|join(', ') }}" placeholder="2348012345678"></div>
<div><label>Emails (comma separated)</label><input name="email" value="{{ (n.get('email') or [])|join(', ') }}"></div>
<div><button>Save notifications</button></div></div>
<p class="muted">Only add numbers and emails that agreed to receive these. Sent from your own WhatsApp number. Last report week: {{ (n.get('state') or {}).get('report_week', 'none yet') }}.</p></form></div>
{% if t.status in ('pending','provisioning_failed','cancelling','cancelled','terminated') %}<h2>Delete tenant</h2><div class="card"><form method="post" action="/ui/tenants/{{ t.id }}/delete" onsubmit="return confirm('Permanently delete this tenant?')">
<input type="hidden" name="csrf" value="{{ csrf }}"><div class="grid2"><div><label>Type the slug <b>{{ t.slug }}</b> to confirm</label><input name="confirm" autocomplete="off" required></div><div><label>Only if it has money history (test tenants): type DELETE-HISTORY</label><input name="purge" autocomplete="off"></div><div><button style="background:#c0392b;border-color:#c0392b">Delete permanently</button></div></div>
<p class="muted">Removes this tenant, its wallet, channels and settings. Refused if it has money history (kept for accounting) unless you type DELETE-HISTORY, which erases that history too. The Chatwoot account is not deleted. The audit trail is kept.</p></form></div>{% endif %}
{% endblock %}"""

T_RATES = """{% extends "base" %}{% block body %}<h2>Provider rate cards (what Meta charges per message)</h2>
<div class="card"><form class="inline" method="post" action="/ui/rates"><input type="hidden" name="csrf" value="{{ csrf }}">
<select name="category"><option>marketing</option><option>utility</option><option>authentication</option><option>service</option></select>
<input name="market" value="*" size="3" title="ISO country or *"><input name="currency" value="USD" size="4">
<input name="rate" type="text" inputmode="decimal" placeholder="Per message, e.g. 0.0067" required>
<input name="effective_from" type="date" required><input name="source" placeholder="Source (Meta rate card URL or file)" required style="min-width:240px"><button>Add new version</button></form>
<p class="muted">A new version closes the previous one on its start date. History is never edited and backdating is refused. Copy figures from Meta's current rate card, never from memory.</p></div>
<div class="card"><form class="inline" method="get" action="/ui/rates"><input name="q" value="{{ q }}" placeholder="Filter: market or category, e.g. NG or utility" style="min-width:260px">
<select name="per">{% for o in ('10','20','50','all') %}<option value="{{ o }}" {{ 'selected' if o==per }}>{{ 'All' if o=='all' else o ~ ' per page' }}</option>{% endfor %}</select><button class="ghost">Apply</button>
<span class="muted">{{ total }} rate version(s){% if pages > 1 %} &middot; page {{ page }} of {{ pages }}{% endif %}</span>
{% if page > 1 %}<a href="/ui/rates?q={{ q|urlencode }}&per={{ per }}&page={{ page-1 }}">&laquo; Prev</a>{% endif %}{% if page < pages %}<a href="/ui/rates?q={{ q|urlencode }}&per={{ per }}&page={{ page+1 }}">Next &raquo;</a>{% endif %}</form></div>
<table><tr><th>Category</th><th>Market</th><th>Currency</th><th>Rate</th><th>From</th><th>To</th><th>Ver</th><th>Source</th></tr>
{% for r in rates %}<tr><td>{{ r.category }}</td><td>{{ r.market }}</td><td>{{ r.currency }}</td><td>{{ r.rate_micro|rate }}</td><td>{{ r.effective_from }}</td><td>{{ r.effective_to or 'open' }}</td><td>{{ r.version }}</td><td class="muted">{{ r.source }}</td></tr>{% endfor %}</table>
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

def _rate_txt(micro) -> str:
    t = f"{Decimal(int(micro or 0)) / Decimal(1_000_000):f}".rstrip("0").rstrip(".")
    return t or "0"


env = Environment(loader=DictLoader({"base": BASE, "list": T_LIST, "tenant": T_TENANT, "rates": T_RATES, "plans": T_PLANS, "overview": T_OVERVIEW,
                                     "audit": T_AUDIT, "usage": T_USAGE, "adj": T_ADJ}),
                  autoescape=select_autoescape(default=True, default_for_string=True))
env.filters["rate"] = _rate_txt
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


@router.post("/ui/tenants/{tid}/notify")
def ui_tenant_notify(tid: str, report: str = Form(""), low: str = Form(""), whatsapp: str = Form(""), email: str = Form(""), csrf: str = Form(...),
                     user: str = Depends(staff), db: Session = Depends(get_db)):
    from ..services import notify as nf
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    nf.set_conf(t, report=report == "1", low=low == "1", whatsapp=[x for x in whatsapp.split(",") if x.strip()], email=[x for x in email.split(",") if x.strip()])
    ts.audit(db, user, "tenant.notify_set", t.id, {"report": t.notify["report_enabled"], "low": t.notify["low_balance_enabled"],
                                                   "whatsapp": len(t.notify["whatsapp"]), "email": len(t.notify["email"])})
    db.commit()
    return RedirectResponse(f"/ui/tenants/{tid}", status_code=303)


@router.post("/ui/tenants/{tid}/delete")
def ui_tenant_delete(tid: str, confirm: str = Form(""), purge: str = Form(""), csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    try:
        ts.delete_tenant(db, t, user, confirm.strip(), purge_history=purge.strip() == "DELETE-HISTORY")
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return RedirectResponse("/ui", status_code=303)


@router.post("/ui/tenants/{tid}/ai-adjust")
def ui_ai_adjust(tid: str, delta: str = Form(...), reason: str = Form(...), csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    from ..services import ai_allowance
    check_csrf(user, csrf)
    t = db.get(Tenant, tid) or (_ for _ in ()).throw(HTTPException(404))
    try:
        d = int(delta.strip().replace("+", ""))
    except ValueError:
        raise HTTPException(422, "delta must be a whole number such as 50 or -20")
    if d == 0 or abs(d) > 1_000_000:
        raise HTTPException(422, "delta must be between -1,000,000 and 1,000,000 and not zero")
    try:
        c, created, used = ai_allowance.adjust(db, t.id, d, f"ui:{uid()}", reason.strip()[:200])
    except ValueError as e:
        raise HTTPException(409, str(e))
    ts.audit(db, user, "ai.adjust", t.id, {"delta": d, "reason": reason.strip()[:200], "period": c.period, "used": used})
    db.commit()
    return RedirectResponse(f"/ui/tenants/{tid}", status_code=303)


@router.get("/ui/overview", response_class=HTMLResponse)
def ui_overview(user: str = Depends(staff), db: Session = Depends(get_db)):
    import datetime as dt_
    from sqlalchemy import func
    from .. import money
    from ..models import AdjustmentRequest, ProvisioningJob, UsageRecord, Wallet
    from ..services import tenants as ts_
    from ..services.periods import period_start_utc
    counts = dict(db.execute(select(Tenant.status, func.count()).group_by(Tenant.status)).all())
    start = period_start_utc(now())
    msgs = int(db.scalar(select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(UsageRecord.created_at >= start)) or 0)
    spent = dict(db.execute(select(UsageRecord.customer_currency, func.coalesce(func.sum(UsageRecord.charge_micro), 0)).where(UsageRecord.created_at >= start)
                            .group_by(UsageRecord.customer_currency)).all())
    wallets = []
    for cur, n, bal, owed in db.execute(select(Wallet.currency, func.count(), func.coalesce(func.sum(Wallet.balance_micro), 0), func.coalesce(func.sum(Wallet.receivable_micro), 0))
                                         .group_by(Wallet.currency)).all():
        wallets.append({"cur": cur, "n": n, "balance": money.format_micro(int(bal), cur), "owed": money.format_micro(int(owed), cur),
                        "spent": money.format_micro(int(spent.get(cur, 0)), cur)})
    attention = []
    for t in db.scalars(select(Tenant).where(Tenant.status.not_in(("terminated", "cancelled")))).all():
        try:
            w = ts_.customer_view(db, t)["wallet"]
        except Exception:
            continue
        if t.status != "active" or (w["alert"] or "ok") != "ok":
            attention.append({"id": t.id, "name": t.name, "status": t.status, "balance": w["balance"], "alert": w["alert"] or "ok"})
    fx_row = db.scalar(select(AuditLog).where(AuditLog.action == "fx.sync").order_by(AuditLog.created_at.desc()).limit(1))
    mb_row = db.scalar(select(AuditLog).where(AuditLog.action == "meta.billing_sync").order_by(AuditLog.created_at.desc()).limit(1))
    fmt = lambda r: r.created_at.strftime("%Y-%m-%d %H:%M UTC") if r else "never"
    return render("overview", user, staff=True, counts=counts, msgs=msgs, wallets=wallets, attention=attention, fx_at=fmt(fx_row), meta_at=fmt(mb_row),
                  pending_adj=db.scalar(select(func.count()).select_from(AdjustmentRequest).where(AdjustmentRequest.status == "pending")) or 0,
                  bad_jobs=db.scalar(select(func.count()).select_from(ProvisioningJob).where(ProvisioningJob.status.in_(("blocked", "dead")))) or 0)


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
def ui_rates(q: str = "", per: str = "20", page: int = 1, user: str = Depends(staff), db: Session = Depends(get_db)):
    from sqlalchemy import func, or_
    fx_rows = db.scalars(select(FxRate).order_by(FxRate.effective_from.desc()).limit(200)).all()
    approved = {f.approves_id for f in fx_rows if f.approves_id}
    base = select(RateCard)
    qq = q.strip()
    if qq:
        base = base.where(or_(RateCard.market == qq.upper(), RateCard.category == qq.lower()))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    per = per if per in ("10", "20", "50", "all") else "20"
    size = total or 1 if per == "all" else int(per)
    pages = max(1, -(-total // size)) if total else 1
    page = min(max(1, page), pages)
    rows = db.scalars(base.order_by(RateCard.category, RateCard.market, RateCard.effective_from.desc()).offset((page - 1) * size).limit(size)).all()
    return render("rates", user, staff=True, csrf=csrf_token(user), approved=approved, fx=fx_rows, rates=rows, q=qq, per=per, page=page,
                  pages=pages, total=total)


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


def _int_or_none(v: str, name: str):
    v = (v or "").strip()
    if v == "":
        return None
    if not v.isdigit() or int(v) > 10_000_000:
        raise HTTPException(422, f"{name} must be a whole number, or blank")
    return int(v)


def _markup_or_none(v: str):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        d = Decimal(v)
    except ArithmeticError:
        raise HTTPException(422, "markup must be a number such as 1.25")
    if not (Decimal("1") <= d <= Decimal("50")):
        raise HTTPException(422, "markup must be between 1 and 50")
    return d


@router.get("/ui/plans", response_class=HTMLResponse)
def ui_plans(user: str = Depends(staff), db: Session = Depends(get_db)):
    from sqlalchemy import func
    from ..models import Plan, TenantEntitlement
    plans = db.scalars(select(Plan).order_by(Plan.created_at)).all()
    counts = dict(db.execute(select(TenantEntitlement.plan_id, func.count()).group_by(TenantEntitlement.plan_id)).all())
    return render("plans", user, staff=True, plans=plans, counts=counts, csrf=csrf_token(user))


@router.post("/ui/plans/{code}")
def ui_plan_save(code: str, name: str = Form(...), seats: str = Form(""), channels: str = Form(""), ai_replies: str = Form(""),
                 default_markup: str = Form(""), kb_level: str = Form("basic"), active: str = Form("1"), csrf: str = Form(...),
                 user: str = Depends(staff), db: Session = Depends(get_db)):
    from ..models import Plan, TenantEntitlement
    check_csrf(user, csrf)
    p = db.scalar(select(Plan).where(Plan.code == code)) or (_ for _ in ()).throw(HTTPException(404))
    if kb_level not in ("basic", "standard", "advanced"):
        raise HTTPException(422, "bad knowledge base level")
    before = {"seats": p.seats, "channels": p.channels, "ai_replies": p.ai_replies, "markup": str(p.default_markup), "active": p.active}
    p.name, p.seats, p.channels = name.strip()[:100] or p.name, _int_or_none(seats, "seats"), _int_or_none(channels, "channels")
    p.ai_replies, p.default_markup, p.kb_level, p.active = _int_or_none(ai_replies, "AI replies"), _markup_or_none(default_markup), kb_level, active == "1"
    for t in db.scalars(select(Tenant).join(TenantEntitlement, TenantEntitlement.tenant_id == Tenant.id)
                        .where(TenantEntitlement.plan_id == p.id, Tenant.status.in_(("active", "suspended")))):
        jobs.enqueue(db, t.id, "sync_limits", f"sync-limits:{t.id}:{uid()}")
    ts.audit(db, user, "plan.update", None, {"code": p.code, "before": before, "after": {"seats": p.seats, "channels": p.channels,
             "ai_replies": p.ai_replies, "markup": str(p.default_markup), "active": p.active}})
    db.commit()
    return RedirectResponse("/ui/plans", status_code=303)


@router.post("/ui/plans")
def ui_plan_create(code: str = Form(...), name: str = Form(...), seats: str = Form(""), channels: str = Form(""), ai_replies: str = Form(""),
                   default_markup: str = Form(""), csrf: str = Form(...), user: str = Depends(staff), db: Session = Depends(get_db)):
    import re
    from ..models import Plan
    check_csrf(user, csrf)
    code = code.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", code) or db.scalar(select(Plan).where(Plan.code == code)):
        raise HTTPException(422, "plan code must be 2-40 lowercase letters, digits, - or _, and not already used")
    p = Plan(code=code, name=name.strip()[:100], seats=_int_or_none(seats, "seats"), channels=_int_or_none(channels, "channels"),
             ai_replies=_int_or_none(ai_replies, "AI replies"), default_markup=_markup_or_none(default_markup), currency="USD")
    db.add(p)
    ts.audit(db, user, "plan.create", None, {"code": code})
    db.commit()
    return RedirectResponse("/ui/plans", status_code=303)


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
