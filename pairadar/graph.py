"""Machine-readable exports for the explore page: `radar/graph/items.json` and `radar/graph/lane-time.json`.

Both are public files in the same published tree as `radar/feed.json`, schema `radar-graph-1`.

`items.json` holds every published pick, one entry per item, with the lane as an id. From the day this
module is wired in, the nightly run appends the day's picks from the run itself. Days before that
exist only as the published daily pages, so the first run also fills every day of `history.json` that
has no entries yet by reading that day's English page, and only the `## Today's picks` section of it:
the curated baseline, the charts and the watch list are other sections and are never read. A day whose
page cannot be read exactly (an unknown lane, signal or source, or a count that is not the run's count)
is not guessed: it is listed under `backfill.gaps` with the reason.

`lane-time.json` is derived from `history.json` and `items.json`: for every day from the first run to
the run being written and every lane, the picks by evidence class, and the size of the candidate pool
(`null` before the run log recorded one, never zero). A day whose stored entries do not add up to the number
of items the run published (a gap in `items.json`) has `picks_known: false`, is listed in `mismatch_days`, and
has `null` picks in every cell: a zero there would read as "nothing was picked". It carries the selection
rules in force over time, from `data/selection-rules.json`, so that a chart can say that two days are not
measured the same way.

Nothing here changes what is picked or published. A problem with these files is reported on stdout
and the radar run carries on.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import ROOT, Config, Item, dump_json
from .feeds import record
from .fetch import link_digest

SCHEMA = "radar-graph-1"
ITEMS_FILE = "items.json"
LANE_TIME_FILE = "lane-time.json"
RULES_FILE = "selection-rules.json"
EVIDENCE = ("O", "R", "M")
# A lane's English name before it was renamed: the lane `safety` was "Safety, authority & compliance" until
# 2026-09-25 (git: 6c71ba8 had this name under id `safety`, fc1e898 renamed it).
LANE_ALIASES = {"Safety, authority & compliance": "safety"}


class GraphError(Exception):
    """A graph file or a daily page that cannot be used as it is."""


# --- items ---------------------------------------------------------------------------

def item_entry(item: Item, day: str) -> dict[str, Any]:
    """One pick as `items.json` stores it: the public fields of `feed.json`'s `_radar` record."""
    rec = record(item, day, "")
    return {"id": rec["id"], "date": day, "published": rec["published"], "lane": rec["lane"],
            "evidence": rec["evidence"], "source_id": rec["source_id"], "publisher": rec["publisher"],
            "title": rec["title"], "url": rec["url"], "signals": rec["signals"], "numbers": rec["numbers"],
            "excerpt": rec["excerpt"]}


def load_items(path: Path) -> dict[str, Any] | None:
    """The stored document, None when there is no file yet; anything else that is wrong raises."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise GraphError(f"{path.name} cannot be read: {error}") from error
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or not isinstance(payload.get("items"), list)):
        raise GraphError(f"{path.name} is not a {SCHEMA} items file")
    return payload


def empty_items() -> dict[str, Any]:
    return {"schema": SCHEMA, "as_of": None, "first_run": None,
            "backfill": {"source": "radar/daily/<day>.en.md, section Today's picks", "gaps": []}, "items": []}


def append_day(doc: dict[str, Any], entries: list[dict[str, Any]], day: str) -> dict[str, Any]:
    """Replace `day`'s entries with `entries`; a rerun of a day never duplicates it. Days stay in order."""
    kept = [entry for entry in doc["items"] if entry.get("date") != day]
    return {**doc, "items": sorted(kept + entries, key=lambda entry: entry["date"])}


# --- reading a published day --------------------------------------------------------

_UNESCAPE = re.compile(r"\\([\\`*_\[\]])")
_LANE_LINE = re.compile(r"^- \*\*[^*]+\*\*: (?P<lane>.+?) ｜ \*\*[^*]+\*\*: `\[(?P<evidence>[ORM])\]` ｜ "
                        r"\*\*[^*]+\*\*: \[(?P<publisher>.*)\]\((?P<url>[^)]*)\) ｜ `(?P<published>[0-9-]+)`$")
_CONTEXT = re.compile(r" \(“(?:[^”\\]|\\.)*”\)")
_TICKS = re.compile(r"`([^`]+)`")


def unmark(text: str) -> str:
    """Undo `md_text`: the Markdown escapes, `<`, and the two Liquid guards."""
    text = text.replace("&#123;&#123;", "{{").replace("&#123;%", "{%").replace("&lt;", "<")
    return _UNESCAPE.sub(r"\1", text)


def unlink_url(url: str) -> str:
    """Undo `md_url`."""
    return url.replace("%20", " ").replace("%28", "(").replace("%29", ")")


