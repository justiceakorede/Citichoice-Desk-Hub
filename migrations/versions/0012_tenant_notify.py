"""tenants.notify: weekly report and low-balance alert settings

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("notify", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "notify")
