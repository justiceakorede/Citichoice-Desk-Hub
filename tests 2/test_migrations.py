"""Upgrade/downgrade against real PostgreSQL, with legacy rows in place."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
ROOT = os.path.dirname(os.path.dirname(__file__))


def alembic(*args, ok=True):
    env = {**os.environ, "DATABASE_URL": URL, "ENV": "test"}
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert (r.returncode == 0) is ok, out
    return out


def fresh():
    eng = create_engine(URL)
    with eng.begin() as c:
        c.execute(text("drop schema public cascade; create schema public"))
    return eng


def seed_legacy(eng):
    """Two tenants as Phase 1 left them: kobo and cents, two decimals assumed."""
    with eng.begin() as c:
        c.execute(text("""
            insert into tenants (id, wisecp_service_id, slug, name, owner_email, status, suspend_mode, plan, kb_level, base_seats,
                base_channels, base_ai_replies, extra_seats, extra_channels, extra_ai_packs, grant_seats, grant_channels, markup,
                seats_used, channels_used, created_at, updated_at) values
            ('l1','s1','l1','Legacy','a@b.co','active','none','growth','custom',5,2,6000,0,0,0,0,0,1.400,0,0,now(),now()),
            ('l2','s2','l2','Custom','c@d.co','active','none','starter','basic',3,1,2000,1,0,2,0,1,1.800,0,0,now(),now())"""))
        c.execute(text("insert into wallets (tenant_id,currency,balance_minor,updated_at) values ('l1','NGN',150050,now()),('l2','USD',12345,now())"))
        c.execute(text("""insert into ledger_entries (id, tenant_id, kind, amount_minor, balance_after, idempotency_key, created_at) values
            ('le1','l1','credit',200000,200000,'k1',now()), ('le2','l1','debit',-49950,150050,'k2',now()),
            ('le3','l2','credit',12345,12345,'k3',now())"""))
        c.execute(text("""insert into usage_records (id, tenant_id, external_id, channel, category, billed, meta_cost_usd, fx_rate,
            fx_buffer_pct, markup, meta_cost_minor, service_fee_minor, charge_minor, created_at) values
            ('u1','l1','wamid.1','whatsapp','utility',true,0.0067,1600,0.03,1.4,1105,442,1547,now())"""))
        c.execute(text("""insert into rate_cards (id, category, country, usd_per_message, effective_from, note) values
            ('r1','utility','*',0.0067,'2025-01-01','meta 2025'), ('r2','utility','*',0.0070,'2026-01-01','meta 2026'),
            ('r3','marketing','NG',0.0516,'2025-06-01',null)"""))
        c.execute(text("insert into fx_rates (id, currency, rate, effective_from) values ('f1','NGN',1600,'2026-10-01')"))


def test_0002_to_0005_roundtrip_preserves_every_amount():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "head")
    with eng.connect() as c:
        q = lambda sql: c.execute(text(sql)).all()
        assert [tuple(r) for r in q("select tenant_id, currency, balance_micro from wallets order by tenant_id")] == \
            [("l1", "NGN", 1_500_500_000), ("l2", "USD", 123_450_000)]
        assert [tuple(r) for r in q("select id, currency, amount_micro, balance_after_micro from ledger_entries order by id")] == \
            [("le1", "NGN", 2_000_000_000, 2_000_000_000), ("le2", "NGN", -499_500_000, 1_500_500_000), ("le3", "USD", 123_450_000, 123_450_000)]
        # usage: the provider's ACTUAL cost is now separate from the buffer; the total is unchanged
        assert [tuple(r) for r in q("select customer_currency, provider_currency, provider_cost_micro, fx_buffer_micro, service_fee_micro, charge_micro from usage_records")] == \
            [("NGN", "USD", 10_720_000, 330_000, 4_420_000, 15_470_000)]
        snap = q("select rate_source, rate_card_id, rate_micro, unit_cost_micro, buffer_micro, fee_micro, unit_charge_micro, fx_source from pricing_snapshots")
        assert [tuple(r) for r in snap] == [("reconstructed", None, 6_700, 10_720_000, 330_000, 4_420_000, 15_470_000, "phase2_reconstructed")]
        assert q("select count(*) from usage_records where pricing_snapshot_id is null")[0][0] == 0
        assert [tuple(r) for r in q("select provider, external_id, status, tenant_id from message_events")] == [("meta_whatsapp", "wamid.1", "delivered", "l1")]
        assert q("select provider from usage_records")[0][0] == "meta_whatsapp"
        assert [tuple(r) for r in q("select id, market, currency, rate_micro, effective_from::text, effective_to::text, version, source from rate_cards order by id")] == \
            [("r1", "*", "USD", 6_700, "2025-01-01", "2026-01-01", 1, "meta 2025"), ("r2", "*", "USD", 7_000, "2026-01-01", None, 1, "meta 2026"),
             ("r3", "NG", "USD", 51_600, "2025-06-01", None, 1, "phase2_manual")]
        assert [tuple(r) for r in q("select base_currency, quote_currency, rate, source, status, (effective_from at time zone 'UTC')::text from fx_rates")] == \
            [("USD", "NGN", __import__("decimal").Decimal("1600.0000000000"), "phase2_manual", "active", "2026-10-01 00:00:00")]
        for sql in ("update pricing_snapshots set unit_charge_micro = 1", "delete from fx_rates", "update rate_cards set rate_micro = 1"):
            with pytest.raises(Exception):
                c.execute(text(sql))
            c.rollback()
        with pytest.raises(Exception):                                                                       # overlapping active window
            c.execute(text("insert into rate_cards (id, provider, category, market, currency, rate_micro, effective_from, effective_to, source, version, active, created_at, created_by) "
                           "values ('x','meta_whatsapp','utility','*','USD',1,'2025-06-01',null,'t',9,true,now(),'t')"))
        c.rollback()
        assert [tuple(r) for r in q("select id, billing_currency from tenants order by id")] == [("l1", "NGN"), ("l2", "USD")]
        assert q("select price_micro from plans where code='growth'")[0][0] == 50_000_000_000              # N50,000 as seeded in 0002
        assert {r[0] for r in q("select code from currencies where billing_enabled")} == {"NGN", "USD"}     # only what was in use
        assert q("select count(*) from currencies")[0][0] > 150
        assert q("select exponent from currencies where code='JPY'")[0][0] == 0 and q("select exponent from currencies where code='IQD'")[0][0] == 3
        with pytest.raises(Exception):
            c.execute(text("insert into ledger_entries (id, tenant_id, kind, currency, amount_micro, balance_after_micro, idempotency_key, created_at) "
                           "values ('bad','l1','credit','USD',1,1,'bad',now())"))                          # wrong-currency entry refused by the DB
    assert "No new upgrade operations" in alembic("check")

    alembic("downgrade", "0004")
    with eng.connect() as c:
        assert [tuple(r) for r in c.execute(text("select meta_cost_usd, fx_rate, fx_buffer_pct, markup, meta_cost_incl_buffer_micro, charge_micro from usage_records"))] == \
            [(__import__("decimal").Decimal("0.006700"), __import__("decimal").Decimal("1600.0000"), __import__("decimal").Decimal("0.0300"), __import__("decimal").Decimal("1.400"), 11_050_000, 15_470_000)]
        assert [tuple(r) for r in c.execute(text("select id, country, usd_per_message from rate_cards order by id"))][0][1] == "*"
        assert [tuple(r) for r in c.execute(text("select currency, rate, effective_from::text from fx_rates"))] == [("NGN", __import__("decimal").Decimal("1600.0000"), "2026-10-01")]
    alembic("downgrade", "0003")
    with eng.connect() as c:
        assert [tuple(r) for r in c.execute(text("select tenant_id, currency, balance_minor from wallets order by tenant_id"))] == [("l1", "NGN", 150050), ("l2", "USD", 12345)]
        assert [tuple(r) for r in c.execute(text("select id, amount_minor, balance_after from ledger_entries order by id"))] == \
            [("le1", 200000, 200000), ("le2", -49950, 150050), ("le3", 12345, 12345)]
        assert [tuple(r) for r in c.execute(text("select meta_cost_minor, service_fee_minor, charge_minor from usage_records"))] == [(1105, 442, 1547)]
    alembic("upgrade", "head")
    assert "No new upgrade operations" in alembic("check")
    alembic("downgrade", "0001")                                       # the whole chain unwinds cleanly
    alembic("upgrade", "head")


def test_upgrade_aborts_and_changes_nothing_if_a_balance_disagrees_with_its_ledger():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "0003")
    with eng.begin() as c:
        c.execute(text("update wallets set balance_minor = balance_minor + 1 where tenant_id = 'l1'"))      # silent drift
    out = alembic("upgrade", "head", ok=False)
    assert "ABORTED" in out and "l1" in out
    with eng.connect() as c:
        assert c.execute(text("select balance_minor from wallets where tenant_id='l1'")).scalar() == 150051   # untouched
        assert c.execute(text("select to_regclass('currencies')")).scalar() is None                           # nothing half-applied
    assert "0003" in alembic("current")


def test_downgrade_refuses_when_it_would_lose_precision():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "head")
    with eng.begin() as c:
        c.execute(text("ALTER TABLE ledger_entries DISABLE TRIGGER ledger_entries_immutable"))              # the ledger is append-only; this test must tamper on purpose
        c.execute(text("update ledger_entries set amount_micro = amount_micro + 1 where id = 'le2'"))        # a sub-kobo amount
        c.execute(text("ALTER TABLE ledger_entries ENABLE TRIGGER ledger_entries_immutable"))
    out = alembic("downgrade", "0003", ok=False)
    assert "finer than the currency's minor unit" in out
    assert "0009" in alembic("current")                               # the whole downgrade rolled back, including 0005 and 0006


def test_0005_downgrade_refuses_rather_than_lose_information():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "head")
    with eng.begin() as c:
        c.execute(text("update currencies set billing_enabled = true where code = 'EUR'"))
        c.execute(text("insert into rate_cards (id, provider, category, market, currency, rate_micro, effective_from, source, version, active, created_at, created_by) "
                       "values ('eur','meta_whatsapp','utility','DE','EUR',5000,'2026-01-01','t',1,true,now(),'t')"))
    out = alembic("downgrade", "0004", ok=False)
    assert "other than USD" in out
    assert "0009" in alembic("current")


def test_0006_downgrade_refuses_to_drop_lifecycle_history():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "head")
    with eng.begin() as c:
        c.execute(text("insert into message_events (id, tenant_id, provider, external_id, status, occurred_at, received_at) "
                       "values ('m9','l1','meta_whatsapp','wamid.1','read',now(),now())"))
    out = alembic("downgrade", "0005", ok=False)
    assert "lifecycle event" in out
    assert "0009" in alembic("current")


def test_0007_moves_a_negative_balance_out_of_the_wallet_visibly():
    eng = fresh()
    alembic("upgrade", "0001")
    with eng.begin() as c:
        c.execute(text("""insert into tenants (id, wisecp_service_id, slug, name, owner_email, status, suspend_mode, plan, kb_level, base_seats,
            base_channels, base_ai_replies, extra_seats, extra_channels, extra_ai_packs, grant_seats, grant_channels, markup,
            seats_used, channels_used, created_at, updated_at) values
            ('n1','s9','n1','Overdrawn','o@d.co','active','none','growth','custom',5,2,6000,0,0,0,0,0,1.400,0,0,now(),now())"""))
        c.execute(text("insert into wallets (tenant_id,currency,balance_minor,updated_at) values ('n1','NGN',-2500,now())"))     # owes N25.00
        c.execute(text("""insert into ledger_entries (id, tenant_id, kind, amount_minor, balance_after, idempotency_key, created_at) values
            ('a','n1','credit',10000,10000,'ka',now()), ('b','n1','debit',-12500,-2500,'kb',now())"""))
    alembic("upgrade", "head")
    with eng.connect() as c:
        assert [tuple(r) for r in c.execute(text("select balance_micro, receivable_micro from wallets"))] == [(0, 25_000_000)]
        assert [tuple(r) for r in c.execute(text("select kind, amount_micro, balance_after_micro from ledger_entries order by created_at, id"))][-1] == ("reclassification", 25_000_000, 0)
        assert [tuple(r) for r in c.execute(text("select kind, amount_micro, balance_after_micro from receivable_entries"))] == [("accrual", 25_000_000, 25_000_000)]
        assert c.execute(text("select sum(amount_micro) from ledger_entries")).scalar() == 0                  # the ledger still sums to the balance
    assert "No new upgrade operations" in alembic("check")
    out = alembic("downgrade", "0006", ok=False)                                                               # debt cannot be represented in the old shape
    assert "cannot downgrade" in out and "0009" in alembic("current")


def test_0008_preserves_existing_ai_counts_as_records():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "0007")
    with eng.begin() as c:
        c.execute(text("insert into ai_usage (tenant_id, period, replies) values ('l1','2026-10',120),('l1','2026-09',0),('l2','2026-10',7)"))
    alembic("upgrade", "head")
    with eng.connect() as c:
        rows = [tuple(r) for r in c.execute(text("select tenant_id, period, kind, units, state from ai_consumptions order by tenant_id, period"))]
        assert rows == [("l1", "2026-10", "adjustment", 120, "claimed"), ("l2", "2026-10", "adjustment", 7, "claimed")]
        assert c.execute(text("select count(*) from ai_usage u where u.replies <> coalesce((select sum(units) from ai_consumptions a where a.tenant_id=u.tenant_id and a.period=u.period), 0)")).scalar() == 0
    assert "No new upgrade operations" in alembic("check")
    alembic("downgrade", "0007")
    alembic("upgrade", "head")
    with eng.begin() as c:
        c.execute(text("insert into ai_consumptions (id, tenant_id, period, consumption_key, kind, units, state, created_at) values ('x','l1','2026-10','real-key','reply',1,'claimed',now())"))
    assert "cannot downgrade" in alembic("downgrade", "0007", ok=False)


def test_0009_downgrade_refuses_to_destroy_the_audit_trail():
    eng = fresh()
    alembic("upgrade", "0001")
    seed_legacy(eng)
    alembic("upgrade", "head")
    with eng.begin() as c:
        c.execute(text("insert into adjustment_requests (id, tenant_id, currency, kind, amount_micro, reason, evidence_ref, status, requested_by, requested_at) "
                       "values ('q1','l1','NGN','wallet_credit',1000000,'goodwill credit','TKT-1','pending','a@x.co',now())"))
    assert "audit trail" in alembic("downgrade", "0008", ok=False)
    assert "0009" in alembic("current")
