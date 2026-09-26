"""Calendar dates shared by the local scheduler and its hosted fallback."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Singapore")


def run_day(value: str | None = None, *, now: datetime | None = None) -> str:
    value = value or os.environ.get("RADAR_RUN_DAY")
    if value:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("Run date must be YYYY-MM-DD")
        return value
    return (now or datetime.now(ZONE)).astimezone(ZONE).date().isoformat()


def night_day(value: str | None = None, *, previous_day: bool = False,
              now: datetime | None = None) -> str:
    if value or previous_day:
        day = date.fromisoformat(run_day(value, now=now))
        return (day - timedelta(days=int(previous_day))).isoformat()
    # A new process after midnight resumes the latest evening, not the upcoming
    # morning's issue. Bootstrap retries keep the same start instant across midnight
    # and even across the next evening boundary.
    started = os.environ.get("RADAR_RUN_STARTED_AT")
    moment = datetime.fromisoformat(started.replace("Z", "+00:00")) if started else (
        now or datetime.now(ZONE))
    local = moment.astimezone(ZONE)
    day = local.date() - timedelta(days=int(local.hour * 60 + local.minute < 21 * 60 + 30))
    return day.isoformat()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None)
    parser.add_argument("--previous-day", action="store_true")
    parser.add_argument("--night", action="store_true",
                        help="default to the most recent 21:30 Singapore batch")
    args = parser.parse_args()
    if args.night:
        print(night_day(args.date, previous_day=args.previous_day))
    else:
        day = date.fromisoformat(run_day(args.date))
        print((day - timedelta(days=int(args.previous_day))).isoformat())
