"""notification_log: what the hub sent to customers (WhatsApp and email)

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(400)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_notification_log_tenant_id", "notification_log", ["tenant_id"])
    op.create_index("ix_notification_log_created_at", "notification_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("notification_log")
