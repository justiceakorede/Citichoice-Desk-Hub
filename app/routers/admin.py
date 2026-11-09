import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import money
from ..config import get_settings
from ..db import get_db
from ..models import (AiConsumption, Currency, FxRate, ReceivableEntry, LedgerEntry, Plan, ProvisioningJob, RateCard, Tenant, TenantChannel, TenantMember,
                      Wallet, now, uid)
from ..schemas import (AiAdjustBody, ChannelCreate, ChannelPatch, CreditBody, CreditLimitBody, CurrencyPatch, FxBody, MemberBody, PlanBody, PlanPatch,
                       RateBody, RetryBody, SuspendBody, TenantCreate, TenantPatch)
from ..security import admin_key
from ..services import ai_allowance
from ..services import channels as chs
from ..services import fx as fx_svc
from ..services import pricing
from ..services import rates as rates_svc
from ..services import entitlements as ent
from ..services import jobs
from ..services import lifecycle as lc
from ..services import tenants as ts
from ..services import wallet as wl
from ..services.chatwoot import ChatwootClient, ProvisioningError
from ..services import stats as stats_svc
from ..services import wisecp as wisecp_svc
from ..services.chatwoot_reports import ChatwootReports

router = APIRouter(prefix="/admin", dependencies=[Depends(admin_key)])


def get_reports() -> ChatwootReports:
    s = get_settings()
    return ChatwootReports(s.chatwoot_url, s.chatwoot_report_token, s.stats_http_timeout)


def get_wisecp() -> wisecp_svc.WisecpClient:
    return wisecp_svc.get_wisecp()


def get_chatwoot() -> ChatwootClient:
    s = get_settings()
    return ChatwootClient(s.chatwoot_url, s.chatwoot_platform_token)


def _t(db: Session, tid: str) -> Tenant:
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, {"code": "tenant_not_found", "message": "tenant not found"})
    return t


def _lifecycle(fn, *a):
    try:
        return fn(*a)
    except lc.InvalidTransition as e:
        raise HTTPException(409, {"code": "invalid_transition", "message": str(e), "from": e.frm, "to": e.to})


def _sync_status(db: Session, t: Tenant):
    jobs.enqueue(db, t.id, "sync_chatwoot_status", f"cw-status:{t.id}:{uid()}")


def _sync_limits(db: Session, t: Tenant):
    jobs.enqueue(db, t.id, "sync_limits", f"sync-limits:{t.id}:{uid()}")


@router.post("/tenants")
def create_tenant(body: TenantCreate, request: Request, db: Session = Depends(get_db)):
    existing = db.scalar(select(Tenant).where(Tenant.wisecp_service_id == body.wisecp_service_id))
    if existing:
        if existing.billing_currency != body.billing_currency:      # never silently reinterpret an existing wallet
            raise HTTPException(409, {"code": "currency_mismatch", "message": f"tenant is billed in {existing.billing_currency}"})
        return {"created": False, "tenant": ts.summary(db, existing)}
    cur = db.get(Currency, body.billing_currency)
    if not cur or cur.exponent is None or not cur.billing_enabled:
        raise HTTPException(422, {"code": "currency_not_enabled", "message": f"{body.billing_currency} is not enabled for billing"})
    plan = ent.get_plan(db, body.plan)
    t = Tenant(wisecp_service_id=body.wisecp_service_id, name=body.name, slug=ts.slugify(body.name),
               owner_email=body.owner_email, owner_name=body.owner_name, status="pending", billing_currency=cur.code,
               chatwoot_account_id=body.chatwoot_account_id, chatwoot_user_id=body.chatwoot_user_id)
    db.add(t)
    db.flush()
    ent.create_entitlement(db, t, plan, body.model_dump())
    db.add(Wallet(tenant_id=t.id, currency=cur.code, balance_micro=0))
    jobs.enqueue(db, t.id, "provision_tenant", f"provision:{t.id}", {"create_owner_user": body.create_owner_user})
    ts.audit(db, request.state.actor, "tenant.create", t.id,
             {"plan": plan.code, "adopted_account": bool(body.chatwoot_account_id), "entitlement": ent.snapshot(t)})
    db.commit()
    return {"created": True, "tenant": ts.summary(db, t)}


