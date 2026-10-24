"""CLI entry point: `python -m pairadar [--offline] [--limit N]`."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import charts, heat
from .banner import write_banners
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
from .feeds import FEED_JSON, load_store, upsert, within, write_feeds
from .render import chart_title, inject_readme, update_index, write_daily, write_latest
from .site import write_site

HISTORY_PATH = ROOT / "radar" / "history.json"
# The page_watch baseline: per watched page, a digest and the first-party links or
# line digests seen last time. No page text is kept.
PAGE_STATE_PATH = ROOT / "radar" / "page-watch.json"
# The lane and evidence charts count the picks of this many days, today included.
# The feed store they are read from keeps at most 100 entries, so a week always fits.
CHART_DAYS = 7


def load_page_state(path: Path = PAGE_STATE_PATH) -> dict[str, Any]:
    """The page_watch baseline, or {} when there is none or it cannot be read.

    The baseline only saves re-announcing old posts; a damaged one (a torn write, a bad
    merge) is rebuilt on this run, as on a first look, instead of failing the radar.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[radar] page_watch baseline {path.name} unreadable ({type(exc).__name__}); rebuilding it")
        return {}
    if not isinstance(payload, dict):
        print(f"[radar] page_watch baseline {path.name} is not an object; rebuilding it")
        return {}
    return payload


