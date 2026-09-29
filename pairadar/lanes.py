"""Which published items were filed on thin evidence.

`docs/METHODOLOGY.md` section 9 states the known limit plainly: keyword classification
will misfile interdisciplinary work, and corrections belong in `data/taxonomy.json`.
This is the deterministic half of acting on that -- it names the items whose lane was
decided by almost nothing, so a reviewer, or a later agent loop, starts from a short
list instead of reading the whole day.

Two signals, both readable off the existing scoring:

- **thin**: one keyword or none decided the lane, so the lane is close to a fallback;
- **ambiguous**: the runner-up lane scored within a margin of the winner, so the item
  sits between two lanes and the tie-break picked one.

    python3 -m pairadar.lanes                       # report on the published picks
    python3 -m pairadar.lanes --fail-on-suspicious  # exit 1 when the list is non-empty
    python3 -m pairadar.lanes --labels labels.json  # agreement with hand-labeled lanes

The second form is for automation that should only act when there is something to act
on. Nothing here changes a classification: the taxonomy is edited by a human, or by a
reviewed pull request.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import ROOT, Config, Item, load_config, load_json
from .distill import lane_hits

LATEST_PATH = ROOT / "radar" / "latest.json"
MARGIN = 0.35


def ranked_lanes(text: str, config: Config) -> list[tuple[str, int, float]]:
    """Every lane scored against the text, best first: (lane_id, hits, weighted score).

    Ties are broken as `classify` breaks them, so the first entry is the lane it chose.
    """
    scored = [
        (lane["id"], hits, hits * float(lane.get("weight", 1.0)))
        for lane in config.lanes
        for hits in (lane_hits(text, lane),)
    ]
    return sorted(scored, key=lambda entry: (-entry[2], not (entry[1] > 0 and entry[0] != "foundation")))


def flag_reasons(best: tuple[str, int, float], runner_up: tuple[str, int, float],
                 margin: float = MARGIN) -> list[str]:
    """Why a ranking is weak evidence for its winner: "thin", "ambiguous", both or neither.

    An empty list means the classifier is confident; a non-empty one is its only
    abstention signal.
    """
    reasons = []
    if best[1] <= 1:
        reasons.append("thin")
    if best[2] - runner_up[2] <= margin and runner_up[1] > 0:
        reasons.append("ambiguous")
    return reasons


def inspect(item: Item, config: Config, margin: float = MARGIN) -> dict[str, Any] | None:
    """Return a finding when the item's lane rests on thin or ambiguous evidence."""
    ranking = ranked_lanes(f"{item.title}. {item.summary}", config)
    best, runner_up = ranking[0], ranking[1]
    reasons = flag_reasons(best, runner_up, margin)
    if not reasons:
        return None
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "lane": item.lane,
        "reasons": reasons,
        "best": {"lane": best[0], "hits": best[1], "score": round(best[2], 3)},
        "runner_up": {"lane": runner_up[0], "hits": runner_up[1], "score": round(runner_up[2], 3)},
    }


def suspicious(items: list[Item], config: Config, margin: float = MARGIN) -> list[dict[str, Any]]:
    findings = [inspect(item, config, margin) for item in items]
    return [finding for finding in findings if finding]


def published_picks(path: Path = LATEST_PATH) -> list[Item]:
    if not path.exists():
        return []
    return [Item.from_dict(entry) for entry in load_json(path).get("picked", [])]


def report(items: list[Item], config: Config, margin: float = MARGIN) -> dict[str, Any]:
    return {
        "checked": len(items),
        "margin": margin,
        "with_summaries": sum(1 for item in items if item.summary),
        "suspicious": suspicious(items, config, margin),
    }


class LabelError(ValueError):
    """A label file that cannot be scored; the message is shown to the user as is."""


