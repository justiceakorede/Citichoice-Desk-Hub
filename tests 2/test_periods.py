import datetime as dt

from app.services.periods import billing_period, period_start_utc

UTC = dt.timezone.utc


def test_month_rolls_over_at_lagos_midnight_not_utc():
    assert billing_period(dt.datetime(2026, 10, 31, 22, 59, tzinfo=UTC)) == "2026-10"
    assert billing_period(dt.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)) == "2026-11"   # 00:00 in Lagos (UTC+1)
    assert period_start_utc(dt.datetime(2026, 11, 15, 12, 0, tzinfo=UTC)) == dt.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)


def test_naive_datetimes_are_treated_as_utc():
    assert billing_period(dt.datetime(2026, 10, 31, 23, 30)) == "2026-11"
