"""Pure next-run computation for BackupSchedule, shared logic duplicated in
worker/app/schedule_utils.py (see web/app/models.py's dual-model convention -
these two files must be kept in sync by hand)."""
from datetime import datetime, time, timedelta

VALID_FREQUENCIES = ("daily", "weekly")


def parse_days_of_week(days_of_week: str | None) -> list[int]:
    if not days_of_week:
        return []
    return sorted({int(d) for d in days_of_week.split(",") if d.strip() != ""})


def compute_next_run(
    frequency: str,
    time_of_day: time,
    days_of_week: str | None,
    after: datetime,
) -> datetime:
    """The next datetime (same tzinfo as `after`) this schedule should fire,
    strictly after `after`. "daily" fires every day at time_of_day; "weekly"
    fires at time_of_day on any of the given weekdays (Monday=0..Sunday=6,
    matching datetime.weekday())."""
    candidate_days = range(7) if frequency == "daily" else parse_days_of_week(days_of_week)
    if not candidate_days:
        candidate_days = [after.weekday()]

    for offset in range(8):  # today plus a full week guarantees a hit
        day = after + timedelta(days=offset)
        if day.weekday() not in candidate_days:
            continue
        candidate = day.replace(hour=time_of_day.hour, minute=time_of_day.minute, second=0, microsecond=0)
        if candidate > after:
            return candidate
    raise ValueError("could not compute next run - no matching day found")  # pragma: no cover - unreachable
