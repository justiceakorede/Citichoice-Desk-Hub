import datetime as dt
import os
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db as database
from app.main import app
from app.models import AiConsumption, AiUsage, AuditLog
from app.services import periods
from .conftest import ADMIN, INTERNAL, H, bind_key, mk_tenant, run_jobs

GROWTH = 6000


def claim(client, key, count=1, auth=INTERNAL, **kw):
    return client.post("/internal/ai-reply", headers=H(auth), json={"key": key, "count": count, **kw}).json()


def fill(client, n, prefix="fill", auth=INTERNAL):
    """Uses n replies of allowance in chunks (one claim is capped at 1,000 units)."""
    i = 0
    while n > 0:
        take = min(1000, n)
        assert claim(client, f"{prefix}-{i}", take, auth=auth)["allowed"]
        n -= take
        i += 1


def release(client, key, auth=INTERNAL):
    return client.post(f"/internal/ai-reply/{key}/release", headers=H(auth))


def used(tid, period=None):
    with database.SessionLocal() as s:
        row = s.get(AiUsage, (tid, period or periods.billing_period()))
        return row.replies if row else 0


def assert_ai_invariant():
    with database.SessionLocal() as s:
        for row in s.scalars(select(AiUsage)):
            total = s.scalar(select(func.coalesce(func.sum(AiConsumption.units), 0)).where(
                AiConsumption.tenant_id == row.tenant_id, AiConsumption.period == row.period, AiConsumption.state == "claimed"))
            assert row.replies == total, "the month's count must equal the sum of claimed consumptions"


def test_a_retried_workflow_counts_once(client, cw, tenant):
    a = claim(client, "exec-123:msg-9")
    b = claim(client, "exec-123:msg-9")                       # n8n retried the same execution
    assert a["allowed"] and a["replayed"] is False and b["allowed"] and b["replayed"] is True
    assert a["used"] == 1 and b["used"] == 1 and used(tenant) == 1
    assert_ai_invariant()


def test_a_key_is_required(client, cw, tenant):
    assert client.post("/internal/ai-reply", headers=H(INTERNAL), json={"count": 1}).status_code == 422
    assert client.post("/internal/ai-reply", headers=H(INTERNAL), json={"key": "", "count": 1}).status_code == 422


def test_distinct_replies_count_separately_and_the_limit_is_exact(client, cw, tenant):
    fill(client, GROWTH - 1)
    assert used(tenant) == GROWTH - 1
    assert claim(client, "last")["allowed"] is True                                  # reaches exactly the limit
    r = claim(client, "one-too-many")
    assert r["allowed"] is False and r["reason"] == "allowance_reached" and r["used"] == GROWTH
    assert used(tenant) == GROWTH
    assert claim(client, "last")["replayed"] is True                                 # an earlier key is still honoured at the limit
    assert_ai_invariant()


def test_a_failed_generation_gives_the_allowance_back(client, cw, tenant):
    claim(client, "g1", 5)
    assert release(client, "g1").json() == {"released": True} and used(tenant) == 0
    assert release(client, "g1").json() == {"released": False} and used(tenant) == 0     # releasing twice does not refund twice
    again = claim(client, "g1", 5)                                                   # the retry of that reply counts, once
    assert again["allowed"] and again["replayed"] is False and used(tenant) == 5
    assert claim(client, "g1", 5)["replayed"] is True and used(tenant) == 5
    assert_ai_invariant()


def test_releasing_an_unknown_key_is_a_404(client, cw, tenant):
    assert release(client, "never-claimed").status_code == 404


def test_one_tenants_key_cannot_release_anothers_reply(client, cw, tenant):
    other = mk_tenant(client, cw, service="svc-2", name="Other")
    bind_key("chd_other", [other])
    claim(client, "shared-key", 3)
    claim(client, "shared-key", 7, auth="chd_other")                                 # same string, different tenant: independent
    assert release(client, "shared-key", auth="chd_other").json() == {"released": True}
    assert used(tenant) == 3 and used(other) == 0
    assert_ai_invariant()


