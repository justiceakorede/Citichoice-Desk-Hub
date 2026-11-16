"""notification content in the log, editable notification templates

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, typ in (("subject", sa.String(300)), ("content_text", sa.Text()), ("content_html", sa.Text()), ("wa_template", sa.String(120)), ("wa_params", sa.JSON())):
        op.add_column("notification_log", sa.Column(name, typ, nullable=True))
    op.create_table(
        "notify_templates",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("email_subject", sa.String(300), nullable=False),
        sa.Column("email_html", sa.Text(), nullable=False),
        sa.Column("email_text", sa.Text(), nullable=False),
        sa.Column("wa_template", sa.String(120), nullable=False),
        sa.Column("wa_language", sa.String(20), nullable=False, server_default="en"),
        sa.Column("wa_params", sa.JSON(), nullable=False),
        sa.Column("wa_wording", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(200)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notify_templates")
    for name in ("wa_params", "wa_template", "content_html", "content_text", "subject"):
        op.drop_column("notification_log", name)
