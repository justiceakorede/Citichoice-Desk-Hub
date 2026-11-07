"""receivable, credit limit, reservations; the wallet can no longer go negative

Revision ID: 0007
Revises: 0006

Debt the provider already made us pay is now an explicit receivable, not a negative balance. Any legacy wallet that is
negative is reclassified with visible, append-only entries (a ledger 'reclassification' and a receivable 'accrual'), so
the ledger still sums to the balance and nothing is silently rewritten. Both ledgers become append-only by trigger.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bad = bind.execute(sa.text("select distinct kind from ledger_entries where kind not in "
                               "('credit','debit','adjustment','receivable_settlement','reclassification')")).fetchall()
    if bad:
        raise RuntimeError(f"ABORTED: unknown ledger entry kinds {[r[0] for r in bad]}")
    op.alter_column("ledger_entries", "kind", type_=sa.String(32), existing_type=sa.String(20), existing_nullable=False)
    for col in ("receivable_micro", "reserved_micro", "credit_limit_micro"):
        op.add_column("wallets", sa.Column(col, sa.BigInteger(), nullable=False, server_default="0"))

    op.create_table(
        "receivable_entries",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False), sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False), sa.Column("balance_after_micro", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True), sa.Column("reference", sa.String(200)),
        sa.Column("meta", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "currency"], ["wallets.tenant_id", "wallets.currency"], name="fk_receivable_wallet_currency"),
        sa.CheckConstraint("kind in ('accrual', 'settlement', 'writeoff', 'adjustment')", name="ck_receivable_kind"))
    op.create_index("ix_receivable_entries_tenant_id", "receivable_entries", ["tenant_id"])
    op.create_index("ix_receivable_entries_created_at", "receivable_entries", ["created_at"])

    op.create_table(
        "reservations",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False), sa.Column("reference", sa.String(160), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False), sa.Column("state", sa.String(10), nullable=False),
        sa.Column("pricing_snapshot_id", sa.String(32), sa.ForeignKey("pricing_snapshots.id")),
        sa.Column("settled_micro", sa.BigInteger()), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_reservation_reference"),
        sa.CheckConstraint("amount_micro >= 0", name="ck_reservation_amount"),
        sa.CheckConstraint("state in ('held', 'settled', 'released', 'expired')", name="ck_reservation_state"))
    op.create_index("ix_reservations_tenant_id", "reservations", ["tenant_id"])
    op.create_index("ix_reservation_due", "reservations", ["state", "expires_at"])

    # Legacy negative wallets: move the debt out of the balance, visibly.
    op.execute("""insert into ledger_entries (id, tenant_id, kind, currency, amount_micro, balance_after_micro, idempotency_key, reference, created_at)
                  select replace(gen_random_uuid()::text, '-', ''), tenant_id, 'reclassification', currency, -balance_micro, 0,
                         'migration-0007:' || tenant_id, 'negative balance reclassified as receivable', now()
                  from wallets where balance_micro < 0""")
    op.execute("""insert into receivable_entries (id, tenant_id, currency, kind, amount_micro, balance_after_micro, idempotency_key, reference, created_at)
                  select replace(gen_random_uuid()::text, '-', ''), tenant_id, currency, 'accrual', -balance_micro, -balance_micro,
                         'migration-0007:' || tenant_id, 'negative balance reclassified as receivable', now()
                  from wallets where balance_micro < 0""")
    op.execute("update wallets set receivable_micro = -balance_micro, balance_micro = 0 where balance_micro < 0")

    for name, cond in (("ck_wallet_balance_nonneg", "balance_micro >= 0"), ("ck_wallet_receivable_nonneg", "receivable_micro >= 0"),
                       ("ck_wallet_reserved_nonneg", "reserved_micro >= 0"), ("ck_wallet_credit_limit_nonneg", "credit_limit_micro >= 0"),
                       ("ck_wallet_no_funds_and_debt", "balance_micro = 0 or receivable_micro = 0")):
        op.create_check_constraint(name, "wallets", cond)
    op.create_check_constraint("ck_ledger_kind", "ledger_entries",
                               "kind in ('credit', 'debit', 'adjustment', 'receivable_settlement', 'reclassification')")
    op.execute("CREATE OR REPLACE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION desk_immutable()")
    op.execute("CREATE OR REPLACE TRIGGER receivable_entries_immutable BEFORE UPDATE OR DELETE ON receivable_entries FOR EACH ROW EXECUTE FUNCTION desk_immutable()")


def downgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(sa.text("""select (select count(*) from wallets where receivable_micro <> 0 or reserved_micro <> 0 or credit_limit_micro <> 0)
                                + (select count(*) from receivable_entries) + (select count(*) from reservations)""")).scalar()
    if n:
        raise RuntimeError(f"cannot downgrade: {n} receivable/reservation/credit-limit record(s) cannot be represented in the older shape")
    op.execute("DROP TRIGGER IF EXISTS receivable_entries_immutable ON receivable_entries")
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries")
    op.drop_constraint("ck_ledger_kind", "ledger_entries", type_="check")
    for name in ("ck_wallet_no_funds_and_debt", "ck_wallet_credit_limit_nonneg", "ck_wallet_reserved_nonneg",
                 "ck_wallet_receivable_nonneg", "ck_wallet_balance_nonneg"):
        op.drop_constraint(name, "wallets", type_="check")
    op.drop_table("reservations")
    op.drop_table("receivable_entries")
    bad = bind.execute(sa.text("select count(*) from ledger_entries where length(kind) > 20")).scalar()
    if bad:
        raise RuntimeError(f"cannot downgrade: {bad} ledger entries have kinds longer than the old column allows")
    op.alter_column("ledger_entries", "kind", type_=sa.String(20), existing_type=sa.String(32), existing_nullable=False)
    for col in ("credit_limit_micro", "reserved_micro", "receivable_micro"):
        op.drop_column("wallets", col)
