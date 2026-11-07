import datetime as dt
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update

os.environ.update({
    "OIDC_ISSUER": "https://idp.test", "OIDC_CLIENT_ID": "client-123", "OIDC_CLIENT_SECRET": "s3cret-for-tests",
    "OIDC_REDIRECT_URI": "https://deskhub.test/auth/callback", "OIDC_POST_LOGOUT_URI": "https://deskhub.test/auth/logged-out",
    "OIDC_PROJECT_ID": "proj-1", "STAFF_UI_HOST": "deskhub.test", "API_HOST": "hub.test"})
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENV"] = "test"

from app import db as database                                  # noqa: E402
from app.main import app                                        # noqa: E402
from app.models import ApiKey, ApiKeyTenant, Base, FxRate, ProvisioningJob, RateCard, StaffSession, now  # noqa: E402
from app.routers.admin import get_chatwoot                      # noqa: E402
from app.security import hash_key                               # noqa: E402
from app.services import currencies as currency_svc, entitlements, jobs                     # noqa: E402
from app.services.chatwoot import ProvisioningError             # noqa: E402

ADMIN, INTERNAL = "chd_admin_key", "chd_internal_key"


class FakeChatwoot:
    """Stands in for Chatwoot. Failures can be scripted per operation:
    fake.fail["create_account"] = [(ambiguous, took_effect), ...]  (consumed one per call)."""

    def __init__(self):
        self.n, self.accounts, self.users, self.calls, self.fail = 100, {}, {}, [], {}

    def _do(self, op, effect, *args):
        f = self.fail.get(op, []).pop(0) if self.fail.get(op) else None
        if f and not f[1]:
            raise ProvisioningError(f"scripted failure in {op}", ambiguous=f[0])
        out = effect()
        self.calls.append((op,) + args)
        if f:
            raise ProvisioningError(f"scripted failure after effect in {op}", ambiguous=f[0])
        return out

    def create_account(self, name, limits=None):
        def eff():
            self.n += 1
            self.accounts[self.n] = {"name": name, "status": "active", "limits": limits}
            return self.n
        return self._do("create_account", eff, name)

    def update_account(self, i, **fields):
        return self._do("update_account", lambda: self.accounts[i].update(fields), i, fields)

    def set_status(self, i, status):
        return self._do("set_status", lambda: self.accounts[i].update(status=status), i, status)

    def delete_account(self, i):
        raise AssertionError("the hub must never delete Chatwoot accounts in Phase 2")

    def create_user(self, name, email):
        def eff():
            self.n += 1
            self.users[self.n] = email
            return self.n
        return self._do("create_user", eff, email)

    def add_user(self, account_id, user_id, role="administrator"):
        return self._do("add_user", lambda: None, account_id, user_id)

    def login_link(self, user_id):
        return f"https://desk.test/login/{user_id}/one-time-secret"

    def count(self, op):
        return sum(1 for c in self.calls if c[0] == op)


