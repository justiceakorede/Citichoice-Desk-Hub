"""maker-checker adjustment requests and one-time usage reversals

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adjustment_requests",
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False), sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False), sa.Column("target_usage_id", sa.String(32), sa.ForeignKey("usage_records.id")),
        sa.Column("reason", sa.String(300), nullable=False), sa.Column("evidence_ref", sa.String(200), nullable=False),
        sa.Column("status", sa.String(10), nullable=False), sa.Column("requested_by", sa.String(200), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False), sa.Column("decided_by", sa.String(200)),
        sa.Column("decided_at", sa.DateTime(timezone=True)), sa.Column("decision_note", sa.String(300)),
        sa.Column("self_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind in ('wallet_credit', 'wallet_debit', 'receivable_writeoff', 'manual_charge', 'usage_reversal')", name="ck_adjustment_kind"),
        sa.CheckConstraint("status in ('pending', 'posted', 'rejected')", name="ck_adjustment_status"),
        sa.CheckConstraint("amount_micro > 0", name="ck_adjustment_amount"),
        sa.CheckConstraint("decided_by is null or decided_by <> requested_by or self_approved", name="ck_adjustment_two_people"))
    op.create_index("ix_adjustment_requests_tenant_id", "adjustment_requests", ["tenant_id"])
    op.create_index("ix_adjustment_status", "adjustment_requests", ["status", "requested_at"])
    op.create_table(
        "usage_reversals",
        sa.Column("usage_id", sa.String(32), sa.ForeignKey("usage_records.id"), primary_key=True),
        sa.Column("request_id", sa.String(32), sa.ForeignKey("adjustment_requests.id"), nullable=False, unique=True),
        sa.Column("amount_micro", sa.BigInteger(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("select count(*) from adjustment_requests")).scalar()
    if n:
        raise RuntimeError(f"cannot downgrade: {n} adjustment request(s) are part of the audit trail and would be lost")
    op.drop_table("usage_reversals")
    op.drop_table("adjustment_requests")
