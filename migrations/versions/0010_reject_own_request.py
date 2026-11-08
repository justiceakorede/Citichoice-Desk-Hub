"""a staff member may reject (withdraw) their own adjustment request; only approving needs a second person

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

OLD = "decided_by is null or decided_by <> requested_by or self_approved"
NEW = "status = 'rejected' or " + OLD


def upgrade() -> None:
    with op.batch_alter_table("adjustment_requests") as b:
        b.drop_constraint("ck_adjustment_two_people", type_="check")
        b.create_check_constraint("ck_adjustment_two_people", NEW)


def downgrade() -> None:
    with op.batch_alter_table("adjustment_requests") as b:
        b.drop_constraint("ck_adjustment_two_people", type_="check")
        b.create_check_constraint("ck_adjustment_two_people", OLD)