def H(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture()
def cw():
    return FakeChatwoot()


@pytest.fixture()
def client(cw):
    url = os.environ.get("TEST_DATABASE_URL") or "sqlite:///:memory:"
    database.configure(url)
    if not url.startswith("sqlite"):
        with database.engine.begin() as c:
            c.execute(text("drop schema public cascade; create schema public"))
    Base.metadata.create_all(database.engine)
    with database.SessionLocal() as s:
        currency_svc.seed_currencies(s, enabled=("NGN", "USD", "JPY", "KWD"))
        entitlements.seed_plans(s)
        s.add_all([
            ApiKey(name="admin-test", key_hash=hash_key(ADMIN), scopes=["admin"]),
            ApiKey(id="k_internal", name="n8n-test", key_hash=hash_key(INTERNAL), scopes=["internal"]),
            fx_row("NGN", 1600),
            RateCard(category="utility", market="*", currency="USD", rate_micro=6_700, effective_from=dt.date(2020, 1, 1), source="test"),
            RateCard(category="marketing", market="*", currency="USD", rate_micro=51_600, effective_from=dt.date(2020, 1, 1), source="test"),
        ])
        s.commit()
    app.dependency_overrides[get_chatwoot] = lambda: cw
    yield TestClient(app, base_url="https://hub.test")           # machine API host
    app.dependency_overrides.clear()


@pytest.fixture()
def staff_client(client):
    """Browser-style client for the staff UI host, signed in as an administrator."""
    c = TestClient(app, base_url="https://deskhub.test")
    login_as(c)
    return c


def login_as(c, roles=("administrator",), sub="staff-1", email="admin@citichoice.test"):
    from app.auth import staff
    with database.SessionLocal() as s:
        token = staff.create_session(s, sub, email, "Admin", list(roles))
        s.commit()
    c.cookies.set(staff.COOKIE, token, domain="deskhub.test")
    return token


def csrf_of(c):
    from app.auth import staff
    import hashlib
    tok = c.cookies.get(staff.COOKIE)
    with database.SessionLocal() as s:
        return s.get(StaffSession, hashlib.sha256(tok.encode()).hexdigest()).csrf_token


def fx_row(quote, rate, base="USD", hours_ago=1, source="test"):
    """An exchange rate observed an hour ago, i.e. fresh enough for the 48-hour staleness rule."""
    return FxRate(base_currency=base, quote_currency=quote, rate=rate, source=source,
                  fetched_at=now() - dt.timedelta(hours=hours_ago), effective_from=now() - dt.timedelta(hours=hours_ago))


def make_due():
    with database.SessionLocal() as s:
        s.execute(update(ProvisioningJob).where(ProvisioningJob.status == "queued").values(next_attempt_at=now()))
        s.commit()


def run_jobs(cw, max_jobs=50):
    """Runs the worker until nothing is due. Backoff is skipped so tests are instant."""
    ran = []
    for _ in range(max_jobs):
        make_due()
        with database.SessionLocal() as s:
            j = jobs.run_once(s, cw, "test-worker")
            if not j:
                break
            ran.append((j.operation, j.status))
    return ran


def mk_tenant(client, cw, service="svc-1", name="BennyBoom", plan="growth", run=True, currency="NGN", **extra):
    body = {"wisecp_service_id": service, "billing_currency": currency, "name": name, "owner_email": f"{service}@example.com", "plan": plan, **extra}
    r = client.post("/admin/tenants", headers=H(ADMIN), json=body)
    assert r.status_code == 200, r.text
    if run:
        run_jobs(cw)
    return r.json()["tenant"]["id"]


def bind_key(raw, tenant_ids, name=None, scope="internal"):
    with database.SessionLocal() as s:
        k = ApiKey(name=name or raw, key_hash=hash_key(raw), scopes=[scope])
        s.add(k)
        s.flush()
        for t in tenant_ids:
            s.add(ApiKeyTenant(api_key_id=k.id, tenant_id=t))
        s.commit()


@pytest.fixture()
def tenant(client, cw):
    tid = mk_tenant(client, cw)
    with database.SessionLocal() as s:
        s.add(ApiKeyTenant(api_key_id="k_internal", tenant_id=tid))
        s.commit()
    return tid


def credit(client, tid, amount, key, currency="NGN"):
    """Top up a wallet through the admin API. `amount` is in MAJOR units, e.g. "10000" or "0.50"."""
    return client.post(f"/admin/wallets/{tid}/credit", headers=H(ADMIN),
                       json={"amount": str(amount), "currency": currency, "idempotency_key": key})


def assert_money_invariants():
    """The rules that must hold after ANY sequence of operations."""
    from sqlalchemy import func, select
    from app.models import LedgerEntry, ReceivableEntry, Reservation, Wallet
    with database.SessionLocal() as s:
        for w in s.scalars(select(Wallet)):
            ledger = s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_micro), 0)).where(LedgerEntry.tenant_id == w.tenant_id))
            owed = s.scalar(select(func.coalesce(func.sum(ReceivableEntry.amount_micro), 0)).where(ReceivableEntry.tenant_id == w.tenant_id))
            held = s.scalar(select(func.coalesce(func.sum(Reservation.amount_micro), 0)).where(Reservation.tenant_id == w.tenant_id, Reservation.state == "held"))
            assert w.balance_micro == ledger, "balance must equal the sum of the ledger"
            assert w.receivable_micro == owed, "receivable must equal the sum of the receivable ledger"
            assert w.reserved_micro == held, "reserved must equal the sum of held reservations"
            assert w.balance_micro >= 0 and w.receivable_micro >= 0 and w.reserved_micro >= 0
            assert w.balance_micro == 0 or w.receivable_micro == 0, "funds and debt must never coexist"


def staff_pair():
    """Two different signed-in staff members on the staff host: a maker and a checker."""
    maker = TestClient(app, base_url="https://deskhub.test")
    login_as(maker, sub="staff-maker", email="maker@citichoice.test")
    checker = TestClient(app, base_url="https://deskhub.test")
    login_as(checker, sub="staff-checker", email="checker@citichoice.test")
    return maker, checker


def adjust(tid, kind, amount=None, usage_id=None, reason="agreed with the customer", evidence="TKT-1001", approve=True, pair=None):
    """Proposes a manual adjustment as one person and approves (or rejects) it as another. Returns (decision response, request id)."""
    from sqlalchemy import select
    from app.models import AdjustmentRequest
    maker, checker = pair or staff_pair()
    r = maker.post(f"/ui/tenants/{tid}/adjustments", data={"kind": kind, "amount": amount or "", "usage_id": usage_id or "",
                                                           "reason": reason, "evidence": evidence, "csrf": csrf_of(maker)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with database.SessionLocal() as s:
        rid = s.scalars(select(AdjustmentRequest).where(AdjustmentRequest.tenant_id == tid).order_by(AdjustmentRequest.requested_at.desc())).first().id
    resp = checker.post(f"/ui/adjustments/{rid}/{'approve' if approve else 'reject'}", data={"csrf": csrf_of(checker)}, follow_redirects=False)
    return resp, rid
