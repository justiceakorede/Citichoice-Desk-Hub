from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Tenant, TenantChannel
from .entitlements import err


def messaging_count(db: Session, tenant_id: str) -> int:
    return db.scalar(select(func.count()).select_from(TenantChannel).where(
        TenantChannel.tenant_id == tenant_id, TenantChannel.status != "removed",
        TenantChannel.channel_type != "web_chat")) or 0


def create_channel(db: Session, tenant_id: str, data: dict) -> TenantChannel:
    # Lock the bare tenant row (no joins: PostgreSQL refuses FOR UPDATE on an outer join) so concurrent
    # creates for one tenant are serialised and cannot both pass the limit check.
    db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    t = db.get(Tenant, tenant_id)
    if t.status in ("cancelled", "terminated"):
        raise err(409, "tenant_closed", "tenant is closed")
    if data["channel_type"] != "web_chat" and messaging_count(db, t.id) >= t.channels_limit:
        raise err(409, "entitlement_exceeded", f"plan allows {t.channels_limit} messaging channel(s)")
    ch = TenantChannel(tenant_id=t.id, **data)
    try:
        with db.begin_nested():
            db.add(ch)
            db.flush()
    except IntegrityError:
        raise err(409, "channel_conflict", "that external identifier or inbox is already registered")
    return ch
