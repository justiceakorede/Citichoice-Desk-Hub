"""auto-recharge settings and top-up attempts

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auto_recharge",
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("wisecp_client_id", sa.String(32)),
        sa.Column("threshold_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("monthly_cap_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_reason", sa.String(60)),
        sa.Column("updated_by", sa.String(200)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "recharge_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="creating"),
        sa.Column("invoice_id", sa.String(32)),
        sa.Column("error", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_recharge_attempts_tenant_id", "recharge_attempts", ["tenant_id"])
    op.create_index("ix_recharge_tenant_time", "recharge_attempts", ["tenant_id", "created_at"])
    op.create_index("uq_one_open_auto_recharge", "recharge_attempts", ["tenant_id"], unique=True,
                    postgresql_where=sa.text("kind = 'auto' and status in ('creating', 'awaiting_payment')"))


def downgrade() -> None:
    op.drop_table("recharge_attempts")
    op.drop_table("auto_recharge")
