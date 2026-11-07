import random

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db as database
from app.models import AuditLog, LedgerEntry, ReceivableEntry, Wallet
from .conftest import ADMIN, INTERNAL, H, adjust, assert_money_invariants, credit, mk_tenant

CHARGE = 15_458_240            # one utility message to a growth tenant: 0.0067 USD x 1600 x 1.03 x 1.4
N = 1_000_000


def use(client, ext, key=INTERNAL, **kw):
    return client.post("/internal/usage", headers=H(key), json={"external_id": ext, "category": "utility", "country": "NG", **kw})


def wallet(tid):
    with database.SessionLocal() as s:
        w = s.get(Wallet, tid)
        return w.balance_micro, w.receivable_micro, w.reserved_micro


def test_a_charge_within_funds_is_paid_from_funds(client, cw, tenant):
    credit(client, tenant, "100", "r-1")
    r = use(client, "m1").json()
    assert (r["paid_from_funds_micro"], r["added_to_receivable_micro"]) == (CHARGE, 0)
    assert wallet(tenant) == (100 * N - CHARGE, 0, 0)
    assert_money_invariants()


def test_a_charge_beyond_funds_becomes_a_receivable_never_a_negative_balance(client, cw, tenant):
    credit(client, tenant, "20", "r-2")
    use(client, "m1")
    r = use(client, "m2").json()
    left = 20 * N - CHARGE
    assert (r["paid_from_funds_micro"], r["added_to_receivable_micro"]) == (left, CHARGE - left)
    assert wallet(tenant) == (0, CHARGE - left, 0)
    w = client.get(f"/admin/tenants/{tenant}", headers=H(ADMIN)).json()["wallet"]
    assert w["alert"] == "debt" and w["receivable_micro"] == CHARGE - left and w["balance_micro"] == 0
    rec = client.get(f"/admin/wallets/{tenant}/receivable", headers=H(ADMIN)).json()
    assert [(e["kind"], e["amount_micro"]) for e in rec] == [("accrual", CHARGE - left)]
    assert_money_invariants()


def test_a_charge_with_no_funds_at_all_is_still_recorded(client, cw, tenant):
    r = use(client, "m1").json()
    assert r["recorded"] and r["added_to_receivable_micro"] == CHARGE and wallet(tenant) == (0, CHARGE, 0)
    with database.SessionLocal() as s:
        assert s.scalars(select(LedgerEntry)).all() == []            # nothing moved in the wallet itself
    assert_money_invariants()


def test_replaying_a_charge_adds_nothing(client, cw, tenant):
    use(client, "m1")
    again = use(client, "m1").json()
    assert again["recorded"] is False and wallet(tenant) == (0, CHARGE, 0)


def test_charges_keep_being_recorded_for_suspended_and_closed_tenants(client, cw, tenant):
    client.post(f"/admin/tenants/{tenant}/suspend", headers=H(ADMIN), json={"mode": "lock"})
    assert use(client, "m1").json()["recorded"] is True
    client.post(f"/admin/tenants/{tenant}/cancel", headers=H(ADMIN))
    from .conftest import run_jobs
    run_jobs(cw)
    slug = client.get(f"/admin/tenants/{tenant}", headers=H(ADMIN)).json()["slug"]
    assert client.post(f"/admin/tenants/{tenant}/terminate?confirm={slug}", headers=H(ADMIN)).status_code == 200
    r = use(client, "m2")
    assert r.status_code == 200 and r.json()["recorded"] is True                    # a late provider charge is never lost
    assert wallet(tenant) == (0, 2 * CHARGE, 0)


def test_a_payment_clears_the_debt_before_it_becomes_funds(client, cw, tenant):
    credit(client, tenant, "20", "r-3")
    use(client, "m1"); use(client, "m2")
    owed = 2 * CHARGE - 20 * N
    r = credit(client, tenant, "20", "r-4").json()
    assert r["debt_cleared_micro"] == owed and r["receivable_micro"] == 0 and r["balance_micro"] == 20 * N - owed
    ledger = client.get(f"/admin/wallets/{tenant}/ledger", headers=H(ADMIN)).json()
    assert [(e["kind"], e["amount_micro"]) for e in ledger[:2]][::-1] == [("credit", 20 * N), ("receivable_settlement", -owed)]
    rec = client.get(f"/admin/wallets/{tenant}/receivable", headers=H(ADMIN)).json()
    assert [(e["kind"], e["amount_micro"]) for e in rec] == [("settlement", -owed), ("accrual", owed)]
    again = credit(client, tenant, "20", "r-4").json()                                # the same payment replayed
    assert again["credited"] is False and again["balance_micro"] == 20 * N - owed
    assert_money_invariants()


def test_a_small_payment_only_reduces_the_debt(client, cw, tenant):
    use(client, "m1")
    r = credit(client, tenant, "5", "r-5").json()
    assert (r["balance_micro"], r["receivable_micro"], r["debt_cleared_micro"]) == (0, CHARGE - 5 * N, 5 * N)
    assert_money_invariants()


