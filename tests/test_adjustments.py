import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import db as database
from app.config import get_settings
from app.main import app
from app.models import AdjustmentRequest, AuditLog, LedgerEntry, ReceivableEntry, UsageReversal, Wallet
from .conftest import ADMIN, INTERNAL, H, adjust, assert_money_invariants, bind_key, credit, csrf_of, login_as, mk_tenant, staff_pair

CHARGE = 15_458_240
N = 1_000_000


def use(client, ext, key=INTERNAL, **kw):
    return client.post("/internal/usage", headers=H(key), json={"external_id": ext, "category": "utility", "country": "NG", **kw}).json()


def wallet(tid):
    with database.SessionLocal() as s:
        w = s.get(Wallet, tid)
        return w.balance_micro, w.receivable_micro, w.reserved_micro


def req(rid):
    with database.SessionLocal() as s:
        return s.get(AdjustmentRequest, rid)


def propose(maker, tid, kind="wallet_credit", amount="10", **kw):
    data = {"kind": kind, "amount": amount, "usage_id": "", "reason": "agreed with the customer", "evidence": "TKT-77", **kw}
    return maker.post(f"/ui/tenants/{tid}/adjustments", data={**data, "csrf": csrf_of(maker)}, follow_redirects=False)


def pending_id(tid):
    with database.SessionLocal() as s:
        return s.scalars(select(AdjustmentRequest).where(AdjustmentRequest.tenant_id == tid).order_by(AdjustmentRequest.requested_at.desc())).first().id


# ------------------------------------------------------------------------------------------------ the two-person rule
def test_the_proposer_cannot_approve_their_own_request(client, cw, tenant):
    maker, checker = staff_pair()
    assert propose(maker, tenant, amount="50").status_code == 303
    rid = pending_id(tenant)
    own = maker.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(maker)})
    assert own.status_code == 409 and own.json()["detail"]["code"] == "needs_second_approver"
    assert req(rid).status == "pending" and wallet(tenant) == (0, 0, 0)                      # nothing moved
    ok = checker.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(checker)}, follow_redirects=False)
    assert ok.status_code == 303 and wallet(tenant) == (50 * N, 0, 0)
    r = req(rid)
    assert (r.status, r.requested_by, r.decided_by, r.self_approved) == ("posted", "maker@citichoice.test", "checker@citichoice.test", False)
    assert checker.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(checker)}).json()["detail"]["code"] == "not_pending"
    assert wallet(tenant) == (50 * N, 0, 0)                                                   # approving twice posts once
    assert_money_invariants()


def test_both_people_are_recorded_in_the_audit_trail(client, cw, tenant):
    adjust(tenant, "wallet_credit", "10", reason="compensation for the outage", evidence="INC-9")
    with database.SessionLocal() as s:
        rows = {a.action: a for a in s.scalars(select(AuditLog).where(AuditLog.action.like("adjustment.%")))}
    assert rows["adjustment.request"].actor == "maker@citichoice.test" and rows["adjustment.request"].detail["evidence"] == "INC-9"
    assert rows["adjustment.approve"].actor == "checker@citichoice.test" and rows["adjustment.approve"].detail["requested_by"] == "maker@citichoice.test"
    assert rows["adjustment.approve"].detail["amount_micro"] == 10 * N


def test_a_rejected_request_moves_nothing_and_stays_rejected(client, cw, tenant):
    maker, checker = staff_pair()
    propose(maker, tenant, amount="50")
    rid = pending_id(tenant)
    assert checker.post(f"/ui/adjustments/{rid}/reject", data={"note": "no evidence on file", "csrf": csrf_of(checker)}, follow_redirects=False).status_code == 303
    r = req(rid)
    assert (r.status, r.decision_note) == ("rejected", "no evidence on file") and wallet(tenant) == (0, 0, 0)
    assert checker.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(checker)}).status_code == 409