@router.get("/tenants")
def list_tenants(wisecp_service_id: str | None = None, status: str | None = None, q: str | None = None,
                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    where = []
    if wisecp_service_id: where.append(Tenant.wisecp_service_id == wisecp_service_id)
    if status: where.append(Tenant.status == status)
    if q: where.append(or_(Tenant.name.ilike(f"%{q}%"), Tenant.slug.ilike(f"%{q}%"), Tenant.owner_email.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(Tenant).where(*where))
    rows = db.scalars(select(Tenant).where(*where).order_by(Tenant.created_at).limit(limit).offset(offset)).unique().all()
    return {"items": [ts.summary(db, t) for t in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/tenants/{tid}")
@router.get("/tenants/{tid}/summary")
def tenant_summary(tid: str, db: Session = Depends(get_db)):
    return ts.summary(db, _t(db, tid))


@router.patch("/tenants/{tid}")
def patch_tenant(tid: str, body: TenantPatch, request: Request, db: Session = Depends(get_db)):
    t = _t(db, tid)
    if t.status in ("cancelled", "terminated"):
        raise HTTPException(409, {"code": "tenant_closed", "message": "tenant is closed"})
    before, after = ent.apply_change(db, t, body.model_dump(exclude_unset=True))
    ts.audit(db, request.state.actor, "tenant.entitlement_change", t.id, {"before": before, "after": after})
    _sync_limits(db, t)
    db.commit()
    return ts.summary(db, t)


@router.post("/tenants/{tid}/suspend")
def suspend(tid: str, body: SuspendBody, request: Request, db: Session = Depends(get_db)):
    t = _t(db, tid)
    was_locked = lc.chatwoot_locked(t)
    _lifecycle(lc.suspend, t, body.mode, body.reason)
    ts.audit(db, request.state.actor, "tenant.suspend", t.id, body.model_dump())
    if lc.chatwoot_locked(t) != was_locked:
        _sync_status(db, t)
    db.commit()
    return ts.summary(db, t)


@router.post("/tenants/{tid}/unsuspend")
def unsuspend(tid: str, request: Request, db: Session = Depends(get_db)):
    t = _t(db, tid)
    was_locked = lc.chatwoot_locked(t)
    _lifecycle(lc.unsuspend, t)
    ts.audit(db, request.state.actor, "tenant.unsuspend", t.id)
    if was_locked:
        _sync_status(db, t)
    db.commit()
    return ts.summary(db, t)


@router.post("/tenants/{tid}/cancel")
def cancel(tid: str, request: Request, db: Session = Depends(get_db)):
    """Subscription cancelled: the tenant is locked and the data kept. Ends in 'cancelled' once Chatwoot is locked."""
    t = _t(db, tid)
    needs_lock = _lifecycle(lc.cancel, t)
    ts.audit(db, request.state.actor, "tenant.cancel", t.id, {"status": t.status})
    if needs_lock:
        _sync_status(db, t)
    db.commit()
    return ts.summary(db, t)


@router.post("/tenants/{tid}/terminate")
def terminate(tid: str, confirm: str, request: Request, db: Session = Depends(get_db)):
    """Final state. Keeps the tenant row, wallet, ledger and the (locked) Chatwoot account.
    Physical deletion of Chatwoot data is not implemented until a retention policy exists."""
    t = _t(db, tid)
    if confirm != t.slug:
        raise HTTPException(400, {"code": "confirmation_required", "message": "pass confirm=<tenant slug>"})
    _lifecycle(lc.terminate, t)
    ts.audit(db, request.state.actor, "tenant.terminate", t.id)
    db.commit()
    return ts.summary(db, t)


@router.post("/tenants/{tid}/invite")
def invite(tid: str, request: Request, db: Session = Depends(get_db), cw: ChatwootClient = Depends(get_chatwoot)):
    """Fetches a fresh one-time login link from Chatwoot. The link is returned once and never stored or logged."""
    t = _t(db, tid)
    if t.status != "active" or not t.chatwoot_user_id:
        raise HTTPException(409, {"code": "invalid_state", "message": "tenant must be active with an owner user"})
    try:
        link = cw.login_link(t.chatwoot_user_id)
    except ProvisioningError as e:
        raise HTTPException(502, {"code": "chatwoot_error", "message": str(e)})
    ts.audit(db, request.state.actor, "tenant.invite_link", t.id)
    db.commit()
    return {"invite_link": link}


@router.get("/tenants/{tid}/jobs")
def tenant_jobs(tid: str, db: Session = Depends(get_db)):
    _t(db, tid)
    return [_job(j) for j in db.scalars(select(ProvisioningJob).where(ProvisioningJob.tenant_id == tid)
                                        .order_by(ProvisioningJob.created_at.desc()).limit(100))]


def _job(j: ProvisioningJob) -> dict:
    return dict(id=j.id, tenant_id=j.tenant_id, operation=j.operation, status=j.status, attempts=j.attempts,
                max_attempts=j.max_attempts, last_error=j.last_error, steps=j.result, created_at=j.created_at.isoformat(),
                next_attempt_at=j.next_attempt_at.isoformat() if j.next_attempt_at else None)


@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    q = select(ProvisioningJob).order_by(ProvisioningJob.created_at.desc()).limit(limit)
    if status:
        q = q.where(ProvisioningJob.status.in_(status.split(",")))
    return [_job(j) for j in db.scalars(q)]


@router.post("/jobs/{jid}/retry")
def retry_job(jid: str, body: RetryBody, request: Request, db: Session = Depends(get_db)):
    j = db.get(ProvisioningJob, jid)
    if not j:
        raise HTTPException(404, {"code": "job_not_found", "message": "job not found"})
    try:
        jobs.requeue(db, j, body.chatwoot_account_id, body.chatwoot_user_id, body.confirm_not_created)
    except ValueError as e:
        raise HTTPException(409, {"code": "retry_refused", "message": str(e)})
    ts.audit(db, request.state.actor, "job.retry", j.tenant_id,
             {"job": j.id, "adopted_account": body.chatwoot_account_id, "adopted_user": body.chatwoot_user_id})
    db.commit()
    return _job(j)


@router.get("/tenants/{tid}/channels")
def list_channels(tid: str, db: Session = Depends(get_db)):
    _t(db, tid)
    return [channel_out(c) for c in db.scalars(select(TenantChannel).where(TenantChannel.tenant_id == tid, TenantChannel.status != "removed"))]


def channel_out(c: TenantChannel) -> dict:
    return dict(id=c.id, tenant_id=c.tenant_id, channel_type=c.channel_type, provider=c.provider,
                chatwoot_inbox_id=c.chatwoot_inbox_id, display_name=c.display_name, phone_number=c.phone_number,
                external_id=c.external_id, external_account_id=c.external_account_id, status=c.status,
                verification_status=c.verification_status, config=c.config)


@router.post("/tenants/{tid}/channels")
def add_channel(tid: str, body: ChannelCreate, request: Request, db: Session = Depends(get_db)):
    ch = chs.create_channel(db, _t(db, tid).id, body.model_dump())
    ts.audit(db, request.state.actor, "channel.create", tid, {"channel": ch.id, "type": ch.channel_type})
    db.commit()
    return channel_out(ch)


def _channel(db: Session, tid: str, cid: str) -> TenantChannel:
    c = db.get(TenantChannel, cid)
    if not c or c.tenant_id != tid or c.status == "removed":
        raise HTTPException(404, {"code": "channel_not_found", "message": "channel not found"})
    return c


@router.patch("/tenants/{tid}/channels/{cid}")
def patch_channel(tid: str, cid: str, body: ChannelPatch, request: Request, db: Session = Depends(get_db)):
    c = _channel(db, tid, cid)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    ts.audit(db, request.state.actor, "channel.update", tid, {"channel": c.id})
    db.commit()
    return channel_out(c)


@router.delete("/tenants/{tid}/channels/{cid}")
def remove_channel(tid: str, cid: str, request: Request, db: Session = Depends(get_db)):
    c = _channel(db, tid, cid)
    c.status = "removed"
    ts.audit(db, request.state.actor, "channel.remove", tid, {"channel": c.id})
    db.commit()
    return {"removed": True}


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    return [plan_out(p) for p in db.scalars(select(Plan).order_by(Plan.created_at, Plan.code))]


def _price_micro(db: Session, price, currency: str):
    if price is None:
        return None
    if not db.get(Currency, currency):
        raise HTTPException(422, {"code": "unknown_currency", "message": f"unknown currency {currency}"})
    try:
        return money.input_to_micro(price, currency)
    except money.MoneyError as e:
        raise HTTPException(422, {"code": "bad_amount", "message": str(e)})


def plan_out(p: Plan) -> dict:
    return dict(code=p.code, name=p.name, active=p.active, currency=p.currency,
                price=str(money.micro_to_decimal(p.price_micro)) if p.price_micro is not None else None,
                seats=p.seats, channels=p.channels, ai_replies=p.ai_replies,
                default_markup=str(p.default_markup) if p.default_markup is not None else None, kb_level=p.kb_level)


@router.post("/plans")
def create_plan(body: PlanBody, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(Plan).where(Plan.code == body.code)):
        raise HTTPException(409, {"code": "plan_exists", "message": "plan code already exists"})
    d = body.model_dump()
    price = d.pop("price")
    d["price_micro"] = _price_micro(db, price, d["currency"])
    p = Plan(**d)
    db.add(p)
    ts.audit(db, request.state.actor, "plan.create", None, {"code": p.code})
    db.commit()
    return plan_out(p)


@router.patch("/plans/{code}")
def patch_plan(code: str, body: PlanPatch, request: Request, db: Session = Depends(get_db)):
    """Changes apply to every tenant on this plan immediately; their Chatwoot limits are re-synced by jobs."""
    p = ent.get_plan(db, code, must_be_active=False)
    before = plan_out(p)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "price":
            p.price_micro = _price_micro(db, v, p.currency)
        else:
            setattr(p, k, v)
    ts.audit(db, request.state.actor, "plan.update", None, {"code": code, "before": before, "after": plan_out(p)})
    for t in db.scalars(select(Tenant).join(Tenant.entitlement).where(Tenant.status.in_(("active", "suspended")),
                                                                      Tenant.entitlement.has(plan_id=p.id))).unique():
        _sync_limits(db, t)
    db.commit()
    return plan_out(p)


@router.get("/tenants/{tid}/members")
def list_members(tid: str, db: Session = Depends(get_db)):
    _t(db, tid)
    return [dict(id=m.id, email=m.email, role=m.role, status=m.status, linked=m.subject is not None)
            for m in db.scalars(select(TenantMember).where(TenantMember.tenant_id == tid))]


@router.post("/tenants/{tid}/members")
def add_member(tid: str, body: MemberBody, request: Request, db: Session = Depends(get_db)):
    _t(db, tid)
    m = db.scalar(select(TenantMember).where(TenantMember.tenant_id == tid, TenantMember.email == body.email.lower()))
    if m:
        m.role, m.status = body.role, "active"
    else:
        m = TenantMember(tenant_id=tid, email=body.email.lower(), role=body.role, status="active")
        db.add(m)
    ts.audit(db, request.state.actor, "member.upsert", tid, {"email": body.email.lower(), "role": body.role})
    db.commit()
    return dict(id=m.id, email=m.email, role=m.role, status=m.status)


@router.delete("/tenants/{tid}/members/{mid}")
def revoke_member(tid: str, mid: str, request: Request, db: Session = Depends(get_db)):
    m = db.get(TenantMember, mid)
    if not m or m.tenant_id != tid:
        raise HTTPException(404, {"code": "member_not_found", "message": "member not found"})
    m.status = "revoked"
    ts.audit(db, request.state.actor, "member.revoke", tid, {"email": m.email})
    db.commit()
    return {"revoked": True}


@router.get("/tenants/{tid}/usage-link")
def usage_link(tid: str, db: Session = Depends(get_db)):
    if not get_settings().customer_api_enabled:
        raise HTTPException(404)
    t = _t(db, tid)
    s = get_settings()
    tok = URLSafeTimedSerializer(s.secret_key, salt="usage").dumps(t.id)
    return {"url": f"{s.public_url}/u/{tok}", "valid_hours": 24}


def _amount(amount, currency: str) -> int:
    try:
        return money.input_to_micro(amount, currency)
    except (money.MoneyError, TypeError) as e:
        raise HTTPException(422, {"code": "bad_amount", "message": str(e)})


def _wallet_out(db: Session, tid: str, p: wl.Posting, key: str) -> dict:
    w = db.get(Wallet, tid)
    return {key: p.created, "currency": w.currency, "balance_micro": p.balance_micro, "balance": money.format_micro(p.balance_micro, w.currency),
            "receivable_micro": p.receivable_micro, "debt_cleared_micro": p.settled_micro, "ledger_id": p.ledger_id}


@router.post("/wallets/{tid}/credit")
def credit(tid: str, body: CreditBody, request: Request, db: Session = Depends(get_db)):
    """Money in (a verified payment). Any debt is cleared first; only the rest becomes usable funds."""
    t = _t(db, tid)
    micro = _amount(body.amount, body.currency)
    try:
        p = wl.credit(db, t.id, micro, body.idempotency_key, reference=body.reference, currency=body.currency)
    except wl.CurrencyMismatch as ex:
        raise HTTPException(409, {"code": "currency_mismatch", "message": str(ex)})
    if p.created:
        ts.audit(db, request.state.actor, "wallet.credit", t.id,
                 {"amount_micro": micro, "currency": body.currency, "key": body.idempotency_key, "debt_cleared_micro": p.settled_micro})
    db.commit()
    return _wallet_out(db, t.id, p, "credited")


@router.patch("/wallets/{tid}/credit-limit")
def set_credit_limit(tid: str, body: CreditLimitBody, request: Request, db: Session = Depends(get_db)):
    """How much NEW controlled sending may rest on credit. Default 0. Charges the provider has already made are recorded
    regardless of this limit; the limit only decides whether new sends the hub controls are approved."""
    t = _t(db, tid)
    micro = _amount(body.amount, body.currency)
    w = wl.lock_wallet(db, t.id)
    if body.currency != w.currency:
        raise HTTPException(409, {"code": "currency_mismatch", "message": f"wallet is {w.currency}"})
    before, w.credit_limit_micro = w.credit_limit_micro, micro
    ts.audit(db, request.state.actor, "wallet.credit_limit", t.id, {"before_micro": before, "after_micro": micro,
                                                                   "currency": w.currency, "reason": body.reason})
    db.commit()
    return {"currency": w.currency, "credit_limit_micro": micro, "credit_limit": money.format_micro(micro, w.currency),
            "capacity_micro": wl.capacity_micro(w)}


@router.get("/wallets/{tid}/receivable")
def receivable_ledger(tid: str, limit: int = 100, db: Session = Depends(get_db)):
    t = _t(db, tid)
    rows = db.scalars(select(ReceivableEntry).where(ReceivableEntry.tenant_id == t.id)
                      .order_by(ReceivableEntry.created_at.desc()).limit(min(limit, 500)))
    return [dict(id=r.id, kind=r.kind, currency=r.currency, amount_micro=r.amount_micro, balance_after_micro=r.balance_after_micro,
                 reference=r.reference, at=r.created_at.isoformat()) for r in rows]


@router.get("/wallets/{tid}/ledger")
def ledger(tid: str, limit: int = 100, db: Session = Depends(get_db)):
    t = _t(db, tid)
    rows = db.scalars(select(LedgerEntry).where(LedgerEntry.tenant_id == t.id)
                      .order_by(LedgerEntry.created_at.desc()).limit(min(limit, 500)))
    return [dict(id=r.id, kind=r.kind, currency=r.currency, amount_micro=r.amount_micro,
                 amount=money.format_micro(r.amount_micro, r.currency), balance_after_micro=r.balance_after_micro,
                 reference=r.reference, at=r.created_at.isoformat()) for r in rows]


@router.get("/alerts")
def alerts(db: Session = Depends(get_db)):
    out = []
    for t in db.scalars(select(Tenant).where(Tenant.status == "active")):
        s = ts.summary(db, t)
        if s["wallet"]["alert"]:
            out.append({"tenant_id": t.id, "name": t.name, "alert": s["wallet"]["alert"],
                        "currency": s["wallet"]["currency"], "balance_micro": s["wallet"]["balance_micro"]})
    return out


@router.get("/rate-cards")
def list_rate_cards(category: str | None = None, market: str | None = None, currency: str | None = None,
                    active: bool | None = None, db: Session = Depends(get_db)):
    q = select(RateCard).order_by(RateCard.category, RateCard.market, RateCard.currency, RateCard.effective_from)
    for col, v in ((RateCard.category, category), (RateCard.market, market.upper() if market else None),
                   (RateCard.currency, currency.upper() if currency else None), (RateCard.active, active)):
        if v is not None:
            q = q.where(col == v)
    return [rate_out(r) for r in db.scalars(q)]


def rate_out(r: RateCard) -> dict:
    return dict(id=r.id, provider=r.provider, category=r.category, market=r.market, currency=r.currency,
                rate=str(money.micro_to_decimal(r.rate_micro)), rate_micro=r.rate_micro,
                effective_from=r.effective_from.isoformat(), effective_to=r.effective_to.isoformat() if r.effective_to else None,
                version=r.version, active=r.active, source=r.source)


@router.post("/rate-cards")
def add_rate_card(body: RateBody, request: Request, db: Session = Depends(get_db)):
    try:
        card = rates_svc.add_rate(db, category=body.category, market=body.market, currency=body.currency, rate=body.rate,
                                  effective_from=dt.date.fromisoformat(body.effective_from),
                                  effective_to=dt.date.fromisoformat(body.effective_to) if body.effective_to else None,
                                  source=body.source, source_ref=body.source_ref, actor=request.state.actor)
    except rates_svc.RateError as e:
        raise HTTPException(409 if e.code == "overlap" else 422, {"code": e.code, "message": str(e)})
    except ValueError:
        raise HTTPException(422, {"code": "bad_date", "message": "dates are YYYY-MM-DD"})
    ts.audit(db, request.state.actor, "rate.add", None, {"id": card.id, "category": card.category, "market": card.market,
                                                         "currency": card.currency, "rate_micro": card.rate_micro, "version": card.version})
    db.commit()
    return rate_out(card)


@router.post("/rate-cards/{rid}/deactivate")
def deactivate_rate_card(rid: str, request: Request, db: Session = Depends(get_db)):
    """Withdraws a rate card entered by mistake. Usage already priced from it keeps its snapshot untouched."""
    r = db.get(RateCard, rid)
    if not r:
        raise HTTPException(404, {"code": "not_found", "message": "rate card not found"})
    r.active = False
    ts.audit(db, request.state.actor, "rate.deactivate", None, {"id": rid})
    db.commit()
    return rate_out(r)


def fx_out(f: FxRate) -> dict:
    return dict(id=f.id, base=f.base_currency, quote=f.quote_currency, rate=str(f.rate), source=f.source, status=f.status,
                effective_from=f.effective_from.isoformat(), approves_id=f.approves_id)


@router.get("/fx")
def list_fx(base: str | None = None, quote: str | None = None, status: str | None = None, limit: int = 100,
            db: Session = Depends(get_db)):
    q = select(FxRate).order_by(FxRate.effective_from.desc()).limit(min(limit, 500))
    for col, v in ((FxRate.base_currency, base and base.upper()), (FxRate.quote_currency, quote and quote.upper()), (FxRate.status, status)):
        if v:
            q = q.where(col == v)
    return [fx_out(f) for f in db.scalars(q)]


@router.post("/fx")
def add_fx(body: FxBody, request: Request, db: Session = Depends(get_db)):
    at = dt.datetime.fromisoformat(body.effective_from.replace("Z", "+00:00")) if body.effective_from else now()
    res = fx_svc.record_quotes(db, [fx_svc.Quote(body.base.upper(), body.quote.upper(), body.rate, "manual", at, body.source_ref)],
                               request.state.actor)
    if res["skipped"]:
        raise HTTPException(422, {"code": "bad_pair", "message": "unknown currency, equal currencies or non-positive rate"})
    ts.audit(db, request.state.actor, "fx.add", None, {"pair": f"{body.base}/{body.quote}", "rate": str(body.rate), **res})
    db.commit()
    return res


@router.post("/fx/sync")
def sync_fx(request: Request, db: Session = Depends(get_db)):
    """Pulls current rates from the configured provider (WiseCP). Refuses stale, unreadable or re-based data."""
    try:
        quotes = fx_svc.get_fx_provider().fetch()
    except fx_svc.FxError as e:
        ts.audit(db, request.state.actor, "fx.sync", None, {"error": e.code}, "failure")
        db.commit()
        raise HTTPException(502, {"code": e.code, "message": str(e)})
    res = fx_svc.record_quotes(db, quotes, request.state.actor)
    ts.audit(db, request.state.actor, "fx.sync", None, res)
    db.commit()
    return res


@router.post("/fx/{fx_id}/approve")
def approve_fx(fx_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        row = fx_svc.approve(db, fx_id, request.state.actor)
    except fx_svc.FxError as e:
        raise HTTPException(409, {"code": e.code, "message": str(e)})
    ts.audit(db, request.state.actor, "fx.approve", None, {"approved": fx_id, "new_row": row.id})
    db.commit()
    return fx_out(row)


@router.get("/pricing/preview")
def pricing_preview(tenant_id: str, category: str, country: str | None = None, db: Session = Depends(get_db)):
    """What one message would cost this tenant right now. Nothing is stored."""
    t = _t(db, tenant_id)
    try:
        snap = pricing.price(db, t, db.get(Wallet, t.id), category, country, now())
    except pricing.PricingError as e:
        raise HTTPException(422, {"code": e.code, "message": str(e)})
    cur = snap.customer_currency
    return dict(currency=cur, provider_currency=snap.provider_currency, rate_source=snap.rate_source, fx_rate=str(snap.fx_rate),
                unit_cost_micro=snap.unit_cost_micro, buffer_micro=snap.buffer_micro, fee_micro=snap.fee_micro,
                unit_charge_micro=snap.unit_charge_micro, unit_charge=money.format_micro(snap.unit_charge_micro, cur))


@router.get("/currencies")
def list_currencies(enabled: bool | None = None, db: Session = Depends(get_db)):
    q = select(Currency).order_by(Currency.code)
    if enabled is not None:
        q = q.where(Currency.billing_enabled == enabled)
    return [dict(code=c.code, name=c.name, exponent=c.exponent, iso_status=c.iso_status, billing_enabled=c.billing_enabled)
            for c in db.scalars(q)]


@router.patch("/currencies/{code}")
def patch_currency(code: str, body: CurrencyPatch, request: Request, db: Session = Depends(get_db)):
    c = db.get(Currency, code.upper())
    if not c:
        raise HTTPException(404, {"code": "unknown_currency", "message": "unknown currency"})
    if body.billing_enabled and c.exponent is None:
        raise HTTPException(422, {"code": "no_exponent", "message": f"{c.code} cannot hold money"})
    if not body.billing_enabled and db.scalar(select(func.count()).select_from(Wallet).where(Wallet.currency == c.code)):
        raise HTTPException(409, {"code": "currency_in_use", "message": "wallets exist in this currency"})
    c.billing_enabled = body.billing_enabled
    ts.audit(db, request.state.actor, "currency.billing", None, {"code": c.code, "enabled": body.billing_enabled, "iso_status": c.iso_status})
    db.commit()
    return dict(code=c.code, billing_enabled=c.billing_enabled)


@router.post("/tenants/{tid}/ai-adjust")
def ai_adjust(tid: str, body: AiAdjustBody, request: Request, db: Session = Depends(get_db)):
    """Staff correction to this month's AI-reply count (positive uses allowance, negative gives it back). Audited."""
    t = _t(db, tid)
    try:
        c, created, used = ai_allowance.adjust(db, t.id, body.delta, body.idempotency_key, body.reason)
    except ValueError as e:
        raise HTTPException(409, {"code": "below_zero", "message": str(e)})
    if created:
        ts.audit(db, request.state.actor, "ai.adjust", t.id, {"delta": body.delta, "reason": body.reason, "key": body.idempotency_key, "period": c.period})
    db.commit()
    return {"applied": created, "period": c.period, "used": used, "limit": t.ai_limit}


@router.get("/tenants/{tid}/ai-consumptions")
def ai_consumptions(tid: str, limit: int = 100, db: Session = Depends(get_db)):
    _t(db, tid)
    rows = db.scalars(select(AiConsumption).where(AiConsumption.tenant_id == tid).order_by(AiConsumption.created_at.desc()).limit(min(limit, 500)))
    return [dict(key=r.consumption_key, period=r.period, kind=r.kind, units=r.units, state=r.state, reason=r.reason, at=r.created_at.isoformat()) for r in rows]


@router.get("/tenants/{tid}/stats")
def tenant_stats(tid: str, ranges: str = Query("month,30d", description="comma list of: " + ",".join(stats_svc.RANGES)),
                 db: Session = Depends(get_db), rep: ChatwootReports = Depends(get_reports),
                 cw: ChatwootClient = Depends(get_chatwoot)):
    """Customer dashboard figures for the WiseCP module: conversations, messages (total and per channel), WhatsApp billing, wallet.
    Customer-safe: no markup, no provider cost split, no internal identifiers beyond the hub's own channel ids."""
    t = _t(db, tid)
    wanted = [r.strip() for r in ranges.split(",") if r.strip()]
    if not wanted or len(wanted) > 4 or any(r not in stats_svc.RANGES for r in wanted):
        raise HTTPException(422, {"code": "bad_ranges", "message": f"ranges must be a subset of {list(stats_svc.RANGES)}"})
    out = stats_svc.tenant_stats(db, t, list(dict.fromkeys(wanted)), stats_svc.reports_for(t, rep, cw))
    db.commit()
    for r in out["ranges"].values():                       # the customer sees what they were charged, never how it was built
        for v in r["whatsapp"]["by_category"].values():
            v.pop("charged_micro", None)
        r["whatsapp"].pop("charged_micro", None)
    return out


@router.post("/topups/invoice/{invoice_id}")
def topup_invoice(invoice_id: str, request: Request, db: Session = Depends(get_db),
                  wc: wisecp_svc.WisecpClient = Depends(get_wisecp)):
    """Called by the WiseCP module when an invoice is paid (and by staff to re-run one). The body is ignored on purpose:
    the hub reads the invoice itself and credits per the top-up contract. Safe to call any number of times."""
    try:
        inv = wc.get_invoice(invoice_id)
    except wisecp_svc.WisecpError as e:
        ts.audit(db, request.state.actor, "topup.error", None, {"invoice": invoice_id, "code": e.code, "message": str(e)[:200]}, outcome="error")
        db.commit()
        raise HTTPException(502 if e.code in ("unreachable", "insufficient_scope", "not_configured") or e.code.startswith("http_") else 404 if e.code == "invoice_not_found" else 422,
                            {"code": e.code, "message": str(e)})
    out = wisecp_svc.process_invoice(db, inv, request.state.actor)
    ts.audit(db, request.state.actor, "topup.checked", None, {"invoice": invoice_id, "status": out.get("status"),
             "results": [x.get("result") + (":" + x["reason"] if x.get("reason") else "") for x in out.get("results", [])], "note": out.get("note")})
    db.commit()
    return out
