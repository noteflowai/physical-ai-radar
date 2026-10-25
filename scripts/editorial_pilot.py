#!/usr/bin/env python3
"""Side-by-side pilot for deep reads (docs/editorial-style.md). Never touches the live site.

    editorial_pilot.py packet --date D --out DIR [--deep ARXIV_ID ...]   network: arxiv.org/html only
    editorial_pilot.py check  --date D --out DIR                          offline
    editorial_pilot.py blind  --date D --out DIR                          offline

packet  reads the day's picks from radar/latest.json, chooses three deep-read candidates ([R] arXiv items in different lanes with
        an HTML rendering, highest score first, unless --deep names them), saves each full
        text as DIR/D/fulltext/<id>.json and .txt, and writes DIR/D/brief.md: the writer's
        brief with the style rules, the three evidence packets and the five index items.
check   runs pairadar.editorial on DIR/D/new.<lang>.md (the writer's draft) and measures
        the published page radar/daily/D.<lang>.md with the checks that apply to it
        (stock phrases, template rate of the "why it matters" lines); DIR/D/check.json.
blind   renders the day's three deep reads twice into ONE neutral template (date header, then title,
        source line and body per item; no pilot marker, no lane/signal fields, no index): the
        writer's labelled paragraphs against the model-drafted notes of data/notes/D.json for
        the same items. Random A/B per language into DIR/D/blind/, one common mtime, key in
        ~/.local/state/pairadar/pilot-keys/D.json (0600, directory 0700), never next to the pair.
        Refuses a day without a model-drafted notes file (they arrive with that day's nightly).

DIR must be outside this repository; files are written 0600 in 0700 directories.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import editorial, fulltext  # noqa: E402

LANGS = editorial.LANGS
DEEP, INDEX = 3, 5
_WHY = {"zh": "为什么重要", "en": "Why it matters", "ja": "重要な理由"}


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> None:
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def day_dir(out: Path, day: str) -> Path:
    out = out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise SystemExit("pilot output must be outside the repository")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise SystemExit("--date must be YYYY-MM-DD")
    return out / day


def picks(day: str, latest: Path) -> list[dict]:
    data = json.loads(latest.read_text(encoding="utf-8"))
    if data.get("date") != day:
        raise SystemExit(f"{latest} is for {data.get('date')}, not {day}")
    return data["picked"]


def arxiv_id(item: dict) -> str | None:
    match = re.fullmatch(r"arxiv:(\d{4}\.\d{4,5})", item.get("id", ""))
    return match.group(1) if match else None


def choose(items: list[dict], named: list[str], get) -> tuple[list[tuple[dict, fulltext.Document]], list[dict]]:
    deep, lanes = [], set()
    by_id = {arxiv_id(item): item for item in items if arxiv_id(item)}
    order = [by_id[i] for i in named if i in by_id] if named else sorted(
        (item for item in items if item.get("evidence") == "R" and arxiv_id(item)),
        key=lambda item: -float(item.get("score", 0)))
    for item in order:
        if len(deep) == DEEP or (not named and item.get("lane") in lanes):
            continue
        document = fulltext.fetch_document(arxiv_id(item), get=get)
        if document is None:
            print(f"no HTML full text: {item['id']}", file=sys.stderr)
            continue
        deep.append((item, document))
        lanes.add(item.get("lane"))
    chosen = {item["id"] for item, _ in deep}
    return deep, [item for item in items if item["id"] not in chosen][:INDEX]


def brief(day: str, deep: list[tuple[dict, fulltext.Document]], index: list[dict]) -> str:
    style = (ROOT / "docs" / "editorial-style.md").read_text(encoding="utf-8")
    parts = [f"# Deep-read brief · {day}", "",
             "Write one Markdown file per language (new.zh.md, new.en.md, new.ja.md), each written on its own,",
             "following the style below exactly. Use only the evidence in this brief; every number must appear",
             "in the full text with the locator you give it.", "", "---", style, "---", "",
             "## Deep-read evidence", ""]
    for n, (item, document) in enumerate(deep, 1):
        parts += [f"### Deep {n}: {item['title']}", f"- url: {item['url']}", f"- lane: {item.get('lane')}",
                  f"- abstract: {item.get('summary', '')}", "", "```text", document.as_text().strip(), "```", ""]
    parts += ["## Index items (one line each)", ""]
    for item in index:
        parts.append(f"- {item['title']} | {item['url']} | [{item.get('evidence')}] | {item.get('summary', '')[:600]}")
    return "\n".join(parts) + "\n"


def cmd_packet(args) -> int:
    from pairadar import fetch
    base = day_dir(args.out, args.date)
    deep, index = choose(picks(args.date, args.latest), args.deep, fetch.http_get)
    for item, document in deep:
        write_private(base / "fulltext" / f"{document.arxiv_id}.json",
                      json.dumps(document.as_dict(), ensure_ascii=False, indent=1))
        write_private(base / "fulltext" / f"{document.arxiv_id}.txt", document.as_text())
    write_private(base / "brief.md", brief(args.date, deep, index))
    print(json.dumps({"date": args.date, "deep": [item["id"] for item, _ in deep],
                      "index": [item["id"] for item in index], "brief": str(base / "brief.md")}, indent=1))
    return 0 if len(deep) == DEEP else 1


def old_metrics(page: str, lang: str, style: dict) -> dict:
    """The applicable checks on the published page's "why it matters" lines, today's picks only.

    The curated frontier-baseline section that follows the picks is written by hand and
    is not part of the comparison."""
    marker = f"**{_WHY[lang]}**"
    sections = re.split(r"(?m)^## ", page)
    picks_section = sections[1] if len(sections) > 1 else page
    lines = [line.split(":", 1)[-1].split("：", 1)[-1].strip() for line in picks_section.splitlines()
             if marker in line]
    rate, _ = editorial.template_rate(lines, lang)
    banned = sum(len(editorial.banned_hits(line, lang, style)) for line in lines)
    return {"items": len(lines), "banned_total": banned, "template_rate": round(rate, 3),
            "triples": sum(len(editorial.triples(line, lang, style)) for line in lines)}


def cmd_check(args) -> int:
    base = day_dir(args.out, args.date)
    style = editorial.load_style()
    result = {"date": args.date, "new": {}, "old": {}}
    for lang in LANGS:
        draft = base / f"new.{lang}.md"
        if draft.is_file():
            result["new"][lang] = editorial.check(draft.read_text(encoding="utf-8"), lang, style)
        page = ROOT / "radar" / "daily" / f"{args.date}.{lang}.md"
        if page.is_file():
            result["old"][lang] = old_metrics(page.read_text(encoding="utf-8"), lang, style)
    write_private(base / "check.json", json.dumps(result, ensure_ascii=False, indent=1))
    summary = {lang: {"passed": report["passed"], "banned": report["banned_total"],
                      "template_rate": report["template_rate"],
                      "findings": sum(len(d["findings"]) for d in report["deep"]) + len(report["day"])}
               for lang, report in result["new"].items()}
    print(json.dumps({"new": summary, "old": result["old"]}, ensure_ascii=False, indent=1))
    return 0 if summary and all(v["passed"] for v in summary.values()) else 1


def load_model_notes(path: Path, day: str) -> dict:
    """The day's model-drafted "why it matters" notes, keyed by item URL.

    Refuses anything but a model draft for that day: a pilot day without it would compare the new
    text against the rule-based fallback template (pilot day 1: reviewers called that baseline too low)."""
    if not path.is_file():
        raise SystemExit(f"no notes file {path}: the model-drafted notes arrive with the day's nightly run; "
                         "compare after it, not before")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "pairadar-notes-1" or data.get("date") != day:
        raise SystemExit(f"{path} is not a pairadar-notes-1 document for {day}")
    if (data.get("author") or {}).get("kind") != "llm-draft":
        raise SystemExit(f"{path} is not a model draft (author.kind != llm-draft)")
    return data.get("notes") or {}


_SOURCE = {"zh": "来源", "en": "Source", "ja": "出典"}
_SOURCE_LINE = re.compile(r"^(?:Source: |来源：|出典：)\[([^\]]+)\]\((https?://[^)\s]+)\)")


def deep_blocks(draft: str) -> list[dict]:
    """The writer's deep reads: title, link text, URL and body paragraphs of each ### block."""
    section = re.split(r"(?m)^## ", draft)[1:2]
    blocks = []
    for chunk in re.split(r"(?m)^### ", section[0])[1:] if section else []:
        head, _, rest = chunk.partition("\n")
        source = next((m for line in rest.splitlines() if (m := _SOURCE_LINE.match(line))), None)
        if not source:
            raise SystemExit(f"deep read without a source line: {head[:60]}")
        body = "\n".join(line for line in rest.splitlines() if not _SOURCE_LINE.match(line)).strip()
        blocks.append({"title": re.sub(r"^\d+\.\s*", "", head.strip()), "label": source.group(1),
                       "url": source.group(2), "body": body})
    return blocks


