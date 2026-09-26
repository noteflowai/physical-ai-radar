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


def night_day(value: str | None = None, *, previous_day: bool = False) -> str:
    day = date.fromisoformat(run_day(value))
    return (day - timedelta(days=int(previous_day))).isoformat()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None)
    parser.add_argument("--previous-day", action="store_true")
    args = parser.parse_args()
    print(night_day(args.date, previous_day=args.previous_day))
