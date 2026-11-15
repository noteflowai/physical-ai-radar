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
measured the same way. The `from` of a rule is the date of its commit, not the first run that used it.

The two files are one version: `lane-time.json` carries `items_sha256`, the SHA-256 of the bytes of `items.json`, and
the pair is checked (`contract_problems`) and then written by `publish_pair`, which puts the first file back if the
second cannot be replaced. Between the two renames a reader can still see two generations; one that compares
`items_sha256` (`check_pair`) can always tell them apart. A run log whose `count` is missing, null, a bool or not a
whole number stops the export and leaves the previous pair (`check_history`).

Nothing here changes what is picked or published. A problem with these files is reported on stdout
and the radar run carries on.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import ROOT, Config, Item
from .feeds import record
from .fetch import link_digest

SCHEMA = "radar-graph-1"
ITEMS_FILE = "items.json"
LANE_TIME_FILE = "lane-time.json"
RULES_FILE = "selection-rules.json"
EVIDENCE = ("O", "R", "M")
ENTRY_KEYS = ("id", "date", "published", "lane", "evidence", "source_id", "publisher", "title", "url", "signals",
              "numbers", "excerpt")
# A lane's English name before it was renamed: the lane `safety` was "Safety, authority & compliance" until
# 2026-09-25 (git: 6c71ba8 had this name under id `safety`, fc1e898 renamed it).
LANE_ALIASES = {"Safety, authority & compliance": "safety"}
RULES_DATES = "the date of the commit that changed the rule, not the first run that used it"
SOURCE_EVIDENCE_NOTE = "each source's evidence class in the configuration at the time of the export, not on the day it failed"


class GraphError(Exception):
    """A graph file or a daily page that cannot be used as it is."""