def load_labels(path: Path, lane_ids: list[str], shown: str | None = None) -> list[dict[str, Any]]:
    """Read and validate a whole label file before anything is scored.

    The file is {"items": [{"id"?, "title", "summary"?, "lane"}]}; it is only read.
    Entries are numbered from 1 in messages. `shown` is the path as the user typed it.
    """
    shown = str(path) if shown is None else shown
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LabelError(f"cannot read {shown}: {exc.strerror or exc.__class__.__name__}") from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise LabelError(f"{shown} is not UTF-8 text") from exc
    except json.JSONDecodeError as exc:
        raise LabelError(f"{shown} is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise LabelError(f'{shown}: expected a JSON object with an "items" list')
    known = set(lane_ids)
    entries: list[dict[str, Any]] = []
    for number, entry in enumerate(items, start=1):
        where = f"item {number}"
        if not isinstance(entry, dict):
            raise LabelError(f"{where} is not an object")
        for key in ("title", "lane"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise LabelError(f"{where}: {key} must be a non-empty string")
        for key in ("id", "summary"):
            if key in entry and not isinstance(entry[key], str):
                raise LabelError(f"{where}: {key} must be a string")
        if entry["lane"] not in known:
            raise LabelError(f"{where}: unknown lane {json.dumps(entry['lane'], ensure_ascii=False)}; "
                             f"valid lanes: {', '.join(lane_ids)}")
        entries.append({"id": entry.get("id"), "title": entry["title"],
                        "summary": entry.get("summary", ""), "lane": entry["lane"]})
    return entries


def _lane_score(entry: tuple[str, int, float]) -> dict[str, Any]:
    return {"lane": entry[0], "hits": entry[1], "score": round(entry[2], 3)}


def score_labels(entries: list[dict[str, Any]], config: Config, margin: float = MARGIN) -> dict[str, Any]:
    """Agreement of the classifier with human labels, split by its own abstention signal.

    "confident" entries carry no thin/ambiguous flag, "flagged" ones do. A misfile with
    reasons [] is one the flags failed to catch.
    """
    tally = {group: {"n": 0, "agree": 0} for group in ("all", "confident", "flagged")}
    disagreements: list[dict[str, Any]] = []
    for entry in entries:
        ranking = ranked_lanes(f"{entry['title']}. {entry['summary']}", config)
        best, runner_up = ranking[0], ranking[1]
        reasons = flag_reasons(best, runner_up, margin)
        agree = best[0] == entry["lane"]
        for group in ("all", "flagged" if reasons else "confident"):
            tally[group]["n"] += 1
            tally[group]["agree"] += int(agree)
        if not agree:
            disagreements.append({
                "id": entry["id"],
                "title": entry["title"],
                "label": entry["lane"],
                "predicted": best[0],
                "reasons": reasons,
                "best": _lane_score(best),
                "runner_up": _lane_score(runner_up),
            })
    agreement = {
        group: {"n": counts["n"], "agree": counts["agree"],
                "rate": round(counts["agree"] / counts["n"], 3) if counts["n"] else None}
        for group, counts in tally.items()
    }
    return {"labeled": len(entries), "margin": margin, "agreement": agreement,
            "disagreements": disagreements}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", type=Path, default=None,
                        help="published snapshot to check (default: radar/latest.json)")
    parser.add_argument("--margin", type=float, default=MARGIN,
                        help="score gap below which the runner-up lane counts as ambiguous")
    parser.add_argument("--fail-on-suspicious", action="store_true",
                        help="exit 1 when any item is flagged")
    parser.add_argument("--labels", metavar="PATH",
                        help="score the classifier against a hand-labeled JSON file "
                             "({\"items\": [{\"id\", \"title\", \"summary\", \"lane\"}]}); "
                             "combines only with --margin, exits 0 whenever a report is printed")
    args = parser.parse_args(argv)
    if args.labels is not None:
        for flag, given in (("--fail-on-suspicious", args.fail_on_suspicious),
                            ("--latest", args.latest is not None)):
            if given:
                print(f"lanes: --labels cannot be combined with {flag}", file=sys.stderr)
                return 2
        config = load_config()
        try:
            entries = load_labels(Path(args.labels), [lane["id"] for lane in config.lanes], args.labels)
        except LabelError as exc:
            print(f"lanes: {exc}", file=sys.stderr)
            return 2
        scored = {"labels": Path(args.labels).name, **score_labels(entries, config, args.margin)}
        print(json.dumps(scored, ensure_ascii=False, indent=2))
        return 0
    latest = args.latest if args.latest is not None else LATEST_PATH
    verdict = report(published_picks(latest), load_config(), args.margin)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    if verdict["suspicious"] and args.fail_on_suspicious:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
