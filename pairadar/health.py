"""Which sources are quietly dying, read from the run log rather than a log line.

A single dead source is not allowed to fail the daily run -- that is deliberate --
so a feed that has stopped answering can go unnoticed for weeks. Each run records
the sources that did not answer; this module turns that history into a verdict:
which sources failed often enough, and are still failing, to be worth a look.

A source that has recovered is not struggling, however many days it missed: the
repair job used to wake a model every night for a fortnight after arXiv came back.
A source served by its fallback endpoint (`fallback` in the run log) is reported
separately and does not count as failing -- readers got the day's papers, and the
repair job cannot fix an endpoint that refuses this host. A channel whose local service
did not answer (`unavailable`: page_watch when the loopback Firecrawl is down) is
counted under `unavailable`, never as failing: it is the machine, not a source.
A page_watch page that was not read (`page_failed` in the run log: a non-2xx page, a
site failing inside Firecrawl) is reported under `page_watch_failing`, and one
disallowed by robots.txt (`page_robots_skipped`) under `page_watch_robots_skipped`;
neither is ever struggling. The repair job can only repair a declared feed, so a
page_watch source in `struggling` would wake a model every night for a fix it must
reject; whether to drop such a page from `page_watch` is a person's call.

Each struggling source also carries `streak_days`, the consecutive runs it has
failed counted back from the latest one, and `failing_since`, the date of the oldest
run in that streak. A feed dark since one date has usually moved or retired; a feed
that flaps is usually a flaky host or a rate limit. A run on which the source answered
or was served by its fallback ends the streak.

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

from .config import DATA_DIR, ROOT, load_json

HISTORY_PATH = ROOT / "radar" / "history.json"
WINDOW = 14  # days, today included
THRESHOLD = 3


def page_watch_ids() -> frozenset[str]:
    """The ids declared under `page_watch` in data/sources.json; empty if unreadable."""
    try:
        with (DATA_DIR / "sources.json").open(encoding="utf-8") as handle:
            entries = json.load(handle).get("page_watch", [])
        return frozenset(e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str))
    except (OSError, ValueError, AttributeError, TypeError):
        return frozenset()


def _page_failed(entry: dict[str, Any], watched: frozenset[str]) -> list[str]:
    """page_watch ids not read on this run: `page_failed`, plus any listed under `failed`
    (a run log written before page_failed existed)."""
    return sorted({*entry.get("page_failed", []), *(s for s in entry.get("failed", []) if s in watched)})


def load_runs(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return list(load_json(path).get("runs", []))


def _in_window(runs: list[dict[str, Any]], reference: date, window: int) -> list[dict[str, Any]]:
    """Runs that recorded source health inside the window, oldest first."""
    kept = []
    for entry in runs:
        try:
            age = (reference - date.fromisoformat(entry["date"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age < window and "failed" in entry:
            kept.append(entry)
    return sorted(kept, key=lambda entry: entry["date"])


def _count(runs: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in runs:
        for source in entry.get(key, []):
            counts[source] = counts.get(source, 0) + 1
    return counts


def failures_by_source(runs: list[dict[str, Any]], reference: date,
                       window: int = WINDOW) -> dict[str, int]:
    """Count, per source, the days inside the window on which it did not answer."""
    return _count(_in_window(runs, reference, window), "failed")


def fallbacks_by_source(runs: list[dict[str, Any]], reference: date,
                        window: int = WINDOW) -> dict[str, int]:
    """Count, per source, the days inside the window it was served by its fallback."""
    return _count(_in_window(runs, reference, window), "fallback")


def unavailable_by_channel(runs: list[dict[str, Any]], reference: date,
                           window: int = WINDOW) -> dict[str, int]:
    """Count, per channel, the days inside the window its local service was unavailable."""
    return _count(_in_window(runs, reference, window), "unavailable")


def page_watch_failing(runs: list[dict[str, Any]], reference: date, window: int = WINDOW,
                       threshold: int = THRESHOLD,
                       watched: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """page_watch sources over the threshold that also were not read on the latest run.

    Same rule as `struggling`, read from `page_failed`; reported for a person, never
    to the repair job.
    """
    watched = page_watch_ids() if watched is None else watched
    observed = [{**entry, "page_failed": _page_failed(entry, watched)}
                for entry in _in_window(runs, reference, window)]
    if not observed:
        return []
    latest = set(observed[-1]["page_failed"])
    counts = _count(observed, "page_failed")
    entries = []
    for source, days in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
        if days < threshold or source not in latest:
            continue
        streak_days, since = 0, None
        for entry in reversed(observed):
            if source not in entry.get("page_failed", []):
                break
            streak_days, since = streak_days + 1, entry["date"]
        entries.append({"source": source, "failed_days": days, "streak_days": streak_days,
                        "failing_since": since})
    return entries


def days_observed(runs: list[dict[str, Any]], reference: date, window: int = WINDOW) -> int:
    """Runs inside the window that recorded source health at all."""
    return len(_in_window(runs, reference, window))


def struggling(runs: list[dict[str, Any]], reference: date, window: int = WINDOW,
               threshold: int = THRESHOLD,
               exclude: frozenset[str] | None = None) -> list[tuple[str, int]]:
    """Sources at or over the failure threshold that also failed the latest run, worst first.

    page_watch sources (`exclude`, by default the ids in data/sources.json) are never
    struggling, whatever the run log says: the repair job can only repair a feed.
    """
    exclude = page_watch_ids() if exclude is None else exclude
    observed = _in_window(runs, reference, window)
    if not observed:
        return []
    latest = set(observed[-1].get("failed", []))
    counts = _count(observed, "failed")
    ranked = [(source, days) for source, days in counts.items()
              if days >= threshold and source in latest and source not in exclude]
    return sorted(ranked, key=lambda pair: (-pair[1], pair[0]))


def streak(runs: list[dict[str, Any]], reference: date, window: int,
           source: str) -> tuple[int, str | None]:
    """Consecutive runs, counted back from the latest, on which `source` did not answer.

    Returns the length of that streak and the `date` of its oldest run, or (0, None)
    when the source is not in the latest run's `failed` list. A run that does not
    list the source under `failed` ends the streak, whether the source answered,
    was served by its fallback, or was not attempted at all. Runs outside the window
    are not consulted, so a streak can never reach further back than the window.
    """
    observed = _in_window(runs, reference, window)
    count = 0
    since: str | None = None
    for entry in reversed(observed):
        if source not in entry.get("failed", []):
            break
        count += 1
        since = entry["date"]
    return count, since


def report(runs: list[dict[str, Any]], reference: date, window: int = WINDOW,
           threshold: int = THRESHOLD) -> dict[str, Any]:
    entries = []
    watched = page_watch_ids()
    for source, days in struggling(runs, reference, window, threshold, exclude=watched):
        streak_days, failing_since = streak(runs, reference, window, source)
        entries.append({"source": source, "failed_days": days,
                        "streak_days": streak_days, "failing_since": failing_since})
    return {
        "reference": reference.isoformat(),
        "window_days": window,
        "threshold_days": threshold,
        "runs_with_health": days_observed(runs, reference, window),
        "failures": failures_by_source(runs, reference, window),
        "fallbacks": fallbacks_by_source(runs, reference, window),
        "unavailable": unavailable_by_channel(runs, reference, window),
        "page_watch_failing": page_watch_failing(runs, reference, window, threshold, watched),
        "page_watch_robots_skipped": _count(_in_window(runs, reference, window), "page_robots_skipped"),
        # A feed whose robots.txt disallows it, and an attention API (pairadar.heat) that
        # did not answer: counted here, never struggling -- the repair job fixes feeds.
        "robots_skipped": _count(_in_window(runs, reference, window), "robots_skipped"),
        "signals_unavailable": _count(_in_window(runs, reference, window), "signal_failed"),
        "struggling": entries,
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
