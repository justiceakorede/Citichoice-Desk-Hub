"""Billing periods are calendar months in Africa/Lagos. Timestamps are stored in UTC."""
import datetime as dt
from zoneinfo import ZoneInfo

LAGOS = ZoneInfo("Africa/Lagos")


def billing_period(at: dt.datetime | None = None) -> str:
    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    return at.astimezone(LAGOS).strftime("%Y-%m")


def period_start_utc(at: dt.datetime | None = None) -> dt.datetime:
    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    local = at.astimezone(LAGOS).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(dt.timezone.utc)
