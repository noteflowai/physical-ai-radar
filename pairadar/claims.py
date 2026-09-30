"""Claim-audit worksheet: recently published figures, listed for review against their sources.

Run as python3 -m pairadar.claims. It reads the published store, radar/feed.json, and
prints a Markdown sheet for the RA-01 review of recent numeric claims. Every figure the
pipeline published in the window gets one row with its day, evidence tag, source link and
a status that says only where the figure can be found in what the store kept:

- quoted: in the stored excerpt; the context is the clause number_context quotes.
- title: only in the stored title; the context is quoted from the title.
- no-excerpt: the entry has no excerpt (baseline entries never have one).
- unlocated: in neither. The excerpt is cut to one or two sentences while figures are
  extracted from the full summary, so this means "check the original source first",
  not "unsupported".

No status infers a metric, unit or population, or says a claim is correct. The tool is
offline and read-only: it makes no network call and writes no file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import ROOT, md_text, md_url
from .distill import number_context
from .feeds import FEED_JSON, JSON_FEED_VERSION, within

STORE_PATH = ROOT / "radar" / FEED_JSON
MAX_DAYS = 30
STATUSES = ("quoted", "title", "no-excerpt", "unlocated")
COLUMNS = ("#", "day", "evidence", "claim", "status", "context", "source", "review")
EMPTY = "No numeric claims in this window."
NOTE = ("A status says only whether a figure is found in the stored title or truncated excerpt. "
        "Unlocated means check the original source first, not unsupported; no status verifies "
        "a date, unit, population or result. Record what you checked in the review column.")


class StoreError(ValueError):
    """The store cannot be audited; the message names the problem."""


def valid_day(value: Any) -> bool:
    """A canonical ISO date string. Basic (20260930) and week (2026-W40-3) forms parse
    on newer Pythons but compare wrongly as strings, so they do not count."""
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def read_store(path: Path) -> tuple[list[dict[str, Any]], int]:
    """The dated _radar entries of a feed.json, in stored order, and how many items
    were skipped (not an object, no _radar dict, or no canonical date)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StoreError(f"store not found: {path} (pass --store PATH to a feed.json "
                         "written by the pipeline)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreError(f"cannot read {path}: {exc}") from None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StoreError(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise StoreError(f"not a JSON Feed store: {path} (expected an object with version "
                         f"{JSON_FEED_VERSION}, found a top-level {type(payload).__name__})")
    version = payload.get("version")
    if version != JSON_FEED_VERSION:
        raise StoreError(f"unexpected JSON Feed version in {path}: expected {JSON_FEED_VERSION}, "
                         f"found {json.dumps(version, ensure_ascii=False)}")
    items = payload.get("items")
    if not isinstance(items, list):
        found = "no items" if "items" not in payload else type(items).__name__
        raise StoreError(f"items is not a list in {path} (found {found})")
    entries: list[dict[str, Any]] = []
    skipped = 0
    for element in items:
        radar = element.get("_radar") if isinstance(element, dict) else None
        if isinstance(radar, dict) and valid_day(radar.get("date")):
            entries.append(radar)
        else:
            skipped += 1
    if not entries:
        raise StoreError(f"no dated radar entries in {path}")
    return entries, skipped


def _text(value: Any) -> str:
    """A stored text field, or "" when a hand edit left something else there."""
    return value if isinstance(value, str) else ""


def locate(claim: str, title: str, excerpt: str) -> tuple[str, str]:
    """(status, context) for one figure against the stored title and excerpt."""
    context = number_context(claim, excerpt) if excerpt else ""
    if context:
        return "quoted", context
    context = number_context(claim, title) if title else ""
    if context:
        return "title", context
    if not excerpt:
        return "no-excerpt", ""
    return "unlocated", ""


def _dated(entries: Iterable[Any]) -> list[dict[str, Any]]:
    return [entry for entry in entries if isinstance(entry, dict) and valid_day(entry.get("date"))]


def reference_day(entries: Iterable[Any]) -> str | None:
    """The newest canonical entry date: the sheet never depends on the wall clock."""
    days = [entry["date"] for entry in _dated(entries)]
    return max(days) if days else None


def audit(entries: Iterable[Any], days: int) -> list[dict[str, Any]]:
    """Every figure published in the window ending on the newest entry date.

    Rows are newest day first, then stored entry order, then figure order, and keep
    the raw stored values. Nothing is truncated here.
    """
    dated = _dated(entries)
    if not dated:
        return []
    reference = max(entry["date"] for entry in dated)
    window = sorted(within(dated, reference, days), key=lambda entry: entry["date"], reverse=True)
    rows: list[dict[str, Any]] = []
    for entry in window:
        title, excerpt = _text(entry.get("title")), _text(entry.get("excerpt"))
        numbers = entry.get("numbers")
        for claim in numbers if isinstance(numbers, list) else []:
            if not isinstance(claim, str) or not claim.strip():
                continue
            status, context = locate(claim, title, excerpt)
            rows.append({
                "day": entry["date"],
                "evidence": _text(entry.get("evidence")),
                "id": _text(entry.get("id")),
                "title": title,
                "url": _text(entry.get("url")),
                "published": _text(entry.get("published")),
                "claim": claim,
                "status": status,
                "context": context,
            })
    return rows


def _flat(text: str) -> str:
    """Keep a value inside its table cell: escape pipes, fold line breaks."""
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def cell(text: str) -> str:
    return _flat(md_text(text))


def source_cell(title: str, url: str) -> str:
    return f"[{cell(title)}]({_flat(md_url(url))})"


def _count(rows: Iterable[dict[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row["status"] == status)


def render(rows: list[dict[str, Any]], *, store: Path, reference: str | None, days: int,
           limit: int, skipped: int) -> str:
    """The Markdown sheet: header, whole-window counts, truncation notice and table."""
    total = len(rows)
    shown = rows[:limit]
    lines = [
        "# Claim audit worksheet (RA-01)",
        "",
        f"Store: {cell(str(store))}; reference day {reference or 'none'}; days {days}; "
        f"figures in window {total}; rows shown {len(shown)}; skipped entries {skipped}",
        "",
    ]
    if not rows:
        lines.extend([EMPTY, ""])
        return "\n".join(lines)
    lines.extend(["Status counts (whole window): "
                  + "; ".join(f"{status} {_count(rows, status)}" for status in STATUSES), ""])
    if len(shown) < total:
        hidden = rows[len(shown):]
        lines.extend([
            f"Showing {len(shown)} of {total} figures; {len(hidden)} not shown "
            f"({_count(hidden, 'unlocated')} unlocated, {_count(hidden, 'no-excerpt')} no-excerpt "
            f"among them). Rerun with --limit {total} to list all.",
            "",
        ])
    lines.extend([NOTE, "", "| " + " | ".join(COLUMNS) + " |",
                  "|" + "|".join("---" for _ in COLUMNS) + "|"])
    for number, row in enumerate(shown, 1):
        values = [str(number), row["day"], cell(row["evidence"]), cell(row["claim"]), row["status"],
                  cell(row["context"]), source_cell(row["title"], row["url"]), ""]
        lines.append("| " + " | ".join(values) + " |")
    lines.append("")
    return "\n".join(lines)


def _bounded(low: int, high: int | None = None) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
        if value < low or (high is not None and value > high):
            span = f"from {low} to {high}" if high is not None else f"of at least {low}"
            raise argparse.ArgumentTypeError(f"expected an integer {span}, got {value}")
        return value
    return parse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m pairadar.claims",
        description="Print a Markdown worksheet of recently published numeric claims for review "
                    "against their original sources. Offline and read-only.")
    parser.add_argument("--store", type=Path, default=STORE_PATH, metavar="PATH",
                        help="published feed.json to audit (default: radar/feed.json)")
    parser.add_argument("--days", type=_bounded(1, MAX_DAYS), default=7, metavar="N",
                        help=f"days before the newest entry date to include, 1 to {MAX_DAYS} (default: 7)")
    parser.add_argument("--limit", type=_bounded(1), default=30, metavar="N",
                        help="table rows to show, at least 1; totals always count the whole window "
                             "(default: 30)")
    args = parser.parse_args(argv)
    try:
        entries, skipped = read_store(args.store)
    except StoreError as exc:
        print(f"claims: {exc}", file=sys.stderr)
        return 2
    rows = audit(entries, args.days)
    sys.stdout.write(render(rows, store=args.store, reference=reference_day(entries), days=args.days,
                            limit=args.limit, skipped=skipped))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
