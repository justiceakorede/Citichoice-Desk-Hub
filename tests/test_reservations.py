import datetime as dt

import pytest
from sqlalchemy import select, update

from app import db as database
from app.models import Reservation, Wallet, now
from app.services import reservations
from .conftest import ADMIN, INTERNAL, H, assert_money_invariants, bind_key, credit, mk_tenant

CHARGE = 15_458_240            # also the hold for one utility message
N = 1_000_000


def reserve(client, send_id, key=INTERNAL, category="utility", **kw):
    return client.post("/internal/reservations", headers=H(key), json={"send_id": send_id, "category": category, "country": "NG", **kw}).json()


def usage(client, ext, key=INTERNAL, **kw):
    return client.post("/internal/usage", headers=H(key), json={"external_id": ext, "category": "utility", "country": "NG", **kw}).json()


def wallet(tid):
    with database.SessionLocal() as s:
        w = s.get(Wallet, tid)
        return w.balance_micro, w.receivable_micro, w.reserved_micro


def limit(client, tid, amount):
    assert client.patch(f"/admin/wallets/{tid}/credit-limit", headers=H(ADMIN), json={"amount": str(amount), "currency": "NGN", "reason": "test"}).status_code == 200


def test_a_hold_reduces_what_can_still_be_spent(client, cw, tenant):
    credit(client, tenant, "100", "h-1")
    r = reserve(client, "send-1")
    assert r["allowed"] and r["amount_micro"] == CHARGE and r["state"] == "held"
    assert wallet(tenant) == (100 * N, 0, CHARGE)
    w = client.get(f"/admin/tenants/{tenant}", headers=H(ADMIN)).json()["wallet"]
    assert w["available_micro"] == 100 * N - CHARGE and w["capacity_micro"] == 100 * N - CHARGE
    assert_money_invariants()


def test_the_same_send_id_returns_the_same_hold(client, cw, tenant):
    credit(client, tenant, "100", "h-2")
    a, b = reserve(client, "send-1"), reserve(client, "send-1")
    assert a["reservation_id"] == b["reservation_id"] and b["created"] is False
    assert wallet(tenant)[2] == CHARGE                                                  # held once, not twice


def test_no_funds_and_no_credit_means_no_approval(client, cw, tenant):
    r = reserve(client, "send-1")
    assert r["allowed"] is False and r["reason"] == "insufficient_funds"
    assert wallet(tenant) == (0, 0, 0)


def test_credit_limit_allows_sending_up_to_the_limit_and_no_further(client, cw, tenant):
    limit(client, tenant, "50")                                                         # room for three messages (46.37), not four
    assert [reserve(client, f"s{i}")["allowed"] for i in range(4)] == [True, True, True, False]
    assert reserve(client, "s3")["reason"] == "credit_limit_reached"
    assert_money_invariants()


def test_debt_counts_against_the_credit_limit(client, cw, tenant):
    limit(client, tenant, "50")
    usage(client, "late-1")                                                             # an uncontrolled send was charged: debt 15.46
    cap = client.get(f"/admin/tenants/{tenant}", headers=H(ADMIN)).json()["wallet"]["capacity_micro"]
    assert cap == 50 * N - CHARGE
    assert [reserve(client, f"s{i}")["allowed"] for i in range(3)] == [True, True, False]


def test_incurred_charges_are_recorded_beyond_the_limit_but_do_not_open_the_gate(client, cw, tenant):
    limit(client, tenant, "20")
    for i in range(4):                                                                  # four sends the hub never saw: 61.8 owed against a 20 limit
        assert usage(client, f"late-{i}")["recorded"] is True
    assert wallet(tenant) == (0, 4 * CHARGE, 0)
    r = reserve(client, "s1")
    assert r["allowed"] is False and r["reason"] == "credit_limit_reached"              # recording a debt is not permission to send
    credit(client, tenant, "100", "h-3")                                                # a payment clears the debt, then funds the wallet
    assert reserve(client, "s2")["allowed"] is True
    assert_money_invariants()


def test_release_returns_the_hold_and_is_safe_to_repeat(client, cw, tenant):
    credit(client, tenant, "100", "h-4")
    rid = reserve(client, "s1")["reservation_id"]
    assert client.post(f"/internal/reservations/{rid}/release", headers=H(INTERNAL)).json() == {"released": True, "state": "released"}
    assert client.post(f"/internal/reservations/{rid}/release", headers=H(INTERNAL)).json()["released"] is False
    assert wallet(tenant) == (100 * N, 0, 0)
    assert_money_invariants()