def picks_section(text: str) -> list[str]:
    """The lines of the first `## ` section (the day's picks), up to the next `## `."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        raise GraphError("no section found")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return lines[start + 1:end]


def source_ids(config: Config) -> dict[str, str]:
    """Publisher label -> source id, for the sources that can publish a pick; an ambiguous label maps to None."""
    found: dict[str, str | None] = {}
    for entry in [*config.sources.get("feeds", []), *config.sources.get("page_watch", [])]:
        label, ident = entry.get("publisher"), entry.get("id")
        if not label or not ident:
            continue
        found[label] = ident if found.get(label, ident) == ident else None
    return found


def parse_picks(text: str, day: str, config: Config) -> list[dict[str, Any]]:
    """The picks of one English daily page as `items.json` entries. Raises GraphError on anything it cannot place."""
    lanes = {lane["name"]["en"]: lane["id"] for lane in config.lanes} | LANE_ALIASES
    signals = {name: ident for ident, name in config.ui("en")["signals"].items()}
    publishers = source_ids(config)
    blocks: list[list[str]] = []
    for line in picks_section(text):
        if line.startswith("### "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    entries = []
    for block in blocks:
        heading = re.match(r"^### \d+\. (?P<title>.*)$", block[0])
        if not heading:
            raise GraphError(f"unreadable heading: {block[0][:60]}")
        meta = next((_LANE_LINE.match(line) for line in block if line.startswith("- **") and "｜" in line), None)
        if not meta:
            raise GraphError(f"no lane line under: {block[0][:60]}")
        if meta["lane"] not in lanes:
            raise GraphError(f"unknown lane name: {meta['lane']}")
        url = unlink_url(meta["url"])
        label = unmark(meta["publisher"])
        if label.startswith("arXiv "):
            if not re.fullmatch(r"https://arxiv\.org/abs/\d{4}\.\d{4,5}", url):
                raise GraphError(f"arXiv link in an older format, its id is not recoverable: {url}")
            source, ident = "arxiv", "arxiv:" + url.rsplit("/", 1)[-1]
        else:
            source = publishers.get(label)
            if not source:
                raise GraphError(f"unknown or ambiguous source label: {label}")
            ident = f"{source}:{link_digest(url)}"
        numbers: list[str] = []
        names: list[str] = []
        excerpt = ""
        for line in block[1:]:
            if line.startswith("- **") and "｜" not in line and ": " in line:
                label_text, _, rest = line[2:].partition(": ")
                if label_text == "**Key numbers**":
                    numbers = _TICKS.findall(_CONTEXT.sub("", rest))
                elif label_text == "**Signals**":
                    names = _TICKS.findall(rest)
            elif line.startswith("> ") and not excerpt:
                excerpt = unmark(line[2:])
        for name in names:
            if name not in signals:
                raise GraphError(f"unknown signal name: {name}")
        entries.append({"id": ident, "date": day, "published": meta["published"], "lane": lanes[meta["lane"]],
                        "evidence": meta["evidence"], "source_id": source, "publisher": label,
                        "title": unmark(heading["title"]), "url": url, "signals": [signals[n] for n in names],
                        "numbers": numbers, "excerpt": excerpt})
    return entries


def fill_missing(doc: dict[str, Any], history: list[dict[str, Any]], daily_dir: Path, config: Config,
                 skip_day: str | None = None) -> dict[str, Any]:
    """Add the days of `history` that have no entries, read from their published pages.

    A day is added only when its page is read exactly and holds as many picks as the run recorded; otherwise
    it is listed under `backfill.gaps` with the reason, and nothing is guessed."""
    have = {entry["date"] for entry in doc["items"]}
    gaps = {gap["date"]: gap for gap in doc.get("backfill", {}).get("gaps", [])}
    added: list[dict[str, Any]] = []
    for run in sorted(history, key=lambda entry: entry["date"]):
        day = run["date"]
        if day == skip_day:
            continue
        if day in have or not run.get("count"):
            gaps.pop(day, None)
            continue
        try:
            entries = parse_picks((daily_dir / f"{day}.en.md").read_text(encoding="utf-8"), day, config)
            if len(entries) != run["count"]:
                raise GraphError(f"the page has {len(entries)} picks, the run recorded {run['count']}")
        except (OSError, GraphError) as error:
            gaps[day] = {"date": day, "reason": str(error)}
            continue
        gaps.pop(day, None)
        added.extend(entries)
    backfill = {**doc.get("backfill", empty_items()["backfill"]), "gaps": [gaps[day] for day in sorted(gaps)]}
    return {**doc, "backfill": backfill, "items": sorted(doc["items"] + added, key=lambda entry: entry["date"])}


# --- lane-time -----------------------------------------------------------------------

def load_rules(path: Path) -> list[dict[str, Any]]:
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GraphError(f"{path.name} cannot be read: {error}") from error
    if (not isinstance(rules, list) or not all(isinstance(rule, dict) and rule.get("from") for rule in rules)
            or [rule["from"] for rule in rules] != sorted(rule["from"] for rule in rules)):
        raise GraphError(f"{path.name} must be a list of dated rules in date order")
    for rule in rules:
        for key in ("per_lane", "per_day"):
            # The size in force when the rule took effect: a whole number, or null where it could not be verified.
            if key not in rule or not (rule[key] is None or (isinstance(rule[key], int) and not isinstance(rule[key], bool)
                                                              and rule[key] > 0)):
                raise GraphError(f"{path.name}: rule {rule['from']} needs {key} as a positive integer or null")
    return rules


def lane_time(config: Config, history: list[dict[str, Any]], items: dict[str, Any],
              rules: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    """Per day and lane: picks by evidence class and the pool. Every day from the first run to `as_of` is present."""
    runs = {run["date"]: run for run in history}
    first = min(runs)
    lane_ids = [lane["id"] for lane in config.lanes]
    by_day: dict[str, dict[str, dict[str, int]]] = {}
    for entry in items["items"]:
        cell = by_day.setdefault(entry["date"], {}).setdefault(entry["lane"], {k: 0 for k in EVIDENCE})
        cell[entry["evidence"]] = cell.get(entry["evidence"], 0) + 1
    days, mismatched = [], []
    cursor, end = date.fromisoformat(first), date.fromisoformat(as_of)
    while cursor <= end:
        day = cursor.isoformat()
        run = runs.get(day)
        pool = (run or {}).get("pool") or {}
        lanes = {}
        for lane in lane_ids:
            counts = by_day.get(day, {}).get(lane, {k: 0 for k in EVIDENCE})
            lanes[lane] = {**{k: counts.get(k, 0) for k in EVIDENCE}, "picked": sum(counts.values()),
                           "pool": pool["by_lane"].get(lane, 0) if "by_lane" in pool else None}
        picked = sum(cell["picked"] for cell in lanes.values())
        unreadable = bool(run) and run.get("count") != picked
        if unreadable:
            # The run published `count` items and the stored entries do not add up to it: the lane of each is not
            # known, so no cell may say 0 (that would read as "nothing was picked"). The pool stays: it is the run's.
            mismatched.append(day)
            lanes = {lane: {**{k: None for k in EVIDENCE}, "picked": None, "pool": cell["pool"]}
                     for lane, cell in lanes.items()}
        days.append({"date": day, "ran": run is not None, "count": run.get("count") if run else None,
                     "picks_known": not unreadable,
                     "failed_sources": list((run or {}).get("failed", [])),
                     "unavailable": list((run or {}).get("unavailable", [])),
                     "fallback": list((run or {}).get("fallback", [])), "lanes": lanes})
        cursor += timedelta(days=1)
    pooled = sorted(day for day, run in runs.items() if run.get("pool"))
    return {"schema": SCHEMA, "as_of": as_of, "first_run": first, "pool_since": pooled[0] if pooled else None,
            "per_lane_cap": (runs[pooled[-1]]["pool"].get("per_lane_cap") if pooled else None),
            "mismatch_days": mismatched, "rules": rules, "days": days}


# --- the nightly step ----------------------------------------------------------------

def write_graph(config: Config, ctx: dict[str, Any], root: Path = ROOT, source: Path = ROOT) -> list[Path]:
    """Append the day to `items.json` (filling earlier days from their pages) and rewrite `lane-time.json`.

    Existing files are read from `source`, the result written under `root`. On any problem nothing is written
    and the reason is printed: these files never stop a run."""
    folder_in, folder_out = source / "radar" / "graph", root / "radar" / "graph"
    try:
        doc = load_items(folder_in / ITEMS_FILE) or empty_items()
        day = ctx["date"]
        doc = append_day(doc, [item_entry(item, day) for item in ctx["picked"]], day)
        doc = fill_missing(doc, ctx["history"], source / "radar" / "daily", config, skip_day=day)
        history = ctx["history"]
        as_of = max(day, doc.get("as_of") or day)               # re-running an older day does not move the date back
        doc["as_of"], doc["first_run"] = as_of, min(run["date"] for run in history)
        rules = load_rules(source / "data" / RULES_FILE)
        table = lane_time(config, history, doc, rules, as_of)
    except (GraphError, KeyError, ValueError) as error:
        print(f"[radar] graph export skipped: {error}")
        return []
    paths = [folder_out / ITEMS_FILE, folder_out / LANE_TIME_FILE]
    dump_json(paths[0], doc)
    dump_json(paths[1], table)
    return paths
