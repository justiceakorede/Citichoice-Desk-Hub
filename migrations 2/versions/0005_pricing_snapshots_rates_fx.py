"""versioned rate cards, append-only FX, immutable pricing snapshots

Revision ID: 0005
Revises: 0004

Rate cards gain a currency, a window [from, to), versions and a database-enforced no-overlap rule. FX becomes
append-only observations with a source and a quarantine status. Every message is priced from an immutable snapshot;
existing usage rows get a RECONSTRUCTED snapshot built from the inputs Phase 2 stored (flagged as such, never presented
as having come from a rate card). Triggers make snapshots and FX observations impossible to change or delete.
"""
import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RC_FROZEN = ("rate_micro", "currency", "market", "category", "provider", "effective_from")


def _drop_unique_on(table, cols):
    for uc in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if sorted(uc["column_names"]) == sorted(cols):
            op.drop_constraint(uc["name"], table, type_="unique")


def _triggers_up():
    op.execute("""CREATE OR REPLACE FUNCTION desk_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only (%)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation'; END;
$$ LANGUAGE plpgsql""")
    op.execute("CREATE OR REPLACE TRIGGER pricing_snapshots_immutable BEFORE UPDATE OR DELETE ON pricing_snapshots FOR EACH ROW EXECUTE FUNCTION desk_immutable()")
    op.execute("CREATE OR REPLACE TRIGGER fx_rates_immutable BEFORE UPDATE OR DELETE ON fx_rates FOR EACH ROW EXECUTE FUNCTION desk_immutable()")
    op.execute("CREATE OR REPLACE TRIGGER rate_cards_frozen BEFORE UPDATE ON rate_cards FOR EACH ROW WHEN ("
               + " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in RC_FROZEN) + ") EXECUTE FUNCTION desk_immutable()")
    op.execute("CREATE OR REPLACE TRIGGER rate_cards_no_delete BEFORE DELETE ON rate_cards FOR EACH ROW EXECUTE FUNCTION desk_immutable()")