def test_a_delivered_message_settles_its_hold_exactly_once(client, cw, tenant):
    credit(client, tenant, "100", "h-5")
    rid = reserve(client, "s1")["reservation_id"]
    r = usage(client, "wamid.1", send_id="s1")
    assert r["charge_micro"] == CHARGE and wallet(tenant) == (100 * N - CHARGE, 0, 0)
    with database.SessionLocal() as s:
        res = s.get(Reservation, rid)
        assert res.state == "settled" and res.settled_micro == CHARGE and res.closed_at is not None
    usage(client, "wamid.1", send_id="s1")                                              # a replay changes nothing
    assert wallet(tenant) == (100 * N - CHARGE, 0, 0)
    assert_money_invariants()


def test_a_charge_with_no_live_hold_is_still_recorded(client, cw, tenant):
    credit(client, tenant, "100", "h-6")
    usage(client, "wamid.9", send_id="never-reserved")                                  # unknown send id
    rid = reserve(client, "s2")["reservation_id"]
    client.post(f"/internal/reservations/{rid}/release", headers=H(INTERNAL))
    usage(client, "wamid.10", send_id="s2")                                             # hold was released, message went anyway
    assert wallet(tenant) == (100 * N - 2 * CHARGE, 0, 0)
    assert_money_invariants()


def test_holds_that_never_get_an_outcome_expire_and_free_the_money(client, cw, tenant):
    credit(client, tenant, "100", "h-7")
    rid = reserve(client, "s1")["reservation_id"]
    with database.SessionLocal() as s:
        s.execute(update(Reservation).where(Reservation.id == rid).values(expires_at=now() - dt.timedelta(hours=1)))
        s.commit()
        assert reservations.expire_due(s) == 1
        s.commit()
    assert wallet(tenant) == (100 * N, 0, 0)
    usage(client, "wamid.late", send_id="s1")                                           # the provider charged after expiry: still recorded
    assert wallet(tenant) == (100 * N - CHARGE, 0, 0)
    with database.SessionLocal() as s:
        assert s.get(Reservation, rid).state == "expired"
    assert_money_invariants()


def test_service_messages_need_no_hold(client, cw, tenant):
    r = reserve(client, "s1", category="service")
    assert r["allowed"] is True and r["reservation_id"] is None and r["amount_micro"] == 0


def test_suspended_tenants_get_no_new_holds(client, cw, tenant):
    credit(client, tenant, "100", "h-8")
    client.post(f"/admin/tenants/{tenant}/suspend", headers=H(ADMIN), json={"mode": "pause_outbound"})
    assert reserve(client, "s1")["reason"] == "tenant_suspended"


def test_marketing_must_rest_on_the_tenants_own_funds_never_on_credit(client, cw, tenant):
    limit(client, tenant, "1000")
    r = reserve(client, "m1", category="marketing")
    assert r["allowed"] is False and r["reason"] == "below_marketing_minimum"           # credit cannot satisfy the funded minimum
    credit(client, tenant, "9000", "h-9")                                               # floor is USD 5 = NGN 8,000
    assert reserve(client, "m1", category="marketing")["allowed"] is True


def test_one_tenants_key_cannot_release_settle_or_hold_for_another(client, cw, tenant):
    other = mk_tenant(client, cw, service="svc-2", name="Other")
    bind_key("chd_other", [other])
    credit(client, tenant, "100", "h-10"); credit(client, other, "100", "h-11")
    mine = reserve(client, "shared-id")["reservation_id"]
    assert client.post(f"/internal/reservations/{mine}/release", headers=H("chd_other")).status_code == 404
    assert client.post("/internal/reservations", headers=H("chd_other"), json={"tenant_id": tenant, "send_id": "x", "category": "utility"}).status_code == 404
    theirs = reserve(client, "shared-id", key="chd_other")                              # same send_id string, different tenant: a different hold
    assert theirs["reservation_id"] != mine
    usage(client, "wamid.o", key="chd_other", send_id="shared-id")                      # settles THEIR hold only
    assert wallet(tenant) == (100 * N, 0, CHARGE)
    assert wallet(other) == (100 * N - CHARGE, 0, 0)
    assert_money_invariants()
