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
blind   copies the published page and the draft as A/B (random, per language) into
        DIR/D/blind/ and keeps the key in DIR/D/blind-key.json (0600) for the reviewers'
        organiser only.

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


def cmd_blind(args) -> int:
    base = day_dir(args.out, args.date)
    key = {}
    for lang in LANGS:
        draft, page = base / f"new.{lang}.md", ROOT / "radar" / "daily" / f"{args.date}.{lang}.md"
        if not (draft.is_file() and page.is_file()):
            raise SystemExit(f"missing draft or page for {lang}")
        new_is_a = secrets.randbelow(2) == 0
        key[lang] = {"A": "new" if new_is_a else "old", "B": "old" if new_is_a else "new"}
        a, b = (draft, page) if new_is_a else (page, draft)
        write_private(base / "blind" / f"A.{lang}.md", a.read_text(encoding="utf-8"))
        write_private(base / "blind" / f"B.{lang}.md", b.read_text(encoding="utf-8"))
    write_private(base / "blind-key.json", json.dumps(key, indent=1))
    print(str(base / "blind"))
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
    args = parser.parse_args(argv)
    return {"packet": cmd_packet, "check": cmd_check, "blind": cmd_blind}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
