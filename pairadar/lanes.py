"""Which published items were filed on thin evidence.

`docs/METHODOLOGY.md` section 8 states the known limit plainly: keyword classification
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

The second form is for automation that should only act when there is something to act
on. Nothing here changes a classification: the taxonomy is edited by a human, or by a
reviewed pull request.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import ROOT, Config, Item, load_config, load_json
from .distill import lane_hits

LATEST_PATH = ROOT / "radar" / "latest.json"
MARGIN = 0.35


def ranked_lanes(text: str, config: Config) -> list[tuple[str, int, float]]:
    """Every lane scored against the text, best first: (lane_id, hits, weighted score)."""
    scored = [
        (lane["id"], hits, hits * float(lane.get("weight", 1.0)))
        for lane in config.lanes
        for hits in (lane_hits(text, lane),)
    ]
    return sorted(scored, key=lambda entry: (-entry[2], entry[0]))


def inspect(item: Item, config: Config, margin: float = MARGIN) -> dict[str, Any] | None:
    """Return a finding when the item's lane rests on thin or ambiguous evidence."""
    ranking = ranked_lanes(f"{item.title}. {item.summary}", config)
    best, runner_up = ranking[0], ranking[1]
    reasons = []
    if best[1] <= 1:
        reasons.append("thin")
    if best[2] - runner_up[2] <= margin and runner_up[1] > 0:
        reasons.append("ambiguous")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--latest", type=Path, default=LATEST_PATH)
    parser.add_argument("--margin", type=float, default=MARGIN,
                        help="score gap below which the runner-up lane counts as ambiguous")
    parser.add_argument("--fail-on-suspicious", action="store_true",
                        help="exit 1 when any item is flagged")
    args = parser.parse_args(argv)
    verdict = report(published_picks(args.latest), load_config(), args.margin)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    if verdict["suspicious"] and args.fail_on_suspicious:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
