"""CLI entry point: `python -m pairadar [--offline] [--limit N]`."""
from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import charts
from .config import (
    ASSETS_DIR,
    ROOT,
    Config,
    dump_json,
    load_config,
    load_json,
    now_iso,
)
from .distill import deduplicate, enrich, evidence_mix, lane_distribution, prefilter, select, url_key
from .fetch import baseline_items, fetch_all
from .render import inject_readme, update_index, write_daily, write_latest

HISTORY_PATH = ROOT / "radar" / "history.json"


def load_history() -> list[dict[str, Any]]:
    if HISTORY_PATH.exists():
        payload = load_json(HISTORY_PATH)
        return list(payload.get("runs", []))
    return []


def save_history(runs: list[dict[str, Any]], keep_urls_days: int = 7) -> None:
    """Persist the run log, keeping published URLs only while they still matter."""
    ordered = sorted(runs, key=lambda entry: entry["date"])[-400:]
    if ordered:
        newest = date.fromisoformat(ordered[-1]["date"])
        for entry in ordered:
            if (newest - date.fromisoformat(entry["date"])).days > keep_urls_days:
                entry.pop("urls", None)
    dump_json(HISTORY_PATH, {"runs": ordered})


def recent_urls(runs: list[dict[str, Any]], reference: date, days: int) -> set[str]:
    """URLs published within the repeat window."""
    urls: set[str] = set()
    for entry in runs:
        try:
            age = (reference - date.fromisoformat(entry["date"])).days
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= age <= days:
            urls.update(entry.get("urls", []))
    return urls


def rotate_baseline(items: list[Any], day: str, take: int = 6) -> list[Any]:
    """Deterministically rotate the curated baseline so the front page stays fresh."""
    if not items:
        return []
    ordered = sorted(items, key=lambda item: item.id)
    seed = int(day.replace("-", ""))
    rng = random.Random(seed)
    rng.shuffle(ordered)
    return ordered[:take]


def build_context(config: Config, offline: bool, limit: int, day: str) -> dict[str, Any]:
    reference = datetime.fromisoformat(day).date()
    lookback = int(config.sources.get("arxiv", {}).get("lookback_days", 3))
    repeat_days = int(config.sources.get("filters", {}).get("repeat_days", 7))
    history = [entry for entry in load_history() if entry["date"] != day]
    live = fetch_all(config, offline=offline)
    live = deduplicate(enrich(prefilter(live, config, reference), config, reference))
    picked = select(live, limit=limit, seen=recent_urls(history, reference, repeat_days))

    baseline_all = enrich(baseline_items(config), config, reference)
    curated = {
        f"baseline:{raw['id']}": raw.get("why", {}) for raw in config.baseline.get("items", [])
    }

    tracked = picked + baseline_all
    lane_rows = lane_distribution(tracked, config)
    mix = evidence_mix(tracked)

    history.append({
        "date": day,
        "count": len(picked),
        "lanes": len({item.lane for item in picked}),
        "urls": [url_key(item.url) for item in picked],
    })
    cadence = [(entry["date"], entry["count"]) for entry in sorted(history, key=lambda e: e["date"])]

    window_start = (reference - timedelta(days=lookback)).isoformat()
    return {
        "date": day,
        "generated": now_iso(),
        "window": f"{window_start} → {day} (UTC)",
        "picked": picked,
        "baseline": rotate_baseline(baseline_all, day),
        "baseline_all": baseline_all,
        "curated": curated,
        "lane_rows": lane_rows,
        "mix": mix,
        "cadence": cadence,
        "history": history,
        "live_count": len(live),
        "repeat_days": repeat_days,
    }


def run(offline: bool = False, limit: int = 8, day: str | None = None, write_readme: bool = True) -> dict[str, Any]:
    config = load_config()
    day = day or datetime.now(timezone.utc).date().isoformat()
    ctx = build_context(config, offline=offline, limit=limit, day=day)

    charts.write_all(
        ASSETS_DIR,
        [(config.lane_name(lane_id, "en"), count) for lane_id, count in ctx["lane_rows"]],
        ctx["cadence"],
        ctx["mix"],
        ctx["generated"],
    )
    written: list[Path] = write_daily(config, ctx)
    if write_readme:
        for lang in ("zh", "en", "ja"):
            written.append(inject_readme(config, lang, ctx))
    written.append(write_latest(ctx))
    written.append(update_index(config, ctx, ctx["history"]))
    save_history(ctx["history"], keep_urls_days=ctx["repeat_days"])

    print(
        f"[radar] {day}: {len(ctx['picked'])} picked from {ctx['live_count']} live items, "
        f"{len(ctx['baseline_all'])} baseline entries, {len(written)} files written"
    )
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Physical AI Radar daily update.")
    parser.add_argument("--offline", action="store_true", help="skip all network sources")
    parser.add_argument("--limit", type=int, default=8, help="max items on the daily shortlist")
    parser.add_argument("--date", dest="day", default=None, help="override the run date (YYYY-MM-DD)")
    parser.add_argument("--no-readme", action="store_true", help="do not touch the README files")
    args = parser.parse_args(argv)
    run(offline=args.offline, limit=args.limit, day=args.day, write_readme=not args.no_readme)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
