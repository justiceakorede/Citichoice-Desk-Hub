"""message lifecycle events; usage identity scoped by provider

Revision ID: 0006
Revises: 0005

Every status event for a message is now kept in `message_events` (unique per provider, message and status); only the
configured billable status creates a charge. Usage identity becomes (provider, message id) instead of one global message id.
Existing usage rows imply a 'delivered' event each, which is backfilled.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False), sa.Column("external_id", sa.String(160), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_id", "status", name="uq_message_event"),
        sa.CheckConstraint("status in ('sent', 'delivered', 'read', 'failed')", name="ck_message_event_status"))
    op.create_index("ix_message_events_tenant_id", "message_events", ["tenant_id"])

    op.add_column("usage_records", sa.Column("provider", sa.String(30), nullable=False, server_default="meta_whatsapp"))
    op.alter_column("usage_records", "provider", server_default=None)
    for uc in sa.inspect(op.get_bind()).get_unique_constraints("usage_records"):
        if uc["column_names"] == ["external_id"]:
            op.drop_constraint(uc["name"], "usage_records", type_="unique")
    op.create_unique_constraint("uq_usage_provider_event", "usage_records", ["provider", "external_id"])
    op.execute("""insert into message_events (id, tenant_id, provider, external_id, status, occurred_at, received_at)
                  select replace(gen_random_uuid()::text, '-', ''), tenant_id, provider, external_id, 'delivered', created_at, now() from usage_records""")


def downgrade() -> None:
    bind = op.get_bind()
    extra = bind.execute(sa.text("""select count(*) from message_events m where not exists (
        select 1 from usage_records u where u.provider = m.provider and u.external_id = m.external_id and m.status = 'delivered')""")).scalar()
    if extra:
        raise RuntimeError(f"cannot downgrade: {extra} lifecycle event(s) (sent/read/failed or unbilled) would be lost")
    dup = bind.execute(sa.text("select count(*) from (select external_id from usage_records group by external_id having count(*) > 1) x")).scalar()
    if dup:
        raise RuntimeError(f"cannot downgrade: {dup} message id(s) are used by more than one provider")
    op.drop_table("message_events")
    op.drop_constraint("uq_usage_provider_event", "usage_records", type_="unique")
    op.create_unique_constraint("usage_records_external_id_key", "usage_records", ["external_id"])
    op.drop_column("usage_records", "provider")
