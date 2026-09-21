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
    LANGS,
    README_FILES,
    ROOT,
    Config,
    Item,
    dump_json,
    load_config,
    load_json,
    load_notes,
    now_iso,
)
from .distill import deduplicate, enrich, evidence_mix, lane_distribution, prefilter, select, url_key
from .fetch import baseline_items, fetch_all
from .render import inject_readme, update_index, write_daily, write_latest

HISTORY_PATH = ROOT / "radar" / "history.json"


def load_history(path: Path = HISTORY_PATH) -> list[dict[str, Any]]:
    if path.exists():
        payload = load_json(path)
        return list(payload.get("runs", []))
    return []


def save_history(runs: list[dict[str, Any]], keep_urls_days: int = 7,
                 path: Path = HISTORY_PATH) -> None:
    """Persist the run log, keeping published URLs only while they still matter."""
    ordered = sorted(runs, key=lambda entry: entry["date"])[-400:]
    if ordered:
        newest = date.fromisoformat(ordered[-1]["date"])
        for entry in ordered:
            if (newest - date.fromisoformat(entry["date"])).days > keep_urls_days:
                entry.pop("urls", None)
    dump_json(path, {"runs": ordered})


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
    report = fetch_all(config, offline=offline)
    live = deduplicate(enrich(prefilter(report.items, config, reference), config, reference))
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
        # Kept for every run, not pruned with the URLs: pairadar.health reads a
        # longer window to spot a source that has quietly stopped answering.
        "failed": sorted(report.failed),
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
        "fetch": report.to_dict(),
        "notes": load_notes(day),
    }


def rerender(day: str | None = None, out: Path | None = None, write_readme: bool = True,
             source: Path | None = None) -> dict[str, Any]:
    """Re-render a published day from its snapshot, with no network and no new picks.

    A day's pages are written once, at publish time. Anything that arrives afterwards
    -- drafted notes, a template fix -- only reaches readers when that day is rendered
    again, and re-running the pipeline would select a different set of items. This
    rebuilds the day from `radar/latest.json` instead, so the published evidence is
    what gets rendered.
    """
    source = Path(source) if source else ROOT
    root = Path(out) if out else source
    config = load_config()
    snapshot = load_json(source/"radar"/"latest.json")
    day = day or snapshot["date"]
    if snapshot["date"] != day:
        raise SystemExit(f"radar/latest.json holds {snapshot['date']}, not {day}; "
                         "only the most recent published day can be re-rendered")
    picked = [Item.from_dict(entry) for entry in snapshot["picked"]]
    if picked and not any(item.summary for item in picked):
        print("[rerender] this snapshot predates stored excerpts: source quotes will be missing")
    baseline_all = enrich(baseline_items(config), config, datetime.fromisoformat(day).date())
    history = load_history(source/"radar"/"history.json")
    ctx = {
        "date": day,
        "generated": snapshot["generated"],
        "window": snapshot["window"],
        "picked": picked,
        "baseline": rotate_baseline(baseline_all, day),
        "baseline_all": baseline_all,
        "curated": {f"baseline:{raw['id']}": raw.get("why", {})
                    for raw in config.baseline.get("items", [])},
        "lane_rows": [(lane["id"], snapshot["lane_counts"].get(lane["id"], 0)) for lane in config.lanes],
        "mix": snapshot["evidence_mix"],
        "cadence": [(entry["date"], entry["count"]) for entry in sorted(history, key=lambda e: e["date"])],
        "history": history,
        "fetch": snapshot.get("fetch", {}),
        "notes": load_notes(day),
    }
    # The pages embed the charts, so re-rendering without them would publish a page
    # whose numbers and whose images disagree.
    write_charts(config, ctx, root)
    written = write_daily(config, ctx, root)
    if write_readme:
        for lang in LANGS:
            if (root/README_FILES[lang]).exists():
                written.append(inject_readme(config, lang, ctx, root))
    drafted = len((ctx["notes"] or {}).get("notes", {}))
    print(f"[rerender] {day}: {len(picked)} published picks, {drafted} drafted notes, "
          f"{len(written)} files written")
    return ctx


def write_charts(config: Config, ctx: dict[str, Any], root: Path) -> None:
    """Render one chart set per language, and an unsuffixed English set because
    already-published pages link to those names."""
    assets = root/"assets" if root != ROOT else ASSETS_DIR
    for lang in LANGS:
        ui = config.ui(lang)
        titles = {"lanes": ui["lane_distribution"], "cadence": ui["cadence"],
                  "mix": ui["source_mix"]}
        rows = [(config.chart_label(lane_id, lang), count) for lane_id, count in ctx["lane_rows"]]
        charts.write_all(assets, rows, ctx["cadence"], ctx["mix"], ctx["generated"],
                         titles=titles, suffix=f".{lang}")
        if lang == "en":
            charts.write_all(assets, rows, ctx["cadence"], ctx["mix"], ctx["generated"],
                             titles=titles)


def run(offline: bool = False, limit: int = 8, day: str | None = None, write_readme: bool = True,
        out: Path | None = None) -> dict[str, Any]:
    """Generate one day of radar.

    `out` redirects every generated file under another directory, so a reader can
    inspect real output without rewriting their checkout. The run log is still read
    from the repository, so the repeat window keeps working, and written to `out`.
    """
    root = Path(out) if out else ROOT
    config = load_config()
    day = day or datetime.now(timezone.utc).date().isoformat()
    ctx = build_context(config, offline=offline, limit=limit, day=day)

    write_charts(config, ctx, root)
    written: list[Path] = write_daily(config, ctx, root)
    if write_readme:
        for lang in ("zh", "en", "ja"):
            if (root/README_FILES[lang]).exists():
                written.append(inject_readme(config, lang, ctx, root))
            else:
                print(f"[radar] no {README_FILES[lang]} under {root}: skipping injection")
    written.append(write_latest(ctx, root))
    written.append(update_index(config, ctx, ctx["history"], root))
    save_history(ctx["history"], keep_urls_days=ctx["repeat_days"],
                 path=root/"radar"/"history.json")

    print(
        f"[radar] {day}: {len(ctx['picked'])} picked from {ctx['live_count']} live items, "
        f"{len(ctx['baseline_all'])} baseline entries, {len(written)} files written"
    )
    return ctx


def degraded(fetch: dict[str, Any], offline: bool) -> bool:
    """True when a network round was attempted and no source answered.

    Without this a total outage still writes a complete-looking radar from the
    curated baseline, commits it and reports success -- indistinguishable from a
    day with no news.
    """
    return not offline and bool(fetch.get("attempted")) and not fetch.get("answered")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the Physical AI Radar daily update.")
    parser.add_argument("--offline", action="store_true", help="skip all network sources")
    parser.add_argument("--limit", type=int, default=8, help="max items on the daily shortlist")
    parser.add_argument("--date", dest="day", default=None, help="override the run date (YYYY-MM-DD)")
    parser.add_argument("--no-readme", action="store_true", help="do not touch the README files")
    parser.add_argument("--rerender", action="store_true",
                        help="re-render the published day from radar/latest.json, without fetching")
    parser.add_argument("--out", type=Path, default=None,
                        help="write everything under this directory instead of the repository")
    args = parser.parse_args(argv)
    if args.rerender:
        rerender(day=args.day, out=args.out, write_readme=not args.no_readme)
        return 0
    ctx = run(offline=args.offline, limit=args.limit, day=args.day,
              write_readme=not args.no_readme, out=args.out)
    if degraded(ctx["fetch"], args.offline):
        print("[radar] every source failed: refusing to publish this as a quiet day")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