def render_json(payload: Any) -> bytes:
    """The bytes a graph file is written as (the format of `config.dump_json`)."""
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _day(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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


def validate_items(doc: dict[str, Any], config: Config) -> None:
    """Refuse a stored document whose entries are not what this module writes (01 F4): wrong type, unknown lane or
    evidence, a missing or extra key, the same pick twice on one day, a malformed gap. Raises GraphError."""
    lanes = {lane["id"] for lane in config.lanes}
    text = ("id", "published", "source_id", "publisher", "title", "url", "excerpt")
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(doc["items"]):
        where = f"items[{index}]"
        if not isinstance(entry, dict) or set(entry) != set(ENTRY_KEYS):
            raise GraphError(f"{where} is not an entry with exactly the keys {', '.join(ENTRY_KEYS)}")
        if not all(isinstance(entry[key], str) for key in text) or not all(entry[key] for key in ("id", "title", "url", "source_id")):
            raise GraphError(f"{where} has a field that is not text")
        if not _day(entry["date"]) or not (entry["published"] == "" or _day(entry["published"])):
            raise GraphError(f"{where} has a date that is not YYYY-MM-DD")
        if entry["lane"] not in lanes or entry["evidence"] not in EVIDENCE:
            raise GraphError(f"{where} has an unknown lane or evidence class")
        for key in ("signals", "numbers"):
            if not isinstance(entry[key], list) or not all(isinstance(value, str) for value in entry[key]):
                raise GraphError(f"{where} has {key} that is not a list of text")
        if (entry["date"], entry["id"]) in seen:
            raise GraphError(f"{where} repeats {entry['id']} on {entry['date']}")
        seen.add((entry["date"], entry["id"]))
    backfill = doc.get("backfill", {})
    gaps = backfill.get("gaps", []) if isinstance(backfill, dict) else None
    if not isinstance(gaps, list) or not all(isinstance(gap, dict) and _day(gap.get("date")) and isinstance(gap.get("reason"), str)
                                             for gap in gaps):
        raise GraphError("backfill.gaps is not a list of {date, reason}")


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


def picks_section(text: str, heading: str) -> list[str]:
    """The lines of the one section titled exactly `## <heading>` (the day's picks), up to the next `## `.

    The section is found by its title, never by its position: a page whose first section is something else (the
    curated baseline, a notice) is not read as the picks. No such section, or more than one, raises GraphError."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == f"## {heading}"]
    if not starts:
        raise GraphError(f"no section titled '{heading}'")
    if len(starts) > 1:
        raise GraphError(f"{len(starts)} sections titled '{heading}'")
    start = starts[0]
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
    for line in picks_section(text, config.ui("en")["top_items"]):
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
    """Add the days of `history` whose stored picks are missing or incomplete, read from their published pages.

    A day is complete when the stored entries are as many as the run recorded. A missing or partial day is
    read again from its page, and its stored entries are replaced only when the page is read exactly and holds
    as many picks as the run recorded; otherwise it is listed under `backfill.gaps` with the reason (an
    unresolved gap is never cleared), and nothing is guessed."""
    stored: dict[str, int] = {}
    for entry in doc["items"]:
        stored[entry["date"]] = stored.get(entry["date"], 0) + 1
    gaps = {gap["date"]: gap for gap in doc.get("backfill", {}).get("gaps", [])}
    replaced: set[str] = set()
    added: list[dict[str, Any]] = []
    for run in sorted(history, key=lambda entry: entry["date"]):
        day, count, have = run["date"], run.get("count"), stored.get(run["date"], 0)
        if day == skip_day:                            # the run being written: its entries came from the run itself
            if have == (count or 0):
                gaps.pop(day, None)
            continue
        if have == (count or 0):                       # complete: all of them stored, or none were published
            gaps.pop(day, None)
            continue
        try:
            entries = parse_picks((daily_dir / f"{day}.en.md").read_text(encoding="utf-8"), day, config)
            if len(entries) != count:
                raise GraphError(f"the page has {len(entries)} picks, the run recorded {count}")
        except (OSError, GraphError) as error:
            held = f"{have} of {count} stored; " if have else ""
            gaps[day] = {"date": day, "reason": f"{held}{error}"}
            continue
        gaps.pop(day, None)
        replaced.add(day)
        added.extend(entries)
    backfill = {**doc.get("backfill", empty_items()["backfill"]), "gaps": [gaps[day] for day in sorted(gaps)]}
    kept = [entry for entry in doc["items"] if entry["date"] not in replaced]
    return {**doc, "backfill": backfill, "items": sorted(kept + added, key=lambda entry: entry["date"])}


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
        # `show`: whether the page draws this change as a line (the editor's call: does it make the two sides measured
        # differently?). A rule that is not drawn stays in the data and in the count of "smaller changes".
        if rule.get("show") is not True and rule.get("show") is not False:
            raise GraphError(f"{path.name}: rule {rule['from']} needs show as true or false")
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
        by_lane = pool.get("by_lane")
        lanes = {}
        for lane in lane_ids:
            counts = by_day.get(day, {}).get(lane, {k: 0 for k in EVIDENCE})
            # No `pool` in the run log: unknown (null), never zero. A pool that is there but has no number for a lane
            # (or not a whole one) is unknown for that lane too: the run log writes every lane, with 0 where none.
            known = by_lane.get(lane) if isinstance(by_lane, dict) else None
            lanes[lane] = {**{k: counts.get(k, 0) for k in EVIDENCE}, "picked": sum(counts.values()),
                           "pool": known if _count(known) else None}
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
            "mismatch_days": mismatched, "rules_dates": RULES_DATES, "rules": rules,
            "source_evidence": source_evidence(config, days), "source_evidence_note": SOURCE_EVIDENCE_NOTE, "days": days}


def source_evidence(config: Config, days: list[dict[str, Any]]) -> dict[str, str]:
    """The evidence class (O, R or M) of every source that is named in `failed_sources` on any day of the table.

    Taken from the CURRENT configuration (`arxiv`, `feeds`, `page_watch`), not as it was on each day. A source the
    configuration does not know, or that has no class, is left out: a reader treats an id that is not here as "any class"."""
    known = {"arxiv": config.sources.get("arxiv", {}).get("evidence")}
    for entry in [*config.sources.get("feeds", []), *config.sources.get("page_watch", [])]:
        known[entry.get("id")] = entry.get("evidence")
    named = sorted({source for day in days for source in day["failed_sources"]})
    return {source: known[source] for source in named if known.get(source) in EVIDENCE}


def contract_problems(doc: dict[str, Any], table: dict[str, Any], config: Config) -> list[str]:
    """What is wrong with a pair about to be published, as the consumers of `radar-graph-1` rely on it. Empty: sound.

    The pair is `doc` (items.json) and `table` (lane-time.json). Nothing here is a guess about the data: it checks
    that each file says what the other says, and that an unknown is null and never a zero."""
    problems: list[str] = []
    if doc.get("schema") != SCHEMA or table.get("schema") != SCHEMA:
        problems.append("schema")
    if doc.get("as_of") != table.get("as_of") or doc.get("first_run") != table.get("first_run"):
        problems.append("the two files disagree on as_of or first_run")
    if table.get("items_sha256") != hashlib.sha256(render_json(doc)).hexdigest() or table.get("items_count") != len(doc["items"]):
        problems.append("lane-time.json does not describe this items.json (items_sha256 or items_count)")
    lane_ids = [lane["id"] for lane in config.lanes]
    days = table.get("days", [])
    names = [entry.get("date") for entry in days]
    if not names or names[0] != table.get("first_run") or names[-1] != table.get("as_of"):
        problems.append("days do not run from first_run to as_of")
    elif names != [(date.fromisoformat(names[0]) + timedelta(days=offset)).isoformat() for offset in range(len(names))]:
        problems.append("days are not one per calendar day")
    unknown = []
    for entry in days:
        cells = entry.get("lanes", {})
        if list(cells) != lane_ids:
            problems.append(f"{entry.get('date')}: lanes are not exactly the configured lanes")
            continue
        if entry.get("picks_known") is False:
            unknown.append(entry.get("date"))
            if any(cell.get(key) is not None for cell in cells.values() for key in (*EVIDENCE, "picked")):
                problems.append(f"{entry.get('date')}: picks are not known but a cell says a number")
        elif entry.get("picks_known") is True:
            counted = [cell.get("picked") for cell in cells.values()]
            if not all(_count(value) for value in counted) or (entry.get("ran") and sum(counted) != entry.get("count")):
                problems.append(f"{entry.get('date')}: the cells do not add up to the day's count")
            for cell in cells.values():
                if not all(_count(cell.get(key)) for key in EVIDENCE) or cell.get("picked") != sum(cell[key] for key in EVIDENCE):
                    problems.append(f"{entry.get('date')}: a lane's evidence classes do not add up to its picks")
                    break
        else:
            problems.append(f"{entry.get('date')}: picks_known is not true or false")
        if not all(cell.get("pool") is None or _count(cell.get("pool")) for cell in cells.values()):
            problems.append(f"{entry.get('date')}: a pool is neither null nor a count")
    if table.get("mismatch_days") != unknown:
        problems.append("mismatch_days is not exactly the days whose picks are not known")
    named = {source for entry in days for source in entry.get("failed_sources", [])}
    evidence = table.get("source_evidence")
    if (not isinstance(evidence, dict) or not set(evidence) <= named
            or not all(value in EVIDENCE for value in evidence.values())):
        problems.append("source_evidence is not a map from sources named in failed_sources to O, R or M")
    gap_days = {gap["date"] for gap in doc.get("backfill", {}).get("gaps", [])} & set(names)
    if not gap_days <= set(unknown):
        problems.append("a backfill gap is on a day whose picks are said to be known")
    return problems


def check_pair(folder: Path, config: Config) -> list[str]:
    """For a consumer, and for the tests: read both files from `folder` and report why they cannot be used together
    (a missing file, or two generations: `items_sha256` is the SHA-256 of the items.json bytes). Empty: one version."""
    try:
        raw = (folder / ITEMS_FILE).read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        table = json.loads((folder / LANE_TIME_FILE).read_bytes().decode("utf-8"))
    except (OSError, ValueError) as error:
        return [f"cannot read the pair: {error}"]
    if table.get("items_sha256") != hashlib.sha256(raw).hexdigest():
        return ["lane-time.json was not written for this items.json (mixed generations)"]
    return contract_problems(doc, table, config)


# --- the nightly step ----------------------------------------------------------------

def _stage(folder: Path, name: str, data: bytes) -> Path:
    """Write `data` to a temporary file next to `name`, flushed to disk, readable by everyone like the rest of radar/."""
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return Path(temporary)


def check_history(history: list[dict[str, Any]]) -> None:
    """Refuse to export from a run log whose `count` cannot be trusted (00's ruling D1, from 01's two counter-examples).

    Every run must have a date and a `count` that is a whole number of at least 0 and not a bool: `True` would equal
    one stored pick, and a missing or null count would read as zero. Otherwise the export is skipped as a whole and the
    previous pair stays where it is. Raises GraphError."""
    for index, run in enumerate(history):
        if not isinstance(run, dict) or not _day(run.get("date")) or not _count(run.get("count")):
            shown = run.get("date") if isinstance(run, dict) else None
            raise GraphError(f"the run log has no usable date or count for run {index} ({shown!r})")


def _sync_directory(folder: Path) -> None:
    """Flush the directory entries of `folder` (the renames) to disk."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_pair(folder: Path, items_bytes: bytes, lane_bytes: bytes) -> list[Path]:
    """Replace the pair so that a failure does not normally leave a reader with two generations (01 F2).

    A reader is safe from two generations in two cases only: the second replacement fails and the first file is put
    back (below), or the reader compares `items_sha256` with the bytes of `items.json` (`check_pair`). Between the two
    renames, or if the process dies there, or if putting the file back fails too, a reader without that check can see
    the new `items.json` with the old `lane-time.json`.

    Both files are first written in full beside their names; only then is each replaced (an atomic rename). If the
    second replacement fails, the first file is put back as it was (or removed, if there was none). Should even that
    fail, the error says so, and `lane-time.json` still names the `items.json` it belongs to (`items_sha256`), so a
    reader can tell the two apart. Raises OSError or GraphError; the caller reports it and the run goes on."""
    folder.mkdir(parents=True, exist_ok=True)
    items_path, lane_path = folder / ITEMS_FILE, folder / LANE_TIME_FILE
    before = items_path.read_bytes() if items_path.exists() else None
    staged: list[Path | None] = []
    try:
        # Both files are staged BEFORE either is replaced: a staging failure (disk full, quota) then leaves nothing
        # replaced. Do not move the second staging after the first replacement.
        staged.append(_stage(folder, ITEMS_FILE, items_bytes))
        staged.append(_stage(folder, LANE_TIME_FILE, lane_bytes))
        os.replace(staged[0], items_path)
        staged[0] = None
        try:
            os.replace(staged[1], lane_path)
            staged[1] = None
        except OSError as error:
            try:
                if before is None:
                    items_path.unlink()
                else:
                    back = _stage(folder, ITEMS_FILE, before)
                    staged.append(back)
                    os.replace(back, items_path)
                    staged[-1] = None
            except OSError as again:
                raise GraphError(f"{LANE_TIME_FILE} could not be written ({error}) and {ITEMS_FILE} could not be put "
                                 f"back ({again}): readers must compare items_sha256") from again
            raise
        try:
            _sync_directory(folder)
        except OSError as error:                  # both renames are done and the pair is consistent; only their durability is unknown
            print(f"[radar] graph export: the directory was not synced ({error}); the pair is in place")
    finally:
        for path in staged:
            if path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(path)
    return [items_path, lane_path]


def write_graph(config: Config, ctx: dict[str, Any], root: Path = ROOT, source: Path = ROOT) -> list[Path]:
    """Append the day to `items.json` (filling earlier days from their pages) and rewrite `lane-time.json`.

    Existing files are read from `source`, the result written under `root`. The run log is checked first
    (`check_history`), then the pair before anything is written; when the second file cannot be replaced the first is
    put back. On any problem nothing is published and the reason is printed: these files never stop a run."""
    folder_in, folder_out = source / "radar" / "graph", root / "radar" / "graph"
    try:
        check_history(ctx["history"])
        doc = load_items(folder_in / ITEMS_FILE) or empty_items()
        validate_items(doc, config)
        day = ctx["date"]
        doc = append_day(doc, [item_entry(item, day) for item in ctx["picked"]], day)
        doc = fill_missing(doc, ctx["history"], source / "radar" / "daily", config, skip_day=day)
        history = ctx["history"]
        as_of = max(day, doc.get("as_of") or day)               # re-running an older day does not move the date back
        doc["as_of"], doc["first_run"] = as_of, min(run["date"] for run in history)
        validate_items(doc, config)                             # what is about to be published, picks of the day included
        rules = load_rules(source / "data" / RULES_FILE)
        table = lane_time(config, history, doc, rules, as_of)
        items_bytes = render_json(doc)
        table["items_sha256"], table["items_count"] = hashlib.sha256(items_bytes).hexdigest(), len(doc["items"])
        problems = contract_problems(doc, table, config)
        if problems:
            raise GraphError("the pair is not sound: " + "; ".join(problems[:3]))
        return publish_pair(folder_out, items_bytes, render_json(table))
    except (GraphError, KeyError, ValueError, TypeError, AttributeError, OSError) as error:
        print(f"[radar] graph export skipped: {error}")
        return []