def neutral_page(day: str, lang: str, blocks: list[dict], bodies: list[str]) -> str:
    """One neutral template for both sides: date header, then title, source line and body per item."""
    sep = ": " if lang == "en" else "："
    parts = [f"# Physical AI Radar · {day}", ""]
    for n, (block, body) in enumerate(zip(blocks, bodies), 1):
        parts += [f"## {n}. {block['title']}", "",
                  f"{_SOURCE[lang]}{sep}[{block['label']}]({block['url']})", "", body, ""]
    return "\n".join(parts)


def cmd_blind(args) -> int:
    """A/B pair of the day's deep-read items in one neutral template (pilot days 2-3).

    Both sides carry the same date header, titles and source lines; only the body differs:
    the writer's four labelled paragraphs against the model-drafted note the live page would
    carry. No pilot marker, lane/signal fields, navigation or statistics. Both files get the
    same mtime, are named A/B only, and the key goes outside DIR (reviewers read DIR)."""
    base = day_dir(args.out, args.date)
    keys = (args.keys or Path.home() / ".local" / "state" / "pairadar" / "pilot-keys").resolve()
    if keys == base.resolve() or base.resolve() in keys.parents:
        raise SystemExit("the key directory must not be under the reviewers' directory")
    notes = load_model_notes(args.notes or ROOT / "data" / "notes" / f"{args.date}.json", args.date)
    key = {}
    pairs = {}
    for lang in LANGS:
        draft = base / f"new.{lang}.md"
        if not draft.is_file():
            raise SystemExit(f"missing draft for {lang}: {draft}")
        blocks = deep_blocks(draft.read_text(encoding="utf-8"))
        if len(blocks) != DEEP:
            raise SystemExit(f"{lang}: expected {DEEP} deep reads, found {len(blocks)}")
        old_bodies = []
        for block in blocks:
            note = (notes.get(block["url"]) or {}).get(lang)
            if not note:
                raise SystemExit(f"no model-drafted note for {block['url']} ({lang}) in the notes file")
            old_bodies.append(note.strip())
        new_text = neutral_page(args.date, lang, blocks, [block["body"] for block in blocks])
        old_text = neutral_page(args.date, lang, blocks, old_bodies)
        new_is_a = secrets.randbelow(2) == 0
        key[lang] = {"A": "new" if new_is_a else "old", "B": "old" if new_is_a else "new"}
        pairs[lang] = (new_text, old_text) if new_is_a else (old_text, new_text)
    blind = base / "blind"
    for name in list(blind.glob("*")) if blind.is_dir() else []:
        name.unlink()
    for lang, (a, b) in pairs.items():
        write_private(blind / f"A.{lang}.md", a)
        write_private(blind / f"B.{lang}.md", b)
    stamp = int(time.time())   # one mtime for every file in the pair: nothing to tell them apart by
    for path in blind.glob("*"):
        os.utime(path, (stamp, stamp))
    os.utime(blind, (stamp, stamp))
    write_private(private_dir(keys) / f"{args.date}.json", json.dumps(key, indent=1))
    print(str(blind))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("packet", "check", "blind"):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "packet":
            command.add_argument("--deep", nargs="*", default=[], help="arXiv ids to deep-read, in order")
            command.add_argument("--latest", type=Path, default=ROOT / "radar" / "latest.json")
        if name == "blind":
            command.add_argument("--notes", type=Path, default=None,
                                 help="the day's model-drafted notes (default data/notes/D.json)")
            command.add_argument("--keys", type=Path, default=None,
                                 help="private key directory, never under --out "
                                      "(default ~/.local/state/pairadar/pilot-keys)")
    args = parser.parse_args(argv)
    return {"packet": cmd_packet, "check": cmd_check, "blind": cmd_blind}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