def _triggers_down():
    for t, tr in (("pricing_snapshots", "pricing_snapshots_immutable"), ("fx_rates", "fx_rates_immutable"),
                  ("rate_cards", "rate_cards_frozen"), ("rate_cards", "rate_cards_no_delete")):
        op.execute(f"DROP TRIGGER IF EXISTS {tr} ON {t}")
    op.execute("DROP FUNCTION IF EXISTS desk_immutable()")


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------ rate cards
    for name, typ, extra in (("provider", sa.String(30), dict(server_default="meta_whatsapp")), ("market", sa.String(2), {}),
                             ("currency", sa.String(3), {}), ("rate_micro", sa.BigInteger(), {}), ("effective_to", sa.Date(), {}),
                             ("source", sa.String(300), {}), ("source_ref", sa.String(64), {}), ("version", sa.Integer(), {}),
                             ("active", sa.Boolean(), {}), ("created_at", sa.DateTime(timezone=True), {}), ("created_by", sa.String(100), {})):
        op.add_column("rate_cards", sa.Column(name, typ, **extra))
    op.execute("""update rate_cards set market = country, currency = 'USD', rate_micro = (usd_per_message * 1000000)::bigint,
                  source = coalesce(note, 'phase2_manual'), version = 1, active = true, created_at = now(), created_by = 'migration:0005'""")
    op.execute("""update rate_cards r set effective_to = n.next_from from (
                    select id, lead(effective_from) over (partition by category, country order by effective_from) next_from from rate_cards) n
                  where n.id = r.id""")
    for col in ("market", "currency", "rate_micro", "source", "version", "active", "created_at", "created_by"):
        op.alter_column("rate_cards", col, nullable=False)
    _drop_unique_on("rate_cards", ["category", "country", "effective_from"])
    for col in ("country", "usd_per_message", "note"):
        op.drop_column("rate_cards", col)
    op.alter_column("rate_cards", "provider", server_default=None, nullable=False)
    op.create_unique_constraint("uq_rate_card_start", "rate_cards", ["provider", "category", "market", "currency", "effective_from"])
    op.create_check_constraint("ck_rate_nonneg", "rate_cards", "rate_micro >= 0")
    op.create_check_constraint("ck_rate_window", "rate_cards", "effective_to is null or effective_to > effective_from")
    op.create_foreign_key("fk_rate_cards_currency", "rate_cards", "currencies", ["currency"], ["code"])
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("""ALTER TABLE rate_cards ADD CONSTRAINT rate_cards_no_overlap EXCLUDE USING gist (
                    provider WITH =, category WITH =, market WITH =, currency WITH =, daterange(effective_from, effective_to, '[)') WITH &&)
                  WHERE (active)""")

    # ------------------------------------------------------------------ fx rates
    for name, typ in (("base_currency", sa.String(3)), ("quote_currency", sa.String(3)), ("source", sa.String(60)),
                      ("source_ref", sa.String(200)), ("fetched_at", sa.DateTime(timezone=True)), ("status", sa.String(12)),
                      ("approves_id", sa.String(32)), ("created_at", sa.DateTime(timezone=True)), ("created_by", sa.String(100))):
        op.add_column("fx_rates", sa.Column(name, typ))
    op.execute("""update fx_rates set base_currency = 'USD', quote_currency = currency, source = 'phase2_manual',
                  fetched_at = (effective_from::timestamp at time zone 'UTC'), status = 'active', created_at = now(), created_by = 'migration:0005'""")
    op.execute("ALTER TABLE fx_rates ALTER COLUMN effective_from TYPE timestamptz USING (effective_from::timestamp AT TIME ZONE 'UTC')")
    for col in ("base_currency", "quote_currency", "source", "fetched_at", "status", "created_at", "created_by"):
        op.alter_column("fx_rates", col, nullable=False)
    _drop_unique_on("fx_rates", ["currency", "effective_from"])
    op.drop_column("fx_rates", "currency")
    op.create_unique_constraint("uq_fx_observation", "fx_rates", ["base_currency", "quote_currency", "source", "effective_from"])
    op.create_check_constraint("ck_fx_positive", "fx_rates", "rate > 0")
    op.create_check_constraint("ck_fx_status", "fx_rates", "status in ('active', 'quarantined')")
    op.alter_column("fx_rates", "rate", type_=sa.Numeric(24, 10), existing_nullable=False)
    for col, ref in (("base_currency", "currencies.code"), ("quote_currency", "currencies.code")):
        op.create_foreign_key(f"fk_fx_rates_{col}", "fx_rates", "currencies", [col], ["code"])
    op.create_foreign_key("fk_fx_rates_approves", "fx_rates", "fx_rates", ["approves_id"], ["id"])
    op.create_index("ix_fx_pair_time", "fx_rates", ["base_currency", "quote_currency", "effective_from"])

    # ------------------------------------------------------------------ pricing snapshots
    op.create_table(
        "pricing_snapshots",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("snapshot_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("provider", sa.String(30), nullable=False), sa.Column("category", sa.String(20), nullable=False),
        sa.Column("market", sa.String(2), nullable=False),
        sa.Column("provider_currency", sa.String(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("customer_currency", sa.String(3), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("rate_source", sa.String(20), nullable=False), sa.Column("rate_card_id", sa.String(32), sa.ForeignKey("rate_cards.id")),
        sa.Column("rate_micro", sa.BigInteger(), nullable=False), sa.Column("fx_rate", sa.Numeric(24, 10), nullable=False),
        sa.Column("fx_source", sa.String(60), nullable=False), sa.Column("fx_rate_ids", sa.JSON(), nullable=False),
        sa.Column("fx_observed_at", sa.DateTime(timezone=True)), sa.Column("fx_buffer_pct", sa.Numeric(8, 6), nullable=False),
        sa.Column("markup", sa.Numeric(8, 4), nullable=False), sa.Column("rounding_rule", sa.String(30), nullable=False),
        sa.Column("unit_cost_micro", sa.BigInteger(), nullable=False), sa.Column("buffer_micro", sa.BigInteger(), nullable=False),
        sa.Column("fee_micro", sa.BigInteger(), nullable=False), sa.Column("unit_charge_micro", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))

    # ------------------------------------------------------------------ usage records: reconstruct, then re-point
    op.add_column("usage_records", sa.Column("pricing_snapshot_id", sa.String(32)))
    op.add_column("usage_records", sa.Column("provider_cost_micro", sa.BigInteger()))
    op.add_column("usage_records", sa.Column("fx_buffer_micro", sa.BigInteger()))
    rows = bind.execute(sa.text("""select id, category, country, billed, customer_currency, provider_currency, meta_cost_usd, fx_rate,
                                   fx_buffer_pct, markup, meta_cost_incl_buffer_micro, service_fee_micro, charge_micro from usage_records""")).fetchall()
    cache = {}
    for (uid, category, country, billed, ccur, pcur, usd, fx, buf_pct, markup, incl, fee, charge) in rows:
        if not billed:
            bind.execute(sa.text("update usage_records set provider_cost_micro = 0, fx_buffer_micro = 0 where id = :i"), {"i": uid})
            continue
        rate_micro = int((Decimal(usd) * 1_000_000).to_integral_value())
        unit = int((Decimal(rate_micro) * Decimal(fx)).to_integral_value(rounding=ROUND_HALF_EVEN))
        f = dict(provider="meta_whatsapp", category=category, market=(country or "*").upper(), provider_currency=pcur, customer_currency=ccur,
                 rate_source="reconstructed", rate_card_id=None, rate_micro=rate_micro, fx_rate=format(Decimal(fx), ".10f"),
                 fx_source="phase2_reconstructed", fx_rate_ids=[], fx_observed_at=None, fx_buffer_pct=format(Decimal(buf_pct), ".6f"),
                 markup=format(Decimal(markup), ".4f"), rounding_rule="phase2_reconstructed", unit_cost_micro=unit,
                 buffer_micro=incl - unit, fee_micro=fee, unit_charge_micro=charge)
        h = hashlib.sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if h not in cache:
            sid = hashlib.md5(h.encode()).hexdigest()
            bind.execute(sa.text("""insert into pricing_snapshots (id, snapshot_hash, provider, category, market, provider_currency, customer_currency,
                rate_source, rate_card_id, rate_micro, fx_rate, fx_source, fx_rate_ids, fx_observed_at, fx_buffer_pct, markup, rounding_rule,
                unit_cost_micro, buffer_micro, fee_micro, unit_charge_micro, created_at) values (:id, :h, :provider, :category, :market,
                :provider_currency, :customer_currency, :rate_source, null, :rate_micro, :fx_rate, :fx_source, '[]', null, :fx_buffer_pct, :markup,
                :rounding_rule, :unit_cost_micro, :buffer_micro, :fee_micro, :unit_charge_micro, now())"""),
                {"id": sid, "h": h, **{k: v for k, v in f.items() if k not in ("rate_card_id", "fx_rate_ids", "fx_observed_at")}})
            cache[h] = (sid, unit, incl - unit)
        sid, unit, buf = cache[h]
        bind.execute(sa.text("update usage_records set pricing_snapshot_id = :s, provider_cost_micro = :u, fx_buffer_micro = :b where id = :i"),
                     {"s": sid, "u": unit, "b": buf, "i": uid})
    for col in ("provider_cost_micro", "fx_buffer_micro"):
        op.alter_column("usage_records", col, nullable=False)
    op.create_foreign_key("fk_usage_pricing_snapshot", "usage_records", "pricing_snapshots", ["pricing_snapshot_id"], ["id"])
    for col in ("meta_cost_usd", "fx_rate", "fx_buffer_pct", "markup", "meta_cost_incl_buffer_micro"):
        op.drop_column("usage_records", col)
    _triggers_up()


def downgrade() -> None:
    """Restores the 0004 shape. Refuses rather than lose information the old shape cannot hold."""
    bind = op.get_bind()
    _triggers_down()
    checks = {
        "rate cards in a currency other than USD or withdrawn": "select count(*) from rate_cards where currency <> 'USD' or not active",
        "FX rows that are not USD-based, quarantined or approvals": "select count(*) from fx_rates where base_currency <> 'USD' or status <> 'active' or approves_id is not null",
        "several FX observations for one pair on the same day": "select count(*) from (select quote_currency, (effective_from at time zone 'UTC')::date d from fx_rates group by 1, 2 having count(*) > 1) x",
        "usage priced in a provider currency other than USD": "select count(*) from pricing_snapshots where provider_currency <> 'USD'",
        "snapshot exchange rates finer than 4 decimals": "select count(*) from pricing_snapshots where fx_rate <> round(fx_rate, 4)",
        "snapshot markups finer than 3 decimals": "select count(*) from pricing_snapshots where markup <> round(markup, 3)",
    }
    for label, sql in checks.items():
        n = bind.execute(sa.text(sql)).scalar()
        if n:
            raise RuntimeError(f"cannot downgrade: {n} row(s): {label}")

    op.add_column("usage_records", sa.Column("meta_cost_usd", sa.Numeric(12, 6)))
    op.add_column("usage_records", sa.Column("fx_rate", sa.Numeric(14, 4)))
    op.add_column("usage_records", sa.Column("fx_buffer_pct", sa.Numeric(6, 4)))
    op.add_column("usage_records", sa.Column("markup", sa.Numeric(6, 3)))
    op.add_column("usage_records", sa.Column("meta_cost_incl_buffer_micro", sa.BigInteger()))
    op.execute("""update usage_records u set meta_cost_usd = s.rate_micro::numeric / 1000000, fx_rate = s.fx_rate, fx_buffer_pct = s.fx_buffer_pct,
                  markup = s.markup, meta_cost_incl_buffer_micro = u.provider_cost_micro + u.fx_buffer_micro
                  from pricing_snapshots s where s.id = u.pricing_snapshot_id""")
    op.execute("""update usage_records set meta_cost_usd = 0, fx_rate = 1, fx_buffer_pct = 0, markup = 1, meta_cost_incl_buffer_micro = 0
                  where pricing_snapshot_id is null""")
    for col in ("meta_cost_usd", "fx_rate", "fx_buffer_pct", "markup", "meta_cost_incl_buffer_micro"):
        op.alter_column("usage_records", col, nullable=False)
    op.drop_constraint("fk_usage_pricing_snapshot", "usage_records", type_="foreignkey")
    for col in ("pricing_snapshot_id", "provider_cost_micro", "fx_buffer_micro"):
        op.drop_column("usage_records", col)
    op.drop_table("pricing_snapshots")

    op.drop_index("ix_fx_pair_time", table_name="fx_rates")
    for c in ("fk_fx_rates_approves", "fk_fx_rates_quote_currency", "fk_fx_rates_base_currency", "uq_fx_observation", "ck_fx_status", "ck_fx_positive"):
        op.drop_constraint(c, "fx_rates", type_=("foreignkey" if c.startswith("fk") else "unique" if c.startswith("uq") else "check"))
    op.add_column("fx_rates", sa.Column("currency", sa.String(3)))
    op.execute("update fx_rates set currency = quote_currency")
    op.alter_column("fx_rates", "currency", nullable=False)
    op.execute("ALTER TABLE fx_rates ALTER COLUMN effective_from TYPE date USING (effective_from AT TIME ZONE 'UTC')::date")
    op.alter_column("fx_rates", "rate", type_=sa.Numeric(14, 4), existing_nullable=False)
    for col in ("base_currency", "quote_currency", "source", "source_ref", "fetched_at", "status", "approves_id", "created_at", "created_by"):
        op.drop_column("fx_rates", col)
    op.create_unique_constraint("fx_rates_currency_effective_from_key", "fx_rates", ["currency", "effective_from"])

    op.execute("ALTER TABLE rate_cards DROP CONSTRAINT rate_cards_no_overlap")
    for c, t in (("fk_rate_cards_currency", "foreignkey"), ("uq_rate_card_start", "unique"), ("ck_rate_window", "check"), ("ck_rate_nonneg", "check")):
        op.drop_constraint(c, "rate_cards", type_=t)
    op.add_column("rate_cards", sa.Column("country", sa.String(2)))
    op.add_column("rate_cards", sa.Column("usd_per_message", sa.Numeric(12, 6)))
    op.add_column("rate_cards", sa.Column("note", sa.String(200)))
    op.execute("update rate_cards set country = market, usd_per_message = rate_micro::numeric / 1000000, note = left(source, 200)")
    op.alter_column("rate_cards", "country", nullable=False)
    op.alter_column("rate_cards", "usd_per_message", nullable=False)
    for col in ("provider", "market", "currency", "rate_micro", "effective_to", "source", "source_ref", "version", "active", "created_at", "created_by"):
        op.drop_column("rate_cards", col)
    op.create_unique_constraint("rate_cards_category_country_effective_from_key", "rate_cards", ["category", "country", "effective_from"])
