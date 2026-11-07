"""Run against real PostgreSQL: TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/test_postgres.py"""
import os
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

from app import db as database
from app.models import Base, LedgerEntry, Tenant, Wallet
from app.services import wallet as wl

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def pg():
    database.configure(URL)
    with database.engine.begin() as c:
        c.execute(text("drop schema public cascade; create schema public"))
    Base.metadata.create_all(database.engine)
    with database.SessionLocal() as s:
        from app.services.currencies import seed_currencies
        from app.services.entitlements import create_entitlement, get_plan, seed_plans
        seed_currencies(s, enabled=("NGN",))
        seed_plans(s)
        t = Tenant(id="t1", slug="t1", name="T", owner_email="a@b.co", status="active", billing_currency="NGN")
        s.add(t)
        s.flush()
        create_entitlement(s, t, get_plan(s, "growth"), {})
        s.add(Wallet(tenant_id="t1", currency="NGN", balance_micro=10000))
        s.commit()
    yield


def _debit(i):
    with database.SessionLocal() as s:
        try:
            wl.apply(s, "t1", -3000, "debit", f"spend-{i}")
            s.commit()
            return "ok"
        except wl.InsufficientFunds:
            s.rollback()
            return "refused"


def test_concurrent_debits_never_overspend(pg):
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(_debit, range(10)))
    with database.SessionLocal() as s:
        bal = s.get(Wallet, "t1").balance_micro
        rows = s.scalars(select(LedgerEntry)).all()
    assert res.count("ok") == 3 and bal == 1000 and len(rows) == 3
    assert bal == 10000 + sum(r.amount_micro for r in rows)


def _credit_same(_):
    with database.SessionLocal() as s:
        e, created = wl.apply(s, "t1", 5000, "credit", "wisecp-addon-77")
        s.commit()
        return created


def test_concurrent_duplicate_topups_credit_once(pg):
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(_credit_same, range(8)))
    with database.SessionLocal() as s:
        assert s.get(Wallet, "t1").balance_micro == 15000
    assert res.count(True) == 1


def test_concurrent_workers_claim_each_job_exactly_once(pg):
    from app.models import ProvisioningJob
    from app.services import jobs
    with database.SessionLocal() as s:
        for i in range(12):
            s.add(ProvisioningJob(tenant_id="t1", operation="sync_limits", idempotency_key=f"j{i}", payload={}, result={}))
        s.commit()

    def worker(n):
        got = []
        with database.SessionLocal() as s:
            while (j := jobs.claim_next(s, f"w{n}")) is not None:
                got.append(j.id)
        return got

    with ThreadPoolExecutor(6) as ex:
        claimed = [i for sub in ex.map(worker, range(6)) for i in sub]
    assert len(claimed) == 12 and len(set(claimed)) == 12


def test_concurrent_channel_creates_cannot_exceed_the_plan_limit(client, cw):
    from .conftest import ADMIN, H, mk_tenant
    tid = mk_tenant(client, cw, plan="starter")          # 1 messaging channel

    def add(i):
        with database.SessionLocal() as s:
            from app.services import channels
            from fastapi import HTTPException
            try:
                channels.create_channel(s, tid, dict(channel_type="whatsapp", display_name=f"n{i}", external_id=f"pn-{i}",
                                                     provider="chatwoot_native", status="pending"))
                s.commit()
                return "ok"
            except HTTPException:
                s.rollback()
                return "refused"
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(add, range(6)))
    assert res.count("ok") == 1


def test_database_refuses_overlapping_rate_cards(pg):
    from sqlalchemy.exc import IntegrityError
    from app.models import RateCard
    import datetime as dt
    with database.SessionLocal() as s:
        s.add(RateCard(category="utility", market="*", currency="NGN", rate_micro=1, effective_from=dt.date(2026, 1, 1), source="t"))
        s.commit()
        s.add(RateCard(category="utility", market="*", currency="NGN", rate_micro=2, effective_from=dt.date(2026, 6, 1), source="t"))
        with pytest.raises(IntegrityError):
            s.commit()                                      # the open-ended first card still covers June: refused by PostgreSQL itself


# ---------------------------------------------------------------------------------- 3.5 / 3.6 concurrency on PostgreSQL
def _set_wallet(**cols):
    with database.SessionLocal() as s:
        w = s.get(Wallet, "t1")
        for k, v in cols.items():
            setattr(w, k, v)
        s.commit()


def test_concurrent_holds_never_exceed_funds_plus_credit(pg):
    from app.services import reservations
    from tests.conftest import assert_money_invariants
    E = 1_000
    _set_wallet(balance_micro=0, credit_limit_micro=10 * E)            # room for exactly ten holds, all of it on credit

    def hold(i):
        with database.SessionLocal() as s:
            r, created, why = reservations.reserve(s, "t1", f"send-{i}", E)
            s.commit()
            return r is not None

    with ThreadPoolExecutor(20) as ex:
        res = list(ex.map(hold, range(60)))
    assert res.count(True) == 10
    with database.SessionLocal() as s:
        assert s.get(Wallet, "t1").reserved_micro == 10 * E


def test_concurrent_duplicate_provider_charges_are_recorded_once(pg):
    _set_wallet(balance_micro=300)

    def charge(_):
        with database.SessionLocal() as s:
            p = wl.charge(s, "t1", 500, "usage:meta_whatsapp:dup")
            s.commit()
            return p.created

    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(charge, range(12)))
    assert res.count(True) == 1
    with database.SessionLocal() as s:
        w = s.get(Wallet, "t1")
        assert (w.balance_micro, w.receivable_micro) == (0, 200)            # 300 from funds, 200 owed: once


def test_concurrent_payments_and_charges_keep_every_rule_intact(pg):
    import random
    from app.services import reservations
    from tests.conftest import assert_money_invariants
    _set_wallet(balance_micro=0)                                          # the fixture's opening balance has no ledger entry behind it
    with database.SessionLocal() as s:                                    # start from a consistent, ledger-backed state
        wl.credit(s, "t1", 5_000, "seed-1"); s.commit()
    _set_wallet(credit_limit_micro=3_000)

    def op(i):
        rnd = random.Random(i)
        with database.SessionLocal() as s:
            try:
                kind = rnd.choice(["credit", "charge", "charge", "hold", "debit"])
                if kind == "credit":
                    wl.credit(s, "t1", rnd.randint(1, 900), f"c-{i}")
                elif kind == "charge":
                    wl.charge(s, "t1", rnd.randint(1, 900), f"ch-{i}")
                elif kind == "hold":
                    reservations.reserve(s, "t1", f"h-{i}", rnd.randint(1, 600))
                else:
                    wl.debit(s, "t1", rnd.randint(1, 300), f"d-{i}")
                s.commit()
            except wl.InsufficientFunds:
                s.rollback()

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(op, range(300)))
    assert_money_invariants()