def test_self_approval_only_under_the_configured_small_limit(client, cw, tenant, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(get_settings(), "adjustment_self_approval_limit_usd", Decimal("5"))      # USD 5 = NGN 8,000 at 1600
    maker, _ = staff_pair()
    propose(maker, tenant, amount="100")
    small = pending_id(tenant)
    assert maker.post(f"/ui/adjustments/{small}/approve", data={"csrf": csrf_of(maker)}, follow_redirects=False).status_code == 303
    r = req(small)
    assert r.self_approved is True and r.decided_by == r.requested_by and wallet(tenant) == (100 * N, 0, 0)
    with database.SessionLocal() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "adjustment.approve")).one().detail["self_approved"] is True
    propose(maker, tenant, amount="9000")
    big = pending_id(tenant)
    assert maker.post(f"/ui/adjustments/{big}/approve", data={"csrf": csrf_of(maker)}).json()["detail"]["code"] == "needs_second_approver"


def test_self_approval_is_off_by_default_and_needs_a_trustworthy_rate(client, cw):
    assert get_settings().adjustment_self_approval_limit_usd == 0
    tid = mk_tenant(client, cw, service="svc-kwd", name="K", currency="KWD")                    # no KWD exchange rate exists
    from decimal import Decimal
    get_settings().adjustment_self_approval_limit_usd = Decimal("5")
    try:
        maker, _ = staff_pair()
        propose(maker, tid, amount="1")
        assert maker.post(f"/ui/adjustments/{pending_id(tid)}/approve", data={"csrf": csrf_of(maker)}).status_code == 409
    finally:
        get_settings().adjustment_self_approval_limit_usd = Decimal("0")


def test_the_database_enforces_two_people_too(client, cw, tenant):
    maker, _ = staff_pair()
    propose(maker, tenant)
    with pytest.raises((IntegrityError, DBAPIError)), database.engine.begin() as c:
        c.execute(text("update adjustment_requests set status='posted', decided_by = requested_by, self_approved = false"))


def test_no_machine_key_can_adjust_a_wallet(client, cw, tenant):
    for key in (ADMIN, INTERNAL):
        r = client.post(f"/admin/wallets/{tenant}/adjust", headers=H(key), json={"amount": "999", "currency": "NGN", "reason": "x", "idempotency_key": "evil-1"})
        assert r.status_code in (404, 405)
    assert wallet(tenant) == (0, 0, 0)


# ------------------------------------------------------------------------------------------------ each kind
def test_credit_clears_debt_first(client, cw, tenant):
    use(client, "m1")
    assert adjust(tenant, "wallet_credit", "100")[0].status_code == 303
    assert wallet(tenant) == (100 * N - CHARGE, 0, 0)
    assert_money_invariants()


def test_debit_takes_only_free_funds_not_held_ones(client, cw, tenant):
    credit(client, tenant, "20", "a-1")
    client.post("/internal/reservations", headers=H(INTERNAL), json={"send_id": "s1", "category": "utility", "country": "NG"})   # holds 15.46
    resp, _ = adjust(tenant, "wallet_debit", "10")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "insufficient_funds"
    assert adjust(tenant, "wallet_debit", "4")[0].status_code == 303
    assert_money_invariants()


def test_a_failed_approval_leaves_the_request_pending_for_a_decision(client, cw, tenant):
    maker, checker = staff_pair()
    propose(maker, tenant, "wallet_debit", "5")
    rid = pending_id(tenant)
    assert checker.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(checker)}).status_code == 409
    assert req(rid).status == "pending"
    assert checker.post(f"/ui/adjustments/{rid}/reject", data={"note": "tenant has no funds", "csrf": csrf_of(checker)}, follow_redirects=False).status_code == 303


def test_writing_off_debt_moves_no_funds_and_cannot_exceed_the_debt(client, cw, tenant):
    use(client, "m1")
    assert adjust(tenant, "receivable_writeoff", "5")[0].status_code == 303
    assert wallet(tenant) == (0, CHARGE - 5 * N, 0)
    with database.SessionLocal() as s:
        assert s.scalars(select(LedgerEntry)).all() == []                                          # the wallet ledger is untouched
        assert [(e.kind, e.amount_micro) for e in s.scalars(select(ReceivableEntry).order_by(ReceivableEntry.created_at))] == [("accrual", CHARGE), ("writeoff", -5 * N)]
    resp, _ = adjust(tenant, "receivable_writeoff", "50")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "exceeds_debt"
    assert wallet(tenant) == (0, CHARGE - 5 * N, 0)
    assert_money_invariants()


