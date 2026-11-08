"""python -m app.cli <command>
  create-key NAME --scope admin|internal [--tenant ID_OR_SLUG ...] [--ip 1.2.3.4 ...] [--days 365]
      internal keys MUST be bound to at least one tenant; admin keys are staff keys and are never bound
  expire-reservations   release holds that never got an outcome (run every 5 minutes)
  list-keys         show API keys (name, scope, tenants, expiry, revoked); never the secret
  revoke-key NAME   revoke an API key immediately
  worker [--once]   process Chatwoot provisioning jobs
  escalate          lock tenants paused longer than SUSPEND_GRACE_DAYS (run daily)
  wisecp-invoice ID        show how the hub reads a WiseCP invoice and what it WOULD credit (changes nothing)
  topup-invoice ID         read a WiseCP invoice and credit its Desk top-up lines (idempotent; use after a missed hook)
  retry-job JOB_ID         requeue a dead/blocked provisioning job (after fixing its cause)
  add-reporter USER_ID     add the Chatwoot reporter user (stats) as administrator of every active tenant workspace (idempotent; run after each new tenant)
  create-tenant SERVICE_ID --name N --email E --plan P --currency C [--chatwoot-account ID] [--chatwoot-user ID] [--no-owner-login]\n      create a hub tenant for an EXISTING WiseCP service (adopts an existing Chatwoot workspace when --chatwoot-account is given)\n  fx-sync           pull current exchange rates from WiseCP and store them (run on a schedule, e.g. hourly)
  seed-plans        load ISO currencies and the default plans (development only; migrations seed production)
  enable-currencies CODE [CODE ...]   allow billing in these ISO currencies (staff decision, audited)
"""
import argparse
import datetime as dt
import time
import uuid

from sqlalchemy import or_, select

