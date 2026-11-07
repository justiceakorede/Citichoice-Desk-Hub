"""AI allowance: idempotent consumptions

Revision ID: 0008
Revises: 0007

Each logical AI reply is now a keyed record, so a retried workflow counts once and a failed generation can be released.
Counts that already exist have no individual records, so each month's total is preserved as ONE staff-style
adjustment row (flagged by its key), keeping `ai_usage.replies` equal to the sum of the records.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_consumptions",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("period", sa.String(7), nullable=False), sa.Column("consumption_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False), sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(10), nullable=False), sa.Column("reason", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "consumption_key", name="uq_ai_consumption_key"),
        sa.CheckConstraint("kind in ('reply', 'adjustment')", name="ck_ai_kind"),
        sa.CheckConstraint("state in ('claimed', 'released')", name="ck_ai_state"),
        sa.CheckConstraint("units <> 0", name="ck_ai_units"))
    op.create_index("ix_ai_consumptions_tenant_id", "ai_consumptions", ["tenant_id"])
    op.execute("""insert into ai_consumptions (id, tenant_id, period, consumption_key, kind, units, state, reason, created_at)
                  select replace(gen_random_uuid()::text, '-', ''), tenant_id, period, 'migration-0008:' || tenant_id || ':' || period,
                         'adjustment', replies, 'claimed', 'count carried over from before keyed consumptions', now()
                  from ai_usage where replies > 0""")


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from ai_consumptions where consumption_key not like 'migration-0008:%'")).scalar()
    if n:
        raise RuntimeError(f"cannot downgrade: {n} keyed AI consumption record(s) would be lost")
    op.drop_table("ai_consumptions")
