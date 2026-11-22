"""Add Funds and automatic recharge: invoices are raised, watched and credited through the existing once-only crediting."""
import datetime as dt
from decimal import Decimal

import pytest

from app import db as database
from app.main import app
from app.models import AutoRecharge, RechargeAttempt, Wallet
from app.routers.admin import get_wisecp
from app.services import auto_recharge as rch
from app.services.wisecp import Invoice, WisecpError, parse_invoice
from .conftest import ADMIN, H, mk_tenant

SERVICE = "15"
CLIENT = "6"


class FakeWisecp:
    def __init__(self):
        self.created, self.cancelled, self.status, self.fail_create = [], [], {}, None

    def create_topup_invoice(self, client_id, service_id, currency, amount, description, *, pay_from_balance, note=""):
        if self.fail_create:
            raise WisecpError(self.fail_create, "refused")
        iid = str(100 + len(self.created))
        self.created.append({"id": iid, "client": client_id, "service": service_id, "currency": currency, "amount": amount, "balance": pay_from_balance})
        self.status[iid] = "unpaid"
        return Invoice(invoice_id=iid, status="unpaid", currency=currency, total=amount)

    def get_invoice(self, iid):
        c = next(x for x in self.created if x["id"] == iid)
        paid = self.status[iid] == "paid"
        raw = {"data": {"id": iid, "status": self.status[iid], "currency": c["currency"], "total": str(c["amount"]),
                        "items": [{"id": int(iid) * 10, "description": "WhatsApp Prepaid Wallet Top-Up", "quantity": 1, "amount": str(c["amount"]), "user_pid": SERVICE}],
                        "payments": [{"amount_in": str(c["amount"]), "amount_out": "0", "pmethod": "Balance" if c["balance"] else "Paystack"}] if paid else []}}
        return parse_invoice(raw)

    def cancel_invoice(self, iid):
        self.cancelled.append(iid)
        self.status[iid] = "cancelled"


@pytest.fixture()
def fw():
    f = FakeWisecp()
    app.dependency_overrides[get_wisecp] = lambda: f
    return f


@pytest.fixture()
def tid(client, cw):
    return mk_tenant(client, cw, service=SERVICE)


def balance(tid):
    with database.SessionLocal() as s:
        return s.get(Wallet, tid).balance_micro


def run(fw, at=None):
    with database.SessionLocal() as s:
        return rch.run(s, fw, at)


def save(client, tid, **over):
    body = {"enabled": True, "threshold": "5000", "amount": "20000", "monthly_cap": "60000", "client_id": CLIENT, **over}
    return client.put(f"/admin/tenants/{tid}/auto-recharge", headers=H(ADMIN), json=body)


def test_add_funds_raises_an_invoice_and_credits_once_when_paid(client, tid, fw):
    r = client.post(f"/admin/tenants/{tid}/add-funds", headers=H(ADMIN), json={"amount": "25000", "client_id": CLIENT})
    assert r.status_code == 200, r.text
    inv = fw.created[0]
    assert inv["amount"] == Decimal("25000") and inv["currency"] == "NGN" and inv["service"] == SERVICE and inv["balance"] is False
    assert run(fw)["credited"] == 0 and balance(tid) == 0            # unpaid: nothing credited
    fw.status[inv["id"]] = "paid"
    assert run(fw)["credited"] == 1 and balance(tid) == 25_000_000_000
    assert run(fw)["credited"] == 0 and balance(tid) == 25_000_000_000   # never twice


def test_add_funds_rejects_tiny_amounts_and_missing_customer(client, tid, fw):
    assert client.post(f"/admin/tenants/{tid}/add-funds", headers=H(ADMIN), json={"amount": "100", "client_id": CLIENT}).json()["detail"]["code"] == "amount_too_small"
    assert client.post(f"/admin/tenants/{tid}/add-funds", headers=H(ADMIN), json={"amount": "25000", "client_id": "x"}).json()["detail"]["code"] == "no_client"
    assert fw.created == []