def test_staff_adjustments_cannot_make_the_wallet_negative(client, cw, tenant):
    credit(client, tenant, "10", "r-6")
    resp, rid = adjust(tenant, "wallet_debit", "10.01")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "insufficient_funds"
    assert adjust(tenant, "wallet_debit", "10")[0].status_code == 303
    assert wallet(tenant) == (0, 0, 0)
    assert_money_invariants()


def test_a_positive_adjustment_also_clears_debt_first(client, cw, tenant):
    use(client, "m1")
    assert adjust(tenant, "wallet_credit", "100")[0].status_code == 303
    assert wallet(tenant) == (100 * N - CHARGE, 0, 0)
    assert_money_invariants()


def test_the_database_itself_refuses_a_negative_wallet_or_funds_with_debt(client, cw, tenant):
    for sql in ("update wallets set balance_micro = -1", "update wallets set receivable_micro = -1", "update wallets set reserved_micro = -1",
                "update wallets set credit_limit_micro = -1", "update wallets set balance_micro = 5, receivable_micro = 5"):
        with pytest.raises((IntegrityError, DBAPIError)), database.engine.begin() as c:
            c.execute(text(sql))


def test_both_ledgers_are_append_only(client, cw, tenant):
    credit(client, tenant, "10", "r-7"); use(client, "m1"); use(client, "m2")
    with database.SessionLocal() as s:
        assert s.scalars(select(LedgerEntry)).first() and s.scalars(select(ReceivableEntry)).first()
    for sql in ("update ledger_entries set amount_micro = 1", "delete from ledger_entries",
                "update receivable_entries set amount_micro = 1", "delete from receivable_entries"):
        with pytest.raises((IntegrityError, DBAPIError)), database.engine.begin() as c:
            c.execute(text(sql))


# ------------------------------------------------------------------------------------------------ credit limit
def set_limit(client, tid, amount, currency="NGN", reason="agreed with finance"):
    return client.patch(f"/admin/wallets/{tid}/credit-limit", headers=H(ADMIN), json={"amount": str(amount), "currency": currency, "reason": reason})


def test_credit_limit_defaults_to_zero_and_changes_are_audited(client, cw, tenant):
    w = client.get(f"/admin/tenants/{tenant}", headers=H(ADMIN)).json()["wallet"]
    assert w["credit_limit_micro"] == 0 and w["capacity_micro"] == 0
    r = set_limit(client, tenant, "50").json()
    assert r["credit_limit_micro"] == 50 * N and r["capacity_micro"] == 50 * N
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "wallet.credit_limit")).one()
    assert row.detail["before_micro"] == 0 and row.detail["after_micro"] == 50 * N and row.detail["reason"] == "agreed with finance"


@pytest.mark.parametrize("amount,currency,status", [("-1", "NGN", 422), ("10.005", "NGN", 422), ("10", "USD", 409)])
def test_bad_credit_limits_are_refused(client, cw, tenant, amount, currency, status):
    assert set_limit(client, tenant, amount, currency).status_code == status


def test_an_internal_key_cannot_touch_the_credit_limit(client, cw, tenant):
    r = client.patch(f"/admin/wallets/{tenant}/credit-limit", headers=H(INTERNAL), json={"amount": "999", "currency": "NGN", "reason": "attempt"})
    assert r.status_code == 403
    assert wallet(tenant) == (0, 0, 0)


# ------------------------------------------------------------------------------------------------ random sequences
def test_invariants_hold_after_every_step_of_a_random_sequence(client, cw, tenant):
    rnd = random.Random(2026)
    set_limit(client, tenant, "30")
    held, n = [], 0
    from .conftest import staff_pair
    pair = staff_pair()
    for step in range(160):
        op = rnd.choice(["topup", "topup", "usage", "usage", "reserve", "reserve", "settle", "release", "adjust"])
        n += 1
        if op == "topup":
            credit(client, tenant, f"{rnd.randint(1, 5000) / 100:.2f}", f"rnd-top-{n}")
        elif op == "usage":
            use(client, f"rnd-use-{n}")
        elif op == "reserve":
            r = client.post("/internal/reservations", headers=H(INTERNAL), json={"send_id": f"rnd-send-{n}", "category": "utility", "country": "NG"}).json()
            if r.get("allowed") and r.get("reservation_id"):
                held.append((f"rnd-send-{n}", r["reservation_id"]))
        elif op == "settle" and held:
            send_id, _ = held.pop(rnd.randrange(len(held)))
            use(client, f"rnd-settle-{n}", send_id=send_id)
        elif op == "release" and held:
            _, rid = held.pop(rnd.randrange(len(held)))
            client.post(f"/internal/reservations/{rid}/release", headers=H(INTERNAL))
        elif op == "adjust":
            adjust(tenant, "wallet_debit", f"{rnd.randint(1, 800) / 100:.2f}", pair=pair)
        assert_money_invariants()
