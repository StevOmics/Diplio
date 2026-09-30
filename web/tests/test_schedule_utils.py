from datetime import datetime, time, timezone

from app.schedule_utils import compute_next_run, parse_days_of_week


def test_parse_days_of_week_sorts_and_dedupes():
    assert parse_days_of_week("2,0,2") == [0, 2]


def test_parse_days_of_week_empty():
    assert parse_days_of_week(None) == []
    assert parse_days_of_week("") == []


def test_daily_later_today():
    after = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # Monday
    result = compute_next_run("daily", time(14, 30), None, after)
    assert result == datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def test_daily_rolls_to_tomorrow_when_time_passed():
    after = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
    result = compute_next_run("daily", time(2, 0), None, after)
    assert result == datetime(2026, 1, 6, 2, 0, tzinfo=timezone.utc)


def test_weekly_picks_next_matching_day():
    after = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # Monday (weekday 0)
    # Next Wednesday (2) or Friday (4) at 03:00
    result = compute_next_run("weekly", time(3, 0), "2,4", after)
    assert result == datetime(2026, 1, 7, 3, 0, tzinfo=timezone.utc)


def test_weekly_wraps_to_next_week():
    after = datetime(2026, 1, 9, 10, 0, tzinfo=timezone.utc)  # Friday
    result = compute_next_run("weekly", time(3, 0), "0", after)  # only Mondays
    assert result == datetime(2026, 1, 12, 3, 0, tzinfo=timezone.utc)