def save_page_state(path: Path, payload: dict[str, Any]) -> None:
    """Write the baseline atomically: a temporary file in the same directory, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o644)  # a public, committed file, like the rest of radar/
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


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


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def window_detail(reference: date, lookback: int, max_age: int, repeat_days: int) -> dict[str, Any]:
    """What the day's pool actually covered.

    The page used to print only the arXiv lookback ("09-18 → 09-21") while feed posts
    up to thirty days old were eligible, so a pick dated two weeks earlier looked
    like a mistake. Both horizons, and the repeat guard, are stated now.
    """
    return {
        "day": reference.isoformat(),
        "papers_from": (reference - timedelta(days=lookback)).isoformat(),
        "posts_from": (reference - timedelta(days=max_age)).isoformat(),
        "repeat_days": repeat_days,
    }


def window_summary(detail: dict[str, Any]) -> str:
    """The language-neutral form kept in radar/latest.json for existing consumers."""
    return (f"papers {detail['papers_from']} → {detail['day']} · "
            f"posts {detail['posts_from']} → {detail['day']} (UTC)")


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
    generated = now_iso()
    lookback = int(config.sources.get("arxiv", {}).get("lookback_days", 3))
    filters = config.sources.get("filters", {})
    repeat_days = int(filters.get("repeat_days", 7))
    history = [entry for entry in load_history() if entry["date"] != day]
    earlier = [entry for entry in load_store(ROOT/"radar"/FEED_JSON) if entry["date"] != day]
    told = [entry["title"] for entry in within(earlier, day, repeat_days) if entry["evidence"] != "R"]
    page_state = load_page_state()
    report = fetch_all(config, offline=offline, reference=reference, page_state=page_state)
    signals = report.signals or heat.Signals()
    heat_state = heat.next_state(heat.load_state(), signals.stars, day)
    live = deduplicate(heat.apply(enrich(prefilter(report.items, config, reference), config, reference),
                                  config, signals, heat_state))
    highlights = filters.get("highlights", {})
    share = highlights.get("max_arxiv_share")
    quota: dict[str, Any] = {}
    picked = select(live, limit=limit, seen=recent_urls(history, reference, repeat_days),
                    per_source=_optional_int(filters.get("per_source")),
                    per_evidence=_optional_int(filters.get("per_evidence")), told=told,
                    max_arxiv=int(limit * float(share)) if share is not None else None,
                    reserve=list(highlights.get("reserve", [])), report=quota)

    baseline_all = enrich(baseline_items(config), config, reference)
    curated = {
        f"baseline:{raw['id']}": raw.get("why", {}) for raw in config.baseline.get("items", [])
    }

    # The charts count what was published, not what is tracked: with the 29 curated
    # baseline entries in the count, today's eight picks barely moved the bars and
    # the chart looked the same every day.
    recent = [Item.from_dict(entry) for entry in within(upsert(earlier, picked, day, generated),
                                                       day, CHART_DAYS - 1)]
    lane_rows = lane_distribution(recent, config)
    mix = evidence_mix(recent)

    history.append({
        "date": day,
        "count": len(picked),
        "lanes": len({item.lane for item in picked}),
        "urls": [url_key(item.url) for item in picked],
        # Kept for every run, not pruned with the URLs: pairadar.health reads a
        # longer window to spot a source that has quietly stopped answering.
        "failed": sorted(report.failed),
        "fallback": sorted(report.fallback),
        # A channel whose local service was down (page_watch without Firecrawl).
        "unavailable": sorted(report.unavailable),
        # page_watch pages not read, kept out of `failed`: pairadar.health reports them
        # as page_watch_failing, never as struggling, so source repair never picks one.
        "page_failed": sorted(report.page_watch.get("failed", [])),
        "page_robots_skipped": sorted(report.page_watch.get("robots_skipped", [])),
        # Feeds whose robots.txt disallows them, and attention APIs that did not answer:
        # reported by pairadar.health, never as struggling (source repair fixes feeds).
        "robots_skipped": sorted(report.robots_skipped),
        "signal_failed": sorted(signals.failed),
        # The highlights quota: what was reserved, what could not be filled.
        "quota": quota,
    })
    cadence = [(entry["date"], entry["count"]) for entry in sorted(history, key=lambda e: e["date"])]

    window = window_detail(reference, lookback, int(filters.get("max_age_days", 30)), repeat_days)
    return {
        "date": day,
        "generated": generated,
        "window": window_summary(window),
        "window_detail": window,
        "picked": picked,
        "baseline": rotate_baseline(baseline_all, day),
        "baseline_all": baseline_all,
        "curated": curated,
        "lane_rows": lane_rows,
        "mix": mix,
        "cadence": cadence,
        "history": history,
        "live_count": len(live),
        "chart_days": CHART_DAYS,
        "repeat_days": repeat_days,
        "fetch": report.to_dict(),
        "notes": load_notes(day),
        # Only a round that changed the baseline (a digest, a link, a line, what was
        # offered) rewrites it; offline, with Firecrawl down or with every page as it
        # was, the file is left alone, so it does not change every day.
        # Written only when a star count changed (offline, or every API down: untouched).
        "heat_state": heat_state if heat_state.get("stars") != heat.load_state().get("stars") else None,
        "page_state": (report.page_state if report.page_state.get("sources")
                       and report.page_state.get("sources") != page_state.get("sources") else None),
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
        "window_detail": snapshot.get("window_detail"),
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
        "chart_days": snapshot.get("chart_days"),
    }
    # The pages embed the charts, so re-rendering without them would publish a page
    # whose numbers and whose images disagree.
    write_charts(config, ctx, root)
    written = write_daily(config, ctx, root) + write_site(config, ctx, root)
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
        titles = {"lanes": chart_title(ui, ui["lane_distribution"], ctx), "cadence": ui["cadence"],
                  "mix": chart_title(ui, ui["source_mix"], ctx)}
        rows = [(config.chart_label(lane_id, lang), count) for lane_id, count in ctx["lane_rows"]]
        charts.write_all(assets, rows, ctx["cadence"], ctx["mix"], ctx["generated"],
                         titles=titles, suffix=f".{lang}")
        if lang == "en":
            charts.write_all(assets, rows, ctx["cadence"], ctx["mix"], ctx["generated"],
                             titles=titles)
    # the READMEs open on the day's banner, drawn from the same context
    write_banners(config, ctx, assets, LANGS)


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
    written: list[Path] = write_daily(config, ctx, root) + write_site(config, ctx, root)
    if write_readme:
        for lang in ("zh", "en", "ja"):
            if (root/README_FILES[lang]).exists():
                written.append(inject_readme(config, lang, ctx, root))
            else:
                print(f"[radar] no {README_FILES[lang]} under {root}: skipping injection")
    written.append(write_latest(ctx, root))
    # Before the index, which lists the weekly pages this writes.
    written.extend(write_feeds(config, ctx, root))
    written.append(update_index(config, ctx, ctx["history"], root))
    save_history(ctx["history"], keep_urls_days=ctx["repeat_days"],
                 path=root/"radar"/"history.json")
    if ctx.get("page_state"):
        save_page_state(root/"radar"/"page-watch.json", ctx["page_state"])
    if ctx.get("heat_state"):
        heat.save_state(root/"radar"/"heat-state.json", ctx["heat_state"])

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


PAGE_WATCH_EVIDENCE_SCHEMA = "pairadar-page-watch-v1"


def page_watch_evidence_dir() -> Path:
    """Where the delivery-freshness objective `radar-page-watch` (noteflow-auto) reads its evidence."""
    base = os.environ.get("PAIRADAR_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "pairadar")
    return Path(base) / "page-watch"


def page_watch_status(page_watch: dict[str, Any] | None, error: bool = False) -> str:
    """The evidence `status` for one run, from FetchReport.to_dict()["page_watch"] only.

    - "error": the radar run itself raised;
    - "firecrawl-unavailable": the local Firecrawl did not answer, or stopped answering
      part way (the channel stopped, so not every page was looked at);
    - "skipped": page_watch disabled, or no entry reached;
    - "all-failed": pages were attempted but none could be read (every page failed or
      was disallowed by robots.txt);
    - "ok": at least one watched page was read.

    Only "ok" is success for the freshness objective (noteflow-auto radar-page-watch);
    every other value is a non-success outcome.
    """
    block = page_watch if isinstance(page_watch, dict) else {}
    if error:
        return "error"
    if block.get("unavailable"):
        return "firecrawl-unavailable"
    if not block.get("enabled") or not block.get("attempted"):
        return "skipped"
    if not block.get("checked"):
        return "all-failed"
    return "ok"


def write_page_watch_evidence(fetch: dict[str, Any] | None, day: str,
                              directory: Path | None = None, error: bool = False) -> Path | None:
    """One record per run day for the freshness objective, written atomically.

    Contract (noteflow-auto config/delivery-freshness.json, objective radar-page-watch):
    `schema` "pairadar-page-watch-v1", `channel` "page_watch", `status` from
    `page_watch_status` (exactly "ok" only when at least one watched page was read;
    "all-failed", "firecrawl-unavailable", "skipped" and "error" are non-success),
    `finished_at` ISO-8601 with an offset. It also carries `sources_checked` (a count),
    `sources_failed` and `sources_robots_skipped` (source ids). Never page text.

    Read only from the explicit `page_watch` block that fetch_page_watch produced, never
    from the report's overall counts. The evidence is observational: any exception in
    here is logged and swallowed, so it can never fail the radar.
    """
    try:
        block = (fetch or {}).get("page_watch") if isinstance(fetch, dict) else None
        block = block if isinstance(block, dict) else {}
        record = {"schema": PAGE_WATCH_EVIDENCE_SCHEMA, "channel": "page_watch",
                  "status": page_watch_status(block, error=error),
                  "day": day, "finished_at": datetime.now(timezone.utc).isoformat(),
                  "sources_checked": len(block.get("checked") or []),
                  "sources_failed": sorted(block.get("failed") or []),
                  "sources_robots_skipped": sorted(block.get("robots_skipped") or [])}
        target_dir = directory or page_watch_evidence_dir()
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_dir / f"{day}.json"
        fd, temporary = tempfile.mkstemp(dir=target_dir, prefix=f".{day}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=1)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        return target
    except Exception as exc:  # noqa: BLE001 -- evidence is observational; never fail the radar
        print(f"[radar] page_watch evidence not written: {type(exc).__name__}: {exc}")
        return None


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
    # Only a real production round reports to the freshness objective; offline and --out runs
    # (tests, previews) never touch the state directory. A run that raises still leaves a record
    # (status "error"), so the objective flags the failed run instead of waiting 72h for staleness.
    reporting = not args.offline and args.out is None
    day = args.day or datetime.now(timezone.utc).date().isoformat()
    try:
        ctx = run(offline=args.offline, limit=args.limit, day=args.day,
                  write_readme=not args.no_readme, out=args.out)
    except BaseException:
        if reporting:
            write_page_watch_evidence(None, day, error=True)
        raise
    if reporting:
        write_page_watch_evidence(ctx.get("fetch"), ctx.get("date") or day)
    if degraded(ctx["fetch"], args.offline):
        print("[radar] every source failed: refusing to publish this as a quiet day")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