def test_a_missed_charge_uses_funds_first_then_becomes_debt(client, cw, tenant):
    credit(client, tenant, "20", "a-2")
    assert adjust(tenant, "manual_charge", "30")[0].status_code == 303
    assert wallet(tenant) == (0, 10 * N, 0)
    assert_money_invariants()


def test_reversing_one_message_charge_refunds_exactly_that_charge_once(client, cw, tenant):
    credit(client, tenant, "100", "a-3")
    use(client, "wamid.X")
    assert wallet(tenant)[0] == 100 * N - CHARGE
    resp, rid = adjust(tenant, "usage_reversal", usage_id="wamid.X", reason="duplicate delivery receipt", evidence="TKT-5")
    assert resp.status_code == 303 and wallet(tenant) == (100 * N, 0, 0) and req(rid).amount_micro == CHARGE
    with database.SessionLocal() as s:
        rev = s.scalars(select(UsageReversal)).one()
        assert rev.amount_micro == CHARGE and rev.request_id == rid
    maker, _ = staff_pair()
    again = propose(maker, tenant, "usage_reversal", "", usage_id="wamid.X")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_reversed"
    assert_money_invariants()


def test_reversing_a_charge_that_became_debt_reduces_the_debt(client, cw, tenant):
    use(client, "wamid.Y")                                                                         # no funds: the whole charge is owed
    assert wallet(tenant) == (0, CHARGE, 0)
    assert adjust(tenant, "usage_reversal", usage_id="wamid.Y")[0].status_code == 303
    assert wallet(tenant) == (0, 0, 0)
    assert_money_invariants()


def test_only_real_charged_messages_of_this_tenant_can_be_reversed(client, cw, tenant):
    other = mk_tenant(client, cw, service="svc-2", name="Other")
    bind_key("chd_other", [other])
    use(client, "theirs", key="chd_other")
    use(client, "free-one", category="service") if False else None
    maker, _ = staff_pair()
    for uid_ in ("does-not-exist", "theirs"):
        r = propose(maker, tenant, "usage_reversal", "", usage_id=uid_)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "no_such_charge"
    assert wallet(other)[1] == CHARGE                                                              # their charge is untouched


@pytest.mark.parametrize("kw,code", [({"reason": "x"}, "reason_required"), ({"evidence": ""}, "reason_required"),
                                     ({"amount": "0"}, "bad_amount"), ({"amount": "-5"}, "bad_amount"),
                                     ({"amount": "10.001"}, "bad_amount"), ({"kind": "teleport"}, "bad_kind")])
def test_bad_proposals_are_refused(client, cw, tenant, kw, code):
    maker, _ = staff_pair()
    r = propose(maker, tenant, **kw) if "kind" not in kw else maker.post(f"/ui/tenants/{tenant}/adjustments", data={"kind": kw["kind"], "amount": "10", "reason": "agreed here", "evidence": "TKT-1", "csrf": csrf_of(maker)})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code
    with database.SessionLocal() as s:
        assert s.scalars(select(AdjustmentRequest)).all() == []


def test_the_adjustments_page_lists_requests_for_staff_only(client, cw, tenant):
    maker, checker = staff_pair()
    propose(maker, tenant, amount="12.50")
    page = checker.get("/ui/adjustments")
    assert page.status_code == 200 and "12.50" in page.text and "Approve" in page.text and "maker@citichoice.test" in page.text
    assert TestClient(app, base_url="https://deskhub.test").get("/ui/adjustments", follow_redirects=False).status_code == 303


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs PostgreSQL for real concurrency")
def test_simultaneous_approvals_post_exactly_once(client, cw, tenant):
    maker, _ = staff_pair()
    propose(maker, tenant, amount="40")
    rid = pending_id(tenant)
    codes = []

    def approve(i):
        c = TestClient(app, base_url="https://deskhub.test")
        login_as(c, sub=f"checker-{i}", email=f"checker{i}@citichoice.test")
        codes.append(c.post(f"/ui/adjustments/{rid}/approve", data={"csrf": csrf_of(c)}, follow_redirects=False).status_code)

    threads = [threading.Thread(target=approve, args=(i,)) for i in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(codes) == [303] + [409] * 7
    assert wallet(tenant) == (40 * N, 0, 0)
    assert_money_invariants()