from . import db as database
from .config import get_settings
from .models import ApiKey, ApiKeyTenant, Base, Tenant, now, uid
from .routers.admin import get_chatwoot
from .security import hash_key, new_key
from .services import jobs, lifecycle
from .services import tenants as ts
from .services.entitlements import seed_plans


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("create-key")
    k.add_argument("name"); k.add_argument("--scope", required=True, choices=["admin", "internal"])
    k.add_argument("--tenant", action="append", default=[]); k.add_argument("--ip", action="append", default=[])
    k.add_argument("--days", type=int, default=365)
    sub.add_parser("expire-reservations"); sub.add_parser("list-keys"); rk = sub.add_parser("revoke-key"); rk.add_argument("name")
    w = sub.add_parser("worker"); w.add_argument("--once", action="store_true")
    sub.add_parser("escalate"); sub.add_parser("seed-plans")
    sub.add_parser("fx-sync")
    ct = sub.add_parser("create-tenant"); ct.add_argument("service_id")
    ct.add_argument("--name", required=True); ct.add_argument("--email", required=True); ct.add_argument("--plan", required=True)
    ct.add_argument("--currency", required=True); ct.add_argument("--chatwoot-account", type=int); ct.add_argument("--chatwoot-user", type=int)
    ct.add_argument("--no-owner-login", action="store_true")
    rj = sub.add_parser("retry-job"); rj.add_argument("job_id")
    ar = sub.add_parser("add-reporter"); ar.add_argument("user_id", type=int)
    wi = sub.add_parser("wisecp-invoice"); wi.add_argument("invoice_id")
    ti = sub.add_parser("topup-invoice"); ti.add_argument("invoice_id")
    ec = sub.add_parser("enable-currencies"); ec.add_argument("codes", nargs="+")
    a = p.parse_args()

    st = get_settings()
    st.validate_for_runtime()
    database.configure(st.effective_database_url)
    if st.env == "development":
        Base.metadata.create_all(database.engine)
    with database.SessionLocal() as db:
        if a.cmd == "create-key":
            if a.scope == "internal" and not a.tenant:
                raise SystemExit("internal keys must be bound to at least one tenant (--tenant)")
            if a.scope == "admin" and a.tenant:
                raise SystemExit("admin keys are not tenant-bound")
            bound = []
            for ref in a.tenant:
                t = db.scalar(select(Tenant).where(or_(Tenant.id == ref, Tenant.slug == ref)))
                if not t:
                    raise SystemExit(f"tenant not found: {ref}")
                bound.append(t.id)
            raw = new_key()
            key = ApiKey(name=a.name, key_hash=hash_key(raw), scopes=[a.scope], allowed_ips=a.ip,
                         expires_at=now() + dt.timedelta(days=a.days))
            db.add(key); db.flush()
            for tid in bound:
                db.add(ApiKeyTenant(api_key_id=key.id, tenant_id=tid))
            ts.audit(db, "cli", "apikey.create", None, {"name": a.name, "scope": a.scope, "tenants": bound})
            db.commit()
            print("Key (shown once, store it now):", raw)
        elif a.cmd == "expire-reservations":
            from .services import reservations
            n = reservations.expire_due(db)
            if n:
                ts.audit(db, "cli", "reservations.expire", None, {"count": n})
            db.commit()
            print(f"expired {n} reservation(s)")
        elif a.cmd == "list-keys":
            for k in db.scalars(select(ApiKey).order_by(ApiKey.created_at)):
                tenants = [r for r in db.scalars(select(ApiKeyTenant.tenant_id).where(ApiKeyTenant.api_key_id == k.id))]
                print(f"{k.name:30} {','.join(k.scopes):9} tenants={len(tenants)} expires={k.expires_at.date() if k.expires_at else '-'} "
                      f"last_used={k.last_used_at.date() if k.last_used_at else 'never'} {'REVOKED' if k.revoked else 'active'}")
        elif a.cmd == "revoke-key":
            keys = list(db.scalars(select(ApiKey).where(ApiKey.name == a.name)))
            if not keys:
                raise SystemExit(f"no key named {a.name}")
            for k in keys:
                k.revoked = True
            ts.audit(db, "cli", "apikey.revoke", None, {"name": a.name, "count": len(keys)}); db.commit()
            print(f"revoked {len(keys)} key(s) named {a.name}")
        elif a.cmd == "enable-currencies":
            from .models import Currency
            for code in a.codes:
                c = db.get(Currency, code.upper())
                if not c or c.exponent is None:
                    raise SystemExit(f"{code}: unknown currency or no ISO exponent")
                if c.iso_status != "iso":
                    print(f"WARNING {c.code} is not in the current ISO list; its 2-decimal exponent is an assumption")
                c.billing_enabled = True
                ts.audit(db, "cli", "currency.billing", None, {"code": c.code, "enabled": True, "iso_status": c.iso_status})
            db.commit(); print("enabled:", ", ".join(a.codes))
        elif a.cmd == "fx-sync":
            from .services import fx as fx_svc
            try:
                res = fx_svc.record_quotes(db, fx_svc.get_fx_provider().fetch(), "cli:fx-sync")
            except fx_svc.FxError as e:
                ts.audit(db, "cli:fx-sync", "fx.sync", None, {"error": e.code}, "failure"); db.commit()
                raise SystemExit(f"fx-sync FAILED ({e.code}): {e}")
            ts.audit(db, "cli:fx-sync", "fx.sync", None, res); db.commit()
            print("fx-sync:", res)
        elif a.cmd == "create-tenant":
            from .models import Currency, Wallet
            from .services import entitlements as ent
            if db.scalar(select(Tenant).where(Tenant.wisecp_service_id == a.service_id)):
                raise SystemExit(f"a tenant for WiseCP service {a.service_id} already exists")
            cur = db.get(Currency, a.currency.upper())
            if not cur or cur.exponent is None or not cur.billing_enabled:
                raise SystemExit(f"{a.currency} is not enabled for billing (python -m app.cli enable-currencies {a.currency})")
            plan = ent.get_plan(db, a.plan)
            t = Tenant(wisecp_service_id=a.service_id, name=a.name, slug=ts.slugify(a.name), owner_email=a.email, status="pending",
                       billing_currency=cur.code, chatwoot_account_id=a.chatwoot_account, chatwoot_user_id=a.chatwoot_user)
            db.add(t); db.flush()
            ent.create_entitlement(db, t, plan, {})
            db.add(Wallet(tenant_id=t.id, currency=cur.code, balance_micro=0))
            jobs.enqueue(db, t.id, "provision_tenant", f"provision:{t.id}", {"create_owner_user": not a.no_owner_login})
            ts.audit(db, "cli", "tenant.create", t.id, {"plan": plan.code, "adopted_account": bool(a.chatwoot_account), "entitlement": ent.snapshot(t)})
            db.commit()
            print(f"tenant {t.id} ({t.slug}) created for WiseCP service {a.service_id}; the worker will provision/adopt it")
        elif a.cmd == "retry-job":
            from .models import ProvisioningJob
            j = db.get(ProvisioningJob, a.job_id)
            if not j:
                raise SystemExit("job not found")
            try:
                jobs.requeue(db, j, None, None, False)
            except ValueError as e:
                raise SystemExit(f"retry refused: {e}")
            ts.audit(db, "cli", "job.retry", j.tenant_id, {"job": j.id})
            db.commit()
            print(f"job {j.id} requeued; the worker will pick it up")
        elif a.cmd == "add-reporter":
            from .services.chatwoot import ProvisioningError
            cw = get_chatwoot()
            ok = bad = 0
            for t in db.scalars(select(Tenant).where(Tenant.chatwoot_account_id.is_not(None), Tenant.status.in_(("active", "suspended")))).unique():
                try:
                    cw.add_user(t.chatwoot_account_id, a.user_id, "administrator")
                    ok += 1
                except ProvisioningError as e:       # e.g. an adopted account the Platform App does not own
                    bad += 1
                    print(f"  {t.slug}: not added ({e})")
            print(f"reporter added to {ok} workspace(s); {bad} could not be changed")
        elif a.cmd in ("wisecp-invoice", "topup-invoice"):
            from .services import wisecp as wc
            try:
                inv = wc.get_wisecp().get_invoice(a.invoice_id)
            except wc.WisecpError as e:
                raise SystemExit(f"cannot read invoice ({e.code}): {e}")
            if a.cmd == "wisecp-invoice":
                print(f"invoice {inv.invoice_id}: status={inv.status} currency={inv.currency} total={inv.total} "
                      f"payments_in={inv.payments_in} rows={inv.payment_rows} methods={inv.payment_methods}")
                for l in inv.lines:
                    print(f"  line {l.line_id}: {l.description!r} qty={l.quantity} unit={l.unit} discount={l.discount}"
                          f"{'%' if l.discount_is_percent else ''} service={l.service_id} addon={l.addon_id} "
                          f"subtotal_ex_tax={l.subtotal} topup={wc.is_topup_line(l)}")
            else:
                res = wc.process_invoice(db, inv, "cli:topup-invoice")
                print(res)
        elif a.cmd == "seed-plans":
            from .services.currencies import seed_currencies
            seed_currencies(db); seed_plans(db); db.commit(); print("currencies and plans ready")
        elif a.cmd == "worker":
            wid = f"worker-{uuid.uuid4().hex[:6]}"
            cw = get_chatwoot()
            print(f"{wid} started; polling for jobs", flush=True)
            while True:
                job = jobs.run_once(db, cw, wid)
                if job is not None:
                    print(f"{wid} ran {job.operation} job {job.id} -> {job.status}", flush=True)   # ids only, never customer data
                if a.once:
                    break
                if job is None:
                    time.sleep(5)
        elif a.cmd == "escalate":
            limit = now() - dt.timedelta(days=st.suspend_grace_days)
            for t in db.scalars(select(Tenant).where(Tenant.status == "suspended", Tenant.suspend_mode == "pause_outbound")).unique():
                at = t.suspended_at.replace(tzinfo=dt.timezone.utc) if t.suspended_at and t.suspended_at.tzinfo is None else t.suspended_at
                if at and at < limit:
                    lifecycle.suspend(t, "lock", "grace period ended")
                    jobs.enqueue(db, t.id, "sync_chatwoot_status", f"cw-status:{t.id}:{uid()}")
                    ts.audit(db, "cli", "tenant.escalate_lock", t.id)
                    db.commit()
                    print("locked", t.slug)


if __name__ == "__main__":
    main()
