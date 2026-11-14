"""Report scheduling maths and on-demand periods. Pure functions, no database. NOT YET RUN in the build sandbox."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services import notify as nf

LAGOS = ZoneInfo("Africa/Lagos")
UTC = dt.timezone.utc


def lagos(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=LAGOS).astimezone(UTC)


def test_daily_covers_yesterday_and_is_due_after_the_hour():
    occ, start, end, key = nf.schedule_point(lagos(2026, 11, 14, 9), "daily", 0, 8)
    assert key == "2026-11-14" and start.astimezone(LAGOS).day == 13 and (end - start) == dt.timedelta(days=1)
    assert 0 <= (lagos(2026, 11, 14, 9) - occ).total_seconds() < 86400


def test_daily_before_the_hour_is_still_yesterdays_report():
    occ, *_rest, key = nf.schedule_point(lagos(2026, 11, 14, 7), "daily", 0, 8)
    assert key == "2026-11-13"


def test_weekly_monday_covers_the_previous_monday_to_sunday():
    occ, start, end, key = nf.schedule_point(lagos(2026, 11, 16, 10), "weekly", 0, 8)
    assert key == "2026-11-16" and start.astimezone(LAGOS).date() == dt.date(2026, 11, 9) and end.astimezone(LAGOS).date() == dt.date(2026, 11, 16)


def test_weekly_mid_week_is_not_due_again_until_next_send_day():
    occ, *_ = nf.schedule_point(lagos(2026, 11, 20, 9), "weekly", 0, 8)       # Friday: last occurrence was Monday
    assert (lagos(2026, 11, 20, 9) - occ).total_seconds() >= 86400            # the 24h window has passed, so nothing is due


def test_monthly_covers_the_previous_calendar_month():
    occ, start, end, key = nf.schedule_point(lagos(2026, 12, 1, 9), "monthly", 0, 8)
    assert key == "2026-12-01" and start.astimezone(LAGOS).date() == dt.date(2026, 11, 1) and end.astimezone(LAGOS).date() == dt.date(2026, 12, 1)


def test_on_demand_periods():
    now = lagos(2026, 11, 14, 15)
    s, e = nf.on_demand_bounds(now, "last7")
    assert (e - s) == dt.timedelta(days=7)
    s, e = nf.on_demand_bounds(now, "mtd")
    assert s.astimezone(LAGOS).day == 1 and e == now
    s, e = nf.on_demand_bounds(now, "custom", "2026-11-01", "2026-11-03")
    assert (e - s) == dt.timedelta(days=3)
    with pytest.raises(ValueError):
        nf.on_demand_bounds(now, "custom", "not-a-date", "")
    with pytest.raises(ValueError):
        nf.on_demand_bounds(now, "fortnight")


def test_set_conf_validates():
    class T:
        notify = None
    t = T()
    nf.set_conf(t, whatsapp_freq="daily", email_freq="weekly", hour=7, weekday="fri")
    assert t.notify["whatsapp_freq"] == "daily" and t.notify["weekday"] == 4 and t.notify["hour"] == 7
    with pytest.raises(ValueError):
        nf.set_conf(t, whatsapp_freq="hourly")
    with pytest.raises(ValueError):
        nf.set_conf(t, hour=24)
