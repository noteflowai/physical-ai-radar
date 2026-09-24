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
from .distill import one_liner, url_key

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


def _why_text(item: Item, config: Config, lang: str, curated: dict[str, dict[str, str]],
              notes: dict[str, Any] | None = None) -> tuple[str, bool]:
    """Return the "why it matters" line and whether it was drafted rather than authored.

    Order: a human-curated line wins, then a drafted note for this exact item, then
    the per-lane template. A drafted line is always reported as drafted so the caller
    can label it -- docs/METHODOLOGY.md section 5 requires that.
    """
    curated_why = curated.get(item.id, {})
    if curated_why.get(lang):
        return curated_why[lang], False
    # Notes are keyed by the link the drafter saw; compare normalised forms so a note
    # written against `http://arxiv.org/abs/IDv1` still finds today's canonical link.
    drafted_notes = {url_key(key): value for key, value in (notes or {}).get("notes", {}).items()}
    drafted = (drafted_notes.get(url_key(item.url), {}) or {}).get(lang, "")
    if drafted:
        return drafted, True
    return config.glossary["why_templates"].get(item.lane, {}).get(lang, ""), False


def item_block(
    item: Item,
    config: Config,
    lang: str,
    curated: dict[str, dict[str, str]],
    index: int,
    ctx_notes: dict[str, Any] | None = None,
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
        lines.append(f"- **{ui['signals_label']}**: " + " · ".join(f"`{tag}`" for tag in tags))
    why, drafted = _why_text(item, config, lang, curated, ctx_notes)
    if why:
        label = f" `{ui['llm_draft']}`" if drafted else ""
        lines.append(f"- **{ui['why']}**{label}: {why}")
    excerpt = one_liner(item.summary)
    if excerpt and item.source_id != "baseline":
        lines.append("")
        lines.append(f"> {excerpt}")
    lines.append("")
    return lines


def window_text(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    """The pool's horizons in the reader's language, or the stored string for a
    snapshot that predates them."""
    detail = ctx.get("window_detail")
    if detail:
        return config.ui(lang)["window_value"].format(**detail)
    return ctx["window"]


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
        f"- {ui['window']}: `{window_text(config, lang, ctx)}`",
        f"- {ui['evidence_legend']}",
        "",
        f"![lane distribution](../../assets/lane-distribution.{lang}.svg)",
        "",
        f"## {ui['top_items']}",
        "",
    ]
    if ctx["picked"]:
        for position, item in enumerate(ctx["picked"], start=1):
            lines.extend(item_block(item, config, lang, ctx["curated"], position, ctx.get("notes")))
    else:
        lines.extend([ui["no_items"], ""])

    lines.extend([f"## {ui['baseline']}", ""])
    for position, item in enumerate(ctx["baseline"], start=1):
        lines.extend(item_block(item, config, lang, ctx["curated"], position, ctx.get("notes")))

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
            f"![cadence](../../assets/cadence.{lang}.svg)",
            "",
            f"![evidence mix](../../assets/evidence-mix.{lang}.svg)",
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
            _draft_notice(config, lang, ctx),
            f"*{ui['disclaimer']}*",
            "",
            f"[{ui['methodology']}](../../docs/METHODOLOGY.md) · [{ui['history']}](../INDEX.md)",
            "",
        ]
    )
    return "\n".join(lines)


def _draft_notice(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    """Name the drafter when any line on the page was drafted, otherwise say nothing."""
    notes = ctx.get("notes") or {}
    if not notes.get("notes"):
        return ""
    author = notes.get("author", {})
    return f"*{config.ui(lang)['llm_notice'].format(agent=author.get('agent', '?'), model=author.get('model', '?'))}*\n"


def readme_block(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    ui = config.ui(lang)
    stem = ctx["date"]
    lines = [
        f"### {ui['today']} · {stem}",
        "",
        f"`{ui['generated']}: {ctx['generated']}` ｜ `{ui['window']}: {window_text(config, lang, ctx)}`",
        "",
    ]
    highlights = (ctx["picked"] or ctx["baseline"])[:5]
    for item in highlights:
        why, drafted = _why_text(item, config, lang, ctx["curated"], ctx.get("notes"))
        numbers = f" ｜ {' · '.join(f'`{n}`' for n in item.numbers[:2])}" if item.numbers else ""
        lines.append(
            f"- {EVIDENCE_LABEL.get(item.evidence, '`[M]`')} **[{item.title}]({item.url})** — "
            f"{config.lane_name(item.lane, lang)}{numbers}"
        )
        if why:
            label = f"`{ui['llm_draft']}` " if drafted else ""
            lines.append(f"  - {label}{why}")
    lines.extend(
        [
            "",
            f"[{ui['today']} ›](radar/daily/{stem}.{lang}.md) · [{ui['history']} ›](radar/INDEX.md)",
            "",
            f"![lane distribution](assets/lane-distribution.{lang}.svg)",
            "",
            f"![cadence](assets/cadence.{lang}.svg)",
            "",
        ]
    )
    return "\n".join(lines)


def inject_readme(config: Config, lang: str, ctx: dict[str, Any], root: Path = ROOT) -> Path:
    path = root / README_FILES[lang]
    text = path.read_text(encoding="utf-8")
    block = readme_block(config, lang, ctx)
    if MARKER_START not in text or MARKER_END not in text:
        raise SystemExit(f"{path.name} is missing the RADAR markers")
    head, _, rest = text.partition(MARKER_START)
    _, _, tail = rest.partition(MARKER_END)
    path.write_text(f"{head}{MARKER_START}\n{block}{MARKER_END}{tail}", encoding="utf-8")
    return path


def write_daily(config: Config, ctx: dict[str, Any], root: Path = ROOT) -> list[Path]:
    written: list[Path] = []
    daily_dir = root/"radar"/"daily" if root != ROOT else DAILY_DIR
    daily_dir.mkdir(parents=True, exist_ok=True)
    for lang in LANGS:
        path = daily_dir / f"{ctx['date']}.{lang}.md"
        path.write_text(render_daily(config, lang, ctx), encoding="utf-8")
        written.append(path)
    return written


def update_index(config: Config, ctx: dict[str, Any], history: list[dict[str, Any]],
                 root: Path = ROOT) -> Path:
    path = root / "radar" / "INDEX.md"
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_latest(ctx: dict[str, Any], root: Path = ROOT) -> Path:
    path = root / "radar" / "latest.json"
    dump_json(
        path,
        {
            "date": ctx["date"],
            "generated": ctx["generated"],
            "window": ctx["window"],
            "window_detail": ctx.get("window_detail"),
            "picked": [item.to_dict() for item in ctx["picked"]],
            "lane_counts": dict(ctx["lane_rows"]),
            "evidence_mix": ctx["mix"],
            "fetch": ctx.get("fetch", {}),
            "baseline_count": len(ctx["baseline_all"]),
        },
    )
    return path
