"""Render layer: trilingual daily pages, README injection and the archive index.

Translation policy (documented in docs/METHODOLOGY.md):
- source titles stay in their original language (usually English) so they remain
  searchable and citable;
- the analytical layer — lane, evidence tag, key numbers, "why it matters" — is
  authored per language from templates and a curated glossary, never machine-translated;
- the English abstract excerpt is labelled as a source excerpt, not as a translation.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .config import (
    DAILY_DIR,
    LANGS,
    MARKER_END,
    MARKER_START,
    README_FILES,
    ROOT,
    Config,
    Item,
    dump_json,
)
from .distill import one_liner

EVIDENCE_LABEL = {"O": "`[O]`", "R": "`[R]`", "M": "`[M]`"}
LANG_SWITCH = {
    "zh": "[English](README.en.md) · [日本語](README.ja.md)",
    "en": "[中文](README.md) · [日本語](README.ja.md)",
    "ja": "[中文](README.md) · [English](README.en.md)",
}
DAILY_SWITCH = {
    "zh": "语言：中文 · [EN](./{stem}.en.md) · [JA](./{stem}.ja.md)",
    "en": "Language: [ZH](./{stem}.zh.md) · EN · [JA](./{stem}.ja.md)",
    "ja": "言語：[ZH](./{stem}.zh.md) · [EN](./{stem}.en.md) · 日本語",
}


def _why_text(item: Item, config: Config, lang: str, curated: dict[str, dict[str, str]]) -> str:
    curated_why = curated.get(item.id, {})
    if curated_why.get(lang):
        return curated_why[lang]
    return config.glossary["why_templates"].get(item.lane, {}).get(lang, "")


def item_block(
    item: Item,
    config: Config,
    lang: str,
    curated: dict[str, dict[str, str]],
    index: int,
) -> list[str]:
    ui = config.ui(lang)
    lines = [
        f"### {index}. {item.title}",
        "",
        f"- **{ui['lane']}**: {config.lane_name(item.lane, lang)} ｜ "
        f"**{ui['evidence']}**: {EVIDENCE_LABEL.get(item.evidence, '`[M]`')} ｜ "
        f"**{ui['source']}**: [{item.publisher or item.source_id}]({item.url}) ｜ `{item.published}`",
    ]
    if item.numbers:
        lines.append(f"- **{ui['numbers']}**: " + " · ".join(f"`{value}`" for value in item.numbers))
    if item.signals:
        tags = [ui["signals"].get(signal, signal) for signal in item.signals]
        lines.append("- " + " / ".join(tags))
    why = _why_text(item, config, lang, curated)
    if why:
        lines.append(f"- **{ui['why']}**: {why}")
    excerpt = one_liner(item.summary)
    if excerpt and item.source_id != "baseline":
        lines.append("")
        lines.append(f"> {excerpt}")
    lines.append("")
    return lines


def _lane_rows_named(config: Config, lane_rows: Iterable[tuple[str, int]], lang: str) -> list[tuple[str, int]]:
    return [(config.lane_name(lane_id, lang), count) for lane_id, count in lane_rows]


def render_daily(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    ui = config.ui(lang)
    stem = ctx["date"]
    lines = [
        f"# {ui['title']} · {stem}",
        "",
        DAILY_SWITCH[lang].format(stem=stem),
        "",
        f"> {ui['tagline']}",
        "",
        f"- {ui['generated']}: `{ctx['generated']}`",
        f"- {ui['window']}: `{ctx['window']}`",
        f"- {ui['evidence_legend']}",
        "",
        f"![lane distribution](../../assets/lane-distribution.svg)",
        "",
        f"## {ui['top_items']}",
        "",
    ]
    if ctx["picked"]:
        for position, item in enumerate(ctx["picked"], start=1):
            lines.extend(item_block(item, config, lang, ctx["curated"], position))
    else:
        lines.extend([ui["no_items"], ""])

    lines.extend([f"## {ui['baseline']}", ""])
    for position, item in enumerate(ctx["baseline"], start=1):
        lines.extend(item_block(item, config, lang, ctx["curated"], position))

    lines.extend(
        [
            f"## {ui['lane_distribution']}",
            "",
            f"| {ui['lane']} | # |",
            "| --- | --- |",
        ]
    )
    for name, count in _lane_rows_named(config, ctx["lane_rows"], lang):
        lines.append(f"| {name} | {count} |")
    lines.extend(
        [
            "",
            f"## {ui['trend']}",
            "",
            "![cadence](../../assets/cadence.svg)",
            "",
            "![evidence mix](../../assets/evidence-mix.svg)",
            "",
            f"## {ui['watchlist']}",
            "",
        ]
    )
    for watch in config.sources.get("standards_watch", []):
        lines.append(f"- [{watch['title']}]({watch['url']}) — {watch['status_note']}")
    lines.extend(
        [
            "",
            "---",
            "",
            f"*{ui['disclaimer']}*",
            "",
            f"[{ui['methodology']}](../../docs/METHODOLOGY.md) · [{ui['history']}](../INDEX.md)",
            "",
        ]
    )
    return "\n".join(lines)


def readme_block(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    ui = config.ui(lang)
    stem = ctx["date"]
    lines = [
        f"### {ui['today']} · {stem}",
        "",
        f"`{ui['generated']}: {ctx['generated']}` ｜ `{ui['window']}: {ctx['window']}`",
        "",
    ]
    highlights = (ctx["picked"] or ctx["baseline"])[:5]
    for item in highlights:
        why = _why_text(item, config, lang, ctx["curated"])
        numbers = f" ｜ {' · '.join(f'`{n}`' for n in item.numbers[:2])}" if item.numbers else ""
        lines.append(
            f"- {EVIDENCE_LABEL.get(item.evidence, '`[M]`')} **[{item.title}]({item.url})** — "
            f"{config.lane_name(item.lane, lang)}{numbers}"
        )
        if why:
            lines.append(f"  - {why}")
    lines.extend(
        [
            "",
            f"[{ui['today']} ›](radar/daily/{stem}.{lang}.md) · [{ui['history']} ›](radar/INDEX.md)",
            "",
            f"![lane distribution](assets/lane-distribution.svg)",
            "",
            f"![cadence](assets/cadence.svg)",
            "",
        ]
    )
    return "\n".join(lines)


def inject_readme(config: Config, lang: str, ctx: dict[str, Any]) -> Path:
    path = ROOT / README_FILES[lang]
    text = path.read_text(encoding="utf-8")
    block = readme_block(config, lang, ctx)
    if MARKER_START not in text or MARKER_END not in text:
        raise SystemExit(f"{path.name} is missing the RADAR markers")
    head, _, rest = text.partition(MARKER_START)
    _, _, tail = rest.partition(MARKER_END)
    path.write_text(f"{head}{MARKER_START}\n{block}{MARKER_END}{tail}", encoding="utf-8")
    return path


def write_daily(config: Config, ctx: dict[str, Any]) -> list[Path]:
    written: list[Path] = []
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    for lang in LANGS:
        path = DAILY_DIR / f"{ctx['date']}.{lang}.md"
        path.write_text(render_daily(config, lang, ctx), encoding="utf-8")
        written.append(path)
    return written


def update_index(config: Config, ctx: dict[str, Any], history: list[dict[str, Any]]) -> Path:
    path = ROOT / "radar" / "INDEX.md"
    lines = [
        "# Archive · 历史归档 · アーカイブ",
        "",
        "| Date | Items | Lanes touched | ZH | EN | JA |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(history, key=lambda item: item["date"], reverse=True)[:120]:
        stem = entry["date"]
        lines.append(
            f"| {stem} | {entry['count']} | {entry.get('lanes', 0)} | "
            f"[zh](daily/{stem}.zh.md) | [en](daily/{stem}.en.md) | [ja](daily/{stem}.ja.md) |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_latest(ctx: dict[str, Any]) -> Path:
    path = ROOT / "radar" / "latest.json"
    dump_json(
        path,
        {
            "date": ctx["date"],
            "generated": ctx["generated"],
            "window": ctx["window"],
            "picked": [item.to_dict() for item in ctx["picked"]],
            "lane_counts": dict(ctx["lane_rows"]),
            "evidence_mix": ctx["mix"],
            "fetch": ctx.get("fetch", {}),
            "baseline_count": len(ctx["baseline_all"]),
        },
    )
    return path