def test_wallet_settings_view_has_presets_and_limits(client, tid, fw):
    v = client.get(f"/admin/tenants/{tid}/wallet-settings", headers=H(ADMIN)).json()
    assert v["currency"] == "NGN" and v["presets"][:2] == [10000, 25000] and v["min_amount"] == "8000.00" and v["auto"]["enabled"] is False


def test_auto_recharge_validates_its_settings(client, tid, fw):
    assert save(client, tid, client_id=None).json()["detail"]["code"] == "no_client"
    assert save(client, tid, amount="100").json()["detail"]["code"] == "amount_too_small"
    assert save(client, tid, monthly_cap="10000").json()["detail"]["code"] == "cap_below_amount"
    assert save(client, tid, threshold="abc").json()["detail"]["code"] == "bad_amount"
    assert save(client, tid).status_code == 200


def test_low_balance_starts_one_recharge_paid_from_account_balance(client, tid, fw):
    save(client, tid)                                                  # wallet is 0, below the 5,000 threshold
    assert run(fw)["started"] == 1
    inv = fw.created[0]
    assert inv["balance"] is True and inv["amount"] == Decimal("20000") and inv["client"] == CLIENT
    assert run(fw)["started"] == 0 and len(fw.created) == 1           # one open recharge at a time
    fw.status[inv["id"]] = "paid"
    assert run(fw)["credited"] == 1 and balance(tid) == 20_000_000_000


def test_healthy_balance_and_disabled_settings_start_nothing(client, tid, fw):
    save(client, tid, enabled=False)
    assert run(fw)["started"] == 0
    from .conftest import credit
    credit(client, tid, "9000", "rc-1")
    save(client, tid)
    assert run(fw)["started"] == 0                                    # 9,000 is above the 5,000 threshold


def test_unpaid_recharge_is_cancelled_after_the_timeout_and_pauses_after_repeated_failures(client, tid, fw):
    save(client, tid)
    t0 = dt.datetime.now(dt.timezone.utc)
    for i in range(3):
        at = t0 + dt.timedelta(hours=2 * i)
        run(fw, at)                                                    # starts a recharge
        run(fw, at + dt.timedelta(minutes=31))                         # still unpaid after 31 minutes: cancelled, one failure
    assert len(fw.cancelled) == 3
    with database.SessionLocal() as s:
        a = s.get(AutoRecharge, tid)
        assert a.consecutive_failures == 3 and a.paused_reason == "failed_3_times"
    assert run(fw, t0 + dt.timedelta(days=2))["started"] == 0         # paused: no more attempts
    assert save(client, tid).status_code == 200                       # the customer switching it on again clears the pause
    assert run(fw, t0 + dt.timedelta(days=2))["started"] == 1


def test_wisecp_refusing_the_invoice_counts_as_a_failure_and_is_recorded(client, tid, fw):
    save(client, tid)
    fw.fail_create = "currency_unknown"
    assert run(fw)["started"] == 0
    with database.SessionLocal() as s:
        att = s.query(RechargeAttempt).one()
        assert att.status == "failed" and "currency_unknown" in att.error
        assert s.get(AutoRecharge, tid).consecutive_failures == 1


def test_monthly_cap_stops_further_recharges(client, tid, fw):
    save(client, tid, monthly_cap="20000")                             # room for exactly one 20,000 recharge
    run(fw)
    fw.status[fw.created[0]["id"]] = "paid"
    run(fw)
    assert balance(tid) == 20_000_000_000
    with database.SessionLocal() as s:                                  # spend the wallet down below the threshold
        w = s.get(Wallet, tid); w.balance_micro = 1_000_000_000; s.commit()
    assert run(fw, dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))["started"] == 0
    with database.SessionLocal() as s:
        assert s.get(AutoRecharge, tid).paused_reason == "monthly_cap_reached"
    assert len(fw.created) == 1
