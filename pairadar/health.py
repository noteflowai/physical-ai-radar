"""Which sources are quietly dying, read from the run log rather than a log line.

A single dead source is not allowed to fail the daily run -- that is deliberate --
so a feed that has stopped answering can go unnoticed for weeks. Each run records
the sources that did not answer; this module turns that history into a verdict:
which sources failed often enough, recently enough, to be worth a look.

    python3 -m pairadar.health                        # print the verdict
    python3 -m pairadar.health --fail-on-struggling   # exit 1 when one is failing

The second form is for automation that should only act when there is something to
act on. The daily run never uses it: losing one source must not turn the build red.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT, load_json

HISTORY_PATH = ROOT / "radar" / "history.json"
WINDOW = 14
THRESHOLD = 3


def load_runs(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return list(load_json(path).get("runs", []))


def failures_by_source(runs: list[dict[str, Any]], reference: date,
                       window: int = WINDOW) -> dict[str, int]:
    """Count, per source, the days inside the window on which it did not answer."""
    counts: dict[str, int] = {}
    for entry in runs:
        try:
            age = (reference - date.fromisoformat(entry["date"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age <= window:
            continue
        for source in entry.get("failed", []):
            counts[source] = counts.get(source, 0) + 1
    return counts


def days_observed(runs: list[dict[str, Any]], reference: date, window: int = WINDOW) -> int:
    """Runs inside the window that recorded source health at all."""
    observed = 0
    for entry in runs:
        try:
            age = (reference - date.fromisoformat(entry["date"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age <= window and "failed" in entry:
            observed += 1
    return observed


def struggling(runs: list[dict[str, Any]], reference: date, window: int = WINDOW,
               threshold: int = THRESHOLD) -> list[tuple[str, int]]:
    """Sources at or over the failure threshold, worst first."""
    counts = failures_by_source(runs, reference, window)
    ranked = [(source, days) for source, days in counts.items() if days >= threshold]
    return sorted(ranked, key=lambda pair: (-pair[1], pair[0]))


def report(runs: list[dict[str, Any]], reference: date, window: int = WINDOW,
           threshold: int = THRESHOLD) -> dict[str, Any]:
    return {
        "reference": reference.isoformat(),
        "window_days": window,
        "threshold_days": threshold,
        "runs_with_health": days_observed(runs, reference, window),
        "failures": failures_by_source(runs, reference, window),
        "struggling": [{"source": source, "failed_days": days}
                       for source, days in struggling(runs, reference, window, threshold)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    parser.add_argument("--date", dest="day", default=None, help="reference date (YYYY-MM-DD)")
    parser.add_argument("--window", type=int, default=WINDOW)
    parser.add_argument("--threshold", type=int, default=THRESHOLD)
    parser.add_argument("--fail-on-struggling", action="store_true",
                        help="exit 1 when a source is over the threshold")
    args = parser.parse_args(argv)
    reference = (datetime.fromisoformat(args.day).date() if args.day
                 else datetime.now(timezone.utc).date())
    verdict = report(load_runs(args.history), reference, args.window, args.threshold)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    if verdict["struggling"] and args.fail_on_struggling:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
