import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db as database
from app.config import get_settings
from app.main import app
from app.models import MessageEvent, UsageRecord, Wallet
from .conftest import ADMIN, INTERNAL, H, bind_key, credit, mk_tenant

BAL = 10_000 * 1_000_000          # NGN 10,000 in micro-units
CHARGE = 15_458_240               # one utility message, from the earlier pricing tests


def ev(client, ext, status, key=INTERNAL, **kw):
    return client.post("/internal/usage", headers=H(key), json={"external_id": ext, "category": "utility", "country": "NG", "status": status, **kw})


def balance(tid):
    with database.SessionLocal() as s:
        return s.get(Wallet, tid).balance_micro


def events(ext):
    with database.SessionLocal() as s:
        return sorted(e.status for e in s.scalars(select(MessageEvent).where(MessageEvent.external_id == ext)))


def test_four_events_for_one_message_are_charged_once(client, cw, tenant):
    credit(client, tenant, "10000", "lc-1")
    for st in ("sent", "delivered", "read", "delivered"):
        assert ev(client, "wamid.A", st).status_code == 200
    assert balance(tenant) == BAL - CHARGE
    assert events("wamid.A") == ["delivered", "read", "sent"]                         # lifecycle kept, duplicate ignored
    with database.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(UsageRecord)) == 1


def test_only_the_billable_status_creates_a_charge(client, cw, tenant):
    credit(client, tenant, "10000", "lc-2")
    r = ev(client, "wamid.B", "sent").json()
    assert r["recorded"] is True and r["billed"] is False and balance(tenant) == BAL
    assert ev(client, "wamid.B", "read").json()["billed"] is False
    assert balance(tenant) == BAL                                                      # read before delivered: still free
    r = ev(client, "wamid.B", "delivered").json()
    assert r["recorded"] is True and r["charge_micro"] == CHARGE and balance(tenant) == BAL - CHARGE


def test_a_failed_message_is_not_charged(client, cw, tenant):
    credit(client, tenant, "10000", "lc-3")
    ev(client, "wamid.C", "sent"); ev(client, "wamid.C", "failed")
    assert balance(tenant) == BAL and events("wamid.C") == ["failed", "sent"]


def test_repeating_the_billable_event_changes_nothing(client, cw, tenant):
    credit(client, tenant, "10000", "lc-4")
    first = ev(client, "wamid.D", "delivered").json()
    again = ev(client, "wamid.D", "delivered").json()
    assert first["recorded"] is True and again["recorded"] is False and again["charge_micro"] == CHARGE
    assert balance(tenant) == BAL - CHARGE


def test_the_billable_status_is_a_setting_not_a_hardcoded_assumption(client, cw, tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "billable_status", "sent")
    credit(client, tenant, "10000", "lc-5")
    assert ev(client, "wamid.E", "delivered").json()["billed"] is False               # delivered no longer bills
    assert balance(tenant) == BAL
    assert ev(client, "wamid.E", "sent").json()["charge_micro"] == CHARGE


def test_a_message_id_belongs_to_one_tenant(client, cw, tenant):
    other = mk_tenant(client, cw, service="svc-2", name="Other")
    bind_key("chd_other", [other])
    credit(client, tenant, "10000", "lc-6"); credit(client, other, "10000", "lc-7")
    assert ev(client, "wamid.F", "delivered").status_code == 200
    for st in ("delivered", "sent", "read"):
        r = ev(client, "wamid.F", st, key="chd_other")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "event_conflict"
    assert balance(other) == BAL                                                       # the second tenant was never charged
    assert events("wamid.F") == ["delivered"]


def test_unknown_status_or_provider_is_refused(client, cw, tenant):
    assert ev(client, "x", "teleported").status_code == 422
    assert client.post("/internal/usage", headers=H(INTERNAL), json={"external_id": "x", "category": "utility", "provider": "other"}).status_code == 422


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs PostgreSQL for real concurrency")
def test_concurrent_duplicate_billable_events_charge_exactly_once(client, cw, tenant):
    credit(client, tenant, "10000", "lc-8")
    out = []

    def worker():
        out.append(ev(TestClient(app, base_url="https://hub.test"), "wamid.RACE", "delivered").status_code)

    ts = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert set(out) == {200}
    assert balance(tenant) == BAL - CHARGE
    with database.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(UsageRecord)) == 1
