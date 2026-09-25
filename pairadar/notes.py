"""Check a drafted notes file and say, item by item, what to fix.

The nightly job used to find out what was wrong with a draft from the test suite: a
traceback from an unrelated test on 09-22, and on 09-23 three failures because one
line cited `1,600` and `94.5`, which the page did not contain. Either way the night
was lost, although the fix was one sentence. This module gives the same verdict in a
form the drafting agent can act on, so the job hands the problems back and asks for
exactly those fixes.

    python3 -m pairadar.notes check 2026-09-25     # exit 1 and list what must change
    python3 -m pairadar.notes stamp 2026-09-25 --drafter M [--reviewer A --reviewer-model M]

`stamp` records which models actually ran. The agent was asked to name its own model,
and a model's account of itself is not evidence; the job knows what it passed to
`--model`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import LANGS, NOTES_SCHEMA, ROOT
from .distill import url_key


def unsupported_numbers(text: str, source: str) -> list[str]:
    """Numeric claims in a drafted line that the published page does not contain.

    The agent is told never to add a number that is not in the source. This is the
    mechanical half of that rule: every multi-digit figure in a drafted line has to
    appear in the page the line is about. Single digits are ignored -- "one" or a
    lane index carries no claim.
    """
    haystack = source.replace(",", "")
    missing = []
    for token in {match.group(0).strip() for match in re.finditer(r"\d+(?:[.,]\d+)?", text)}:
        digits = token.replace(",", "")
        if len(digits.replace(".", "")) >= 2 and digits not in haystack:
            missing.append(token)
    return sorted(missing)


def notes_path(day: str, root: Path = ROOT) -> Path:
    return root / "data" / "notes" / f"{day}.json"


def day_pages(day: str, root: Path = ROOT) -> str:
    pages = [root / "radar" / "daily" / f"{day}.{lang}.md" for lang in LANGS]
    return "\n".join(page.read_text(encoding="utf-8") for page in pages if page.exists())


def day_picks(day: str, root: Path = ROOT) -> list[dict[str, Any]]:
    """The live picks of `day`, if the snapshot is that day's; baseline entries carry their own lines."""
    path = root / "radar" / "latest.json"
    if not path.exists():
        return []
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("date") != day:
        return []
    return [pick for pick in snapshot.get("picked", []) if pick.get("source_id") != "baseline"]


def check(day: str, root: Path = ROOT) -> tuple[list[str], list[str]]:
    """(problems, advice) for the day's notes. Any problem fails the draft; advice does not."""
    path = notes_path(day, root)
    rel = path.relative_to(root).as_posix()
    if not path.exists():
        return [f"{rel} was not written"], []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{rel} is not valid JSON: {error.msg}, line {error.lineno} column {error.colno}"], []
    if not isinstance(document, dict):
        return [f"{rel} must hold one JSON object"], []
    problems: list[str] = []
    if document.get("schema") != NOTES_SCHEMA:
        problems.append(f'"schema" must be "{NOTES_SCHEMA}"')
    if document.get("date") != day:
        problems.append(f'"date" must be "{day}"')
    notes = document.get("notes")
    if not isinstance(notes, dict) or not notes:
        return problems + ['"notes" must map each item url to its zh, en and ja lines'], []

    picks = day_picks(day, root)
    by_key = {url_key(pick["url"]): pick for pick in picks}
    source = day_pages(day, root)
    for key, note in notes.items():
        if picks and url_key(key) not in by_key:
            problems.append(f"{key}: matches no item picked on {day}; key each note by its item's url")
            continue
        if not isinstance(note, dict):
            problems.append(f"{key}: must be an object with zh, en and ja")
            continue
        missing = [lang for lang in LANGS if not str(note.get(lang) or "").strip()]
        if missing:
            problems.append(f"{key}: no {', '.join(missing)} line; write all three languages or drop the item")
        for lang in LANGS:
            figures = unsupported_numbers(str(note.get(lang) or ""), source) if source else []
            if figures:
                problems.append(f"{key} [{lang}]: cites {', '.join(figures)}, which the page does not contain; "
                                "remove the figure or use one the page shows")
    covered = {url_key(key) for key in notes}
    advice = [f"{pick['url']}: no note for “{pick.get('title', '')}”"
              for key, pick in by_key.items() if key not in covered]
    return problems, advice


def stamp(day: str, drafter: str, reviewer: str = "", reviewer_model: str = "", root: Path = ROOT) -> None:
    """Record the models that ran, overwriting whatever the agent said about itself."""
    path = notes_path(day, root)
    document = json.loads(path.read_text(encoding="utf-8"))
    author = document.get("author") if isinstance(document.get("author"), dict) else {}
    author.update({"kind": "llm-draft", "model": drafter, "cli": "kiro-cli"})
    author.setdefault("agent", "radar-analyst")
    document["author"] = author
    if reviewer:
        document["review"] = {"agent": reviewer, "model": reviewer_model, "verdict": "APPROVE"}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="list what must change; exit 1 when anything must")
    check_parser.add_argument("day")
    stamp_parser = sub.add_parser("stamp", help="record the models that ran")
    stamp_parser.add_argument("day")
    stamp_parser.add_argument("--drafter", required=True)
    stamp_parser.add_argument("--reviewer", default="")
    stamp_parser.add_argument("--reviewer-model", default="")
    args = parser.parse_args(argv)

    if args.command == "stamp":
        stamp(args.day, args.drafter, args.reviewer, args.reviewer_model)
        return 0
    problems, advice = check(args.day)
    for line in problems:
        print(f"- {line}")
    for line in advice:
        print(f"- (optional) {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
