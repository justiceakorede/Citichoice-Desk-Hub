"""usage_records.quantity: one row can stand for many messages (daily reconciliation from Meta)

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_records", sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("usage_records", "quantity")