def test_the_allowance_follows_plan_changes_and_packs(client, cw, tenant):
    fill(client, GROWTH, "a")
    assert claim(client, "b")["allowed"] is False
    client.patch(f"/admin/tenants/{tenant}", headers=H(ADMIN), json={"extra_ai_packs": 1})            # +5,000
    assert claim(client, "b")["allowed"] is True
    client.patch(f"/admin/tenants/{tenant}", headers=H(ADMIN), json={"plan": "starter"})              # downgrade: 2,000 + the paid 5,000 pack = 7,000
    r = claim(client, "c", 1000)
    assert r["allowed"] is False and r["limit"] == 2000 + 5000 and r["used"] == GROWTH + 1             # paid packs survive a plan change
    assert claim(client, "c2", 999)["allowed"] is True and claim(client, "c3")["allowed"] is False
    assert claim(client, "a-0", 1000)["replayed"] is True                                            # what was already claimed stands


def test_suspended_tenants_cannot_claim(client, cw, tenant):
    client.post(f"/admin/tenants/{tenant}/suspend", headers=H(ADMIN), json={"mode": "pause_outbound"})
    assert claim(client, "x")["allowed"] is False


def test_the_month_rolls_over_in_lagos_time(client, cw, tenant, monkeypatch):
    from app.services import ai_allowance
    monkeypatch.setattr(ai_allowance, "billing_period", lambda: "2026-10")
    claim(client, "oct-1", 10)
    monkeypatch.setattr(ai_allowance, "billing_period", lambda: "2026-11")
    assert claim(client, "nov-1", 4)["used"] == 4                                                    # a fresh month starts at zero
    assert release(client, "oct-1").json()["released"] is True                                       # the October claim is refunded in October
    assert used(tenant, "2026-10") == 0 and used(tenant, "2026-11") == 4
    assert_ai_invariant()


def test_staff_adjustments_are_idempotent_audited_and_bounded(client, cw, tenant):
    claim(client, "a", 10)
    adj = lambda delta, key: client.post(f"/admin/tenants/{tenant}/ai-adjust", headers=H(ADMIN), json={"delta": delta, "reason": "support goodwill", "idempotency_key": key})
    r = adj(-4, "adj-1").json()
    assert r["applied"] is True and r["used"] == 6
    assert adj(-4, "adj-1").json() == {**r, "applied": False}                                        # a repeat changes nothing
    assert adj(-100, "adj-2").status_code == 409                                                     # cannot go below zero
    assert used(tenant) == 6
    with database.SessionLocal() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "ai.adjust")).one()
    assert row.detail["delta"] == -4 and row.detail["reason"] == "support goodwill"
    listing = client.get(f"/admin/tenants/{tenant}/ai-consumptions", headers=H(ADMIN)).json()
    assert {(c["kind"], c["units"]) for c in listing} == {("reply", 10), ("adjustment", -4)}
    assert_ai_invariant()


def test_an_internal_key_cannot_adjust(client, cw, tenant):
    r = client.post(f"/admin/tenants/{tenant}/ai-adjust", headers=H(INTERNAL), json={"delta": -5, "reason": "attempt", "idempotency_key": "evil-1"})
    assert r.status_code == 403


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs PostgreSQL for real concurrency")
def test_concurrent_duplicates_count_once_and_the_limit_holds(client, cw, tenant):
    out = []

    def go(key):
        out.append(claim(TestClient(app, base_url="https://hub.test"), key, 1))

    threads = [threading.Thread(target=go, args=("same-key",)) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert used(tenant) == 1 and sum(1 for r in out if r["allowed"]) == 12
    out.clear()
    fill(client, GROWTH - 1 - 4)                                                                    # 1 used + 5,995 = 5,996: room for exactly 4 more
    threads = [threading.Thread(target=go, args=(f"k{i}",)) for i in range(30)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(1 for r in out if r["allowed"]) == 4 and used(tenant) == GROWTH
    assert_ai_invariant()
