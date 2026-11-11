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
  seed-meta-rates [--from YYYY-MM-DD]   load Meta's per-country rate card (app/data/meta_rates_2026-10-01.csv, USD) for all four categories
  add-channel TENANT --type whatsapp --name N [--inbox ID] [--phone +234..] [--phone-number-id ID] [--waba-id ID]
      register a channel (the WhatsApp phone-number id is what maps Meta billing events to the customer)
  meta-billing-sync [--days 7] [--dry-run]   bill WhatsApp usage from Meta's daily pricing analytics (idempotent; schedule it hourly)
  notify-run [--force-report] [--dry-run]   send due weekly reports and low-balance alerts (schedule hourly; each is sent once)
  set-notify TENANT [--report on|off] [--low-balance on|off] [--whatsapp 234...,234...] [--email a@b.com,...]
  onboard-check TENANT     checklist of everything a client needs (Chatwoot access, channels, rates, balance, keys, notifications)
  cancel-tenant SLUG        cancel a subscription (locks the workspace; ends in 'cancelled')
  force-cancelled SLUG --reason TEXT   mark a tenant stuck in 'cancelling' as cancelled WITHOUT locking Chatwoot (you do that by hand)
  delete-tenant SLUG --confirm SLUG [--purge-history]   permanently delete a stuck/cancelled/terminated tenant (money history is kept unless purged)
  check-key 'chd_...'      tell whether a key text is valid and what it belongs to; repairs 0/O and 1/l/I typing mix-ups
  list-tenants             show every tenant: id, slug, name, status, WiseCP service, Chatwoot account
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
    sm = sub.add_parser("seed-meta-rates"); sm.add_argument("--from", dest="eff")
    ac = sub.add_parser("add-channel"); ac.add_argument("tenant"); ac.add_argument("--type", required=True); ac.add_argument("--name", required=True)
    ac.add_argument("--inbox", type=int); ac.add_argument("--phone"); ac.add_argument("--phone-number-id"); ac.add_argument("--waba-id")
    mb = sub.add_parser("meta-billing-sync"); mb.add_argument("--days", type=int, default=7); mb.add_argument("--dry-run", action="store_true")
    nr = sub.add_parser("notify-run"); nr.add_argument("--force-report", action="store_true"); nr.add_argument("--dry-run", action="store_true")
    sn = sub.add_parser("set-notify"); sn.add_argument("tenant"); sn.add_argument("--report", choices=["on", "off"]); sn.add_argument("--low-balance", choices=["on", "off"])
    sn.add_argument("--whatsapp"); sn.add_argument("--email")
    oc = sub.add_parser("onboard-check"); oc.add_argument("tenant")
    dt_ = sub.add_parser("delete-tenant"); dt_.add_argument("tenant"); dt_.add_argument("--confirm", required=True); dt_.add_argument("--purge-history", action="store_true")
    ct_ = sub.add_parser("cancel-tenant"); ct_.add_argument("tenant")
    fc_ = sub.add_parser("force-cancelled"); fc_.add_argument("tenant"); fc_.add_argument("--reason", required=True)
    ck_ = sub.add_parser("check-key"); ck_.add_argument("key")
    sub.add_parser("list-tenants")
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
        elif a.cmd == "seed-meta-rates":
            import csv, datetime as _dt
            from pathlib import Path
            from .services import rates as rt
            eff = _dt.date.fromisoformat(a.eff) if a.eff else _dt.date.today()
            added = skipped = 0
            src = "Meta WhatsApp rate card effective 2026-10-01 (owner-supplied spreadsheet; verify against developers.facebook.com)"
            with open(Path(__file__).parent / "data" / "meta_rates_2026-10-01.csv", newline="") as fh:
                for row in csv.DictReader(fh):
                    for cat in rt.CATEGORIES:
                        try:
                            rt.add_rate(db, category=cat, market=row["market"], currency=row["currency"], rate=row[cat], effective_from=eff,
                                        source=src, actor="cli:seed-meta-rates")
                            added += 1
                        except rt.RateError as e:
                            if e.code != "overlap":
                                raise SystemExit(f"{row['market']}/{cat}: {e}")
                            skipped += 1
            db.commit()
            print(f"rate cards added: {added}; already present (skipped): {skipped}")
        elif a.cmd == "add-channel":
            from .services import channels as chs
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (use its id or slug)")
            ch = chs.create_channel(db, t.id, {"channel_type": a.type, "provider": "chatwoot_native", "display_name": a.name,
                                               "chatwoot_inbox_id": a.inbox, "phone_number": a.phone, "external_id": a.phone_number_id,
                                               "external_account_id": a.waba_id, "status": "connected"})
            ts.audit(db, "cli", "channel.create", t.id, {"channel": ch.id, "type": ch.channel_type})
            db.commit()
            print(f"channel {ch.id} registered for {t.slug}")
        elif a.cmd == "meta-billing-sync":
            import datetime as _dt
            from .services import meta_billing as mb_svc
            try:
                rep = mb_svc.sync(db, _dt.datetime.now(_dt.timezone.utc), a.days, a.dry_run)
            except mb_svc.MetaError as e:
                raise SystemExit(f"meta-billing-sync: {e}")
            print(("DRY RUN (nothing charged): " if a.dry_run else "") + str(rep))
        elif a.cmd == "notify-run":
            import datetime as _dt
            from .services import notify as nf
            print(nf.run(db, _dt.datetime.now(_dt.timezone.utc), a.force_report, a.dry_run))
        elif a.cmd == "set-notify":
            from .services import notify as nf
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (python -m app.cli list-tenants)")
            nf.set_conf(t, report=None if a.report is None else a.report == "on", low=None if a.low_balance is None else a.low_balance == "on",
                        whatsapp=None if a.whatsapp is None else a.whatsapp.split(","), email=None if a.email is None else a.email.split(","))
            ts.audit(db, "cli", "tenant.notify_set", t.id, {"report": t.notify["report_enabled"], "low": t.notify["low_balance_enabled"],
                                                            "whatsapp": len(t.notify["whatsapp"]), "email": len(t.notify["email"])})
            db.commit()
            print(f"{t.slug}: {nf.conf(t)}")
        elif a.cmd == "onboard-check":
            import datetime as _dt
            import json as _json
            from . import money
            from .models import TenantChannel
            from .services import notify as nf, pricing as pr, rates as rt, stats as st_svc, wallet as wlt
            from .services.chatwoot_reports import ChatwootReports, ReportError
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (python -m app.cli list-tenants)")
            S = get_settings()
            def line(ok, what, fix=""):
                print(("  [ OK ] " if ok else "  [FIX ] ") + what + ("" if ok or not fix else "\n           -> " + fix))
            print(f"{t.name} ({t.slug}) status={t.status} plan/entitlement ok")
            line(t.status == "active", f"tenant status is {t.status}", "wait for the worker, or `retry-job <job id>`; check Audit for a blocked job")
            line(bool(t.chatwoot_account_id), f"Chatwoot account {t.chatwoot_account_id}", "create-tenant needs --chatwoot-account for an existing workspace")
            line(bool(t.chatwoot_user_id), f"Chatwoot owner user {t.chatwoot_user_id}", "needed for sign-in link, password and stats (create-tenant --chatwoot-user)")
            cw = get_chatwoot()
            tok = ""
            try:
                tok = cw.user_token(t.chatwoot_user_id) if t.chatwoot_user_id else ""
            except Exception as e:
                tok = ""
                print(f"           (Chatwoot said: {str(e)[:120]})")
            line(bool(tok), "Platform App can read the owner user (stats, sign-in)", "Rails console: PlatformAppPermissible for this Account AND this User (see docs/ONBOARDING.md)")
            if tok and t.chatwoot_account_id:
                try:
                    rep = ChatwootReports(S.chatwoot_url, tok, S.stats_http_timeout)
                    now_ = _dt.datetime.now(_dt.timezone.utc)
                    r = rep.summary(t.chatwoot_account_id, now_ - _dt.timedelta(days=30), now_)
                    line(True, f"Chatwoot reports readable (last 30 days: {r['conversations']} conversations, {r['messages']} messages)")
                except ReportError as e:
                    line(False, f"Chatwoot reports failed ({e})", "the owner user must be an administrator of the account")
            chans = db.scalars(select(TenantChannel).where(TenantChannel.tenant_id == t.id, TenantChannel.status != "removed")).all()
            line(bool(chans), f"{len(chans)} channel(s) registered", "add-channel (needed for billing: it maps Meta's number to this client)")
            for c in chans:
                if c.channel_type == "whatsapp":
                    ok = bool(c.external_account_id and c.phone_number and c.external_id)
                    line(ok, f"WhatsApp {c.phone_number or '?'}: WABA={c.external_account_id} phone_number_id={c.external_id}", "re-run add-channel with --phone, --phone-number-id and --waba-id")
                    try:
                        _tok = (_json.loads(S.meta_waba_tokens) if S.meta_waba_tokens else {}).get(str(c.external_account_id)) or S.meta_access_token
                    except ValueError:
                        _tok = ""
                    line(bool(_tok), "a Meta token covers this WABA", "set META_ACCESS_TOKEN, or META_WABA_TOKENS={\"<waba>\":\"<token>\"}")
            w = wlt.get_wallet(db, t.id)
            line(w.balance_micro > 0, f"wallet {w.currency} balance {money.format_micro(w.balance_micro, w.currency)}", "UI > tenant > add credit (opening balance)")
            bad = []
            for cat in rt.CATEGORIES:
                try:
                    pr.price(db, t, w, cat, S.price_display_market or None, _dt.datetime.now(_dt.timezone.utc), persist=False)
                except Exception:
                    bad.append(cat)
            line(not bad, "rate cards give a price for every category" if not bad else f"no price for: {', '.join(bad)}", "python -m app.cli seed-meta-rates, and fx-sync")
            linked = {r for r in db.scalars(select(ApiKeyTenant.api_key_id).where(ApiKeyTenant.tenant_id == t.id))}
            keys = [k for k in db.scalars(select(ApiKey).where(ApiKey.revoked.is_(False))) if "internal" in (k.scopes or []) and k.id in linked]
            line(bool(keys), f"{len(keys)} internal key(s) for n8n", f"python -m app.cli create-key n8n-{t.slug} --scope internal --tenant {t.slug}")
            n = nf.conf(t)
            line(n["report_enabled"] or n["low_balance_enabled"], f"notifications: report={n['report_enabled']} low-balance={n['low_balance_enabled']} whatsapp={len(n['whatsapp'])} email={len(n['email'])}",
                 f"python -m app.cli set-notify {t.slug} --report on --low-balance on --whatsapp <digits> --email <address>")
            line(bool(S.meta_billing_start), f"META_BILLING_START={S.meta_billing_start or 'unset'}", "set it in Coolify (YYYY-MM-DD)")
        elif a.cmd == "cancel-tenant":
            from .services import lifecycle as lc
            from .services import jobs as jb
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (python -m app.cli list-tenants)")
            try:
                needs_lock = lc.cancel(t)
            except lc.InvalidTransition as e:
                raise SystemExit(f"cannot cancel from {e.frm}: {e}")
            if needs_lock:
                jb.enqueue(db, t.id, "sync_chatwoot_status", f"cw-status:{t.id}:{uid()}")
            ts.audit(db, "cli", "tenant.cancel", t.id, {"status": t.status})
            db.commit()
            print(f"{t.slug}: {t.status} (the worker locks the Chatwoot workspace, then it becomes 'cancelled')")
        elif a.cmd == "force-cancelled":
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (python -m app.cli list-tenants)")
            if t.status != "cancelling":
                raise SystemExit(f"only a tenant stuck in 'cancelling' can be forced (this one is {t.status})")
            t.status = "cancelled"
            ts.audit(db, "cli", "tenant.force_cancelled", t.id, {"reason": a.reason, "chatwoot_locked": False})
            db.commit()
            print(f"{t.slug}: cancelled WITHOUT locking Chatwoot. Lock or remove the workspace in Chatwoot yourself if needed.")
        elif a.cmd == "delete-tenant":
            t = db.scalar(select(Tenant).where((Tenant.id == a.tenant) | (Tenant.slug == a.tenant)))
            if not t:
                raise SystemExit("tenant not found (python -m app.cli list-tenants)")
            try:
                ts.delete_tenant(db, t, "cli", a.confirm, purge_history=a.purge_history)
            except ValueError as e:
                raise SystemExit(f"refused: {e}")
            db.commit()
            print("deleted")
        elif a.cmd == "check-key":
            import itertools
            raw = a.key.strip()
            lookup = lambda k: db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(k)))
            found, fixed = lookup(raw), None
            if not found:
                groups = {"0": "0O", "O": "0O", "1": "1lI", "l": "1lI", "I": "1lI"}
                pos = [i for i, c in enumerate(raw) if c in groups]
                if len(pos) <= 12:
                    for combo in itertools.product(*[groups[raw[i]] for i in pos]):
                        cand = list(raw)
                        for i, c in zip(pos, combo):
                            cand[i] = c
                        cand = "".join(cand)
                        found = lookup(cand)
                        if found:
                            fixed = cand
                            break
            if not found:
                raise SystemExit("NO KEY MATCHES this text (not even after trying 0/O and 1/l/I swaps). It is wrong, cut off, or from another system. Create a new key with create-key and copy it, do not retype it.")
            slugs = [t_.slug for t_ in db.scalars(select(Tenant).join(ApiKeyTenant, ApiKeyTenant.tenant_id == Tenant.id).where(ApiKeyTenant.api_key_id == found.id))]
            print(f"MATCH: key '{found.name}' scopes={found.scopes} tenants={slugs} revoked={found.revoked} expires={found.expires_at}")
            if fixed:
                print("The text you gave had look-alike characters wrong (0/O or 1/l/I). Use exactly this instead:")
                print(fixed)
        elif a.cmd == "list-tenants":
            for t in db.scalars(select(Tenant).order_by(Tenant.created_at)):
                print(f"{t.id}  slug={t.slug}  name={t.name!r}  status={t.status}  wisecp_service={t.wisecp_service_id}  chatwoot_account={t.chatwoot_account_id}")
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
                    print("     raw:", str(l.raw)[:400])
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
