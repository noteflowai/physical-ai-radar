"""Render layer: trilingual daily pages, README injection and the archive index.

Translation policy (documented in docs/METHODOLOGY.md):
- source titles stay in their original language (usually English) so they remain
  searchable and citable;
- the analytical layer — lane, evidence tag, key numbers, "why it matters" — is
  authored per language from templates and a curated glossary, never machine-translated;
- the English abstract excerpt is labelled as a source excerpt, not as a translation.
"""
from __future__ import annotations

import re
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
    md_text,
    md_url,
)
from .distill import number_context, one_liner, strip_boilerplate, url_key
from .charts import fit
from .feeds import iso_week, weekly_stems

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


# What a reader should be able to find in the text, by evidence class. A paper is
# expected to show a robot, a closed loop and figures; a post or a news story at
# least figures. Hardware and safety items are rarely evaluated in a closed loop, so
# its absence there says nothing.
EXPECTED = {"R": ("real_robot", "closed_loop", "numbers"), "O": ("numbers",), "M": ("numbers",)}
NO_CLOSED_LOOP = {"hardware", "safety"}


def _join(words: list[str], joiner: dict[str, str], closing: str) -> str:
    if len(words) < 2:
        return "".join(words)
    return joiner["list"].join(words[:-1]) + joiner[closing] + words[-1]


def composed_why(item: Item, config: Config, lang: str) -> str:
    """The deterministic "why it matters" line for an item nobody wrote one for.

    It used to be one sentence per lane, so a page of eight items repeated the same
    few lines word for word, and the README opened with two identical ones. This
    says what differs between items: the kind of evidence, what the source text
    leaves out, and what to watch in the lane. It only reports what the signals
    found in the text, and reads "gives no" rather than "has no": an abstract can
    omit what the paper contains.
    """
    parts = config.glossary["why_parts"]
    expected = [check for check in EXPECTED.get(item.evidence, ("numbers",))
                if not (check == "closed_loop" and item.lane in NO_CLOSED_LOOP)]
    found = set(item.signals) | ({"numbers"} if item.numbers else set())
    missing = [check for check in expected if check not in found]
    names = [parts["checks"][check][lang] for check in (missing or expected)]
    joiner = parts["joiner"][lang]
    if missing:
        checks = parts["missing"][lang].format(checks=_join(names, joiner, "last"))
    else:
        checks = parts["present"][lang].format(checks=_join(names, joiner, "all"))
    return parts["sentence"][lang].format(
        evidence=parts["evidence"].get(item.evidence, parts["evidence"]["M"])[lang],
        checks=checks,
        watch=parts["watch"].get(item.lane, {}).get(lang, ""),
    ).strip()


def why_text(item: Item, config: Config, lang: str, curated: dict[str, dict[str, str]],
              notes: dict[str, Any] | None = None) -> tuple[str, bool]:
    """Return the "why it matters" line and whether it was drafted rather than authored.

    Order: a human-curated line wins, then a drafted note for this exact item, then
    the composed line. A drafted line is always reported as drafted so the caller
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
    return composed_why(item, config, lang), False


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
        f"### {index}. {md_text(item.title)}",
        "",
        f"- **{ui['lane']}**: {config.lane_name(item.lane, lang)} ｜ "
        f"**{ui['evidence']}**: {EVIDENCE_LABEL.get(item.evidence, '`[M]`')} ｜ "
        f"**{ui['source']}**: [{md_text(item.publisher or item.source_id)}]({md_url(item.url)}) ｜ "
        f"`{item.published}`",
    ]
    if item.numbers:
        lines.append(f"- **{ui['numbers']}**: " + " · ".join(
            _number(value, f"{item.title}. {strip_boilerplate(item.summary)}") for value in item.numbers))
    if item.signals:
        tags = [ui["signals"].get(signal, signal) for signal in item.signals]
        lines.append(f"- **{ui['signals_label']}**: " + " · ".join(f"`{tag}`" for tag in tags))
    why, drafted = why_text(item, config, lang, curated, ctx_notes)
    if why:
        label = f" `{ui['llm_draft']}`" if drafted else ""
        lines.append(f"- **{ui['why']}**{label}: {why}")
    excerpt = one_liner(item.summary)
    if excerpt and item.source_id != "baseline":
        lines.append("")
        lines.append(f"> {md_text(excerpt)}")
    lines.append("")
    return lines


def _number(value: str, text: str) -> str:
    """A figure with the words around it in the source, so it says what it measures."""
    context = number_context(value, text)
    if not context or context == value:
        return f"`{value}`"
    return f"`{value}` (“{md_text(context)}”)"


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

    # A day's page is written once and read for months, so it states its numbers as
    # text. It used to embed the shared charts, which every later run redraws: an
    # old page showed today's bars under its own date.
    lines.extend(
        [
            f"## {chart_title(ui, ui['lane_distribution'], ctx)}",
            "",
            f"| {ui['lane']} | # |",
            "| --- | --- |",
        ]
    )
    for name, count in _lane_rows_named(config, ctx["lane_rows"], lang):
        lines.append(f"| {name} | {count} |")
    mix = ctx["mix"]
    runs = [(day, count) for day, count in ctx["cadence"] if day <= stem][-7:]
    lines.extend(
        [
            "",
            f"## {ui['trend']}",
            "",
            f"- {chart_title(ui, ui['source_mix'], ctx)}: "
            f"`[O]` {mix.get('O', 0)} · `[R]` {mix.get('R', 0)} · `[M]` {mix.get('M', 0)}",
            f"- {ui['cadence_line']}: " + " · ".join(f"{day[5:]} **{count}**" for day, count in runs),
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
            f"[{ui['methodology']}](../../docs/METHODOLOGY.md) · "
            f"[{ui['weekly']}](../weekly/{iso_week(stem)[0]}.{lang}.md) · [{ui['history']}](../INDEX.md)",
            "",
        ]
    )
    return "\n".join(lines)


def chart_title(ui: dict[str, Any], title: str, ctx: dict[str, Any]) -> str:
    """A chart's title with the window it counts, when the day recorded one.

    Snapshots from before the charts counted a window counted the curated baseline
    too, and keep their plain title.
    """
    days = ctx.get("chart_days")
    return f"{title} ({ui['chart_window'].format(days=days)})" if days else title


def _draft_notice(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    """Name the drafter when any line on the page was drafted, otherwise say nothing."""
    notes = ctx.get("notes") or {}
    if not notes.get("notes"):
        return ""
    author = notes.get("author", {})
    return f"*{config.ui(lang)['llm_notice'].format(agent=author.get('agent', '?'), model=author.get('model', '?'))}*\n"


def short_lane(name: str) -> str:
    """A lane's name without its parenthesised gloss, for chips and the poster."""
    return re.split(r"\s*[（(]", name, maxsplit=1)[0]


EVIDENCE_DOT = {"O": "🟢", "R": "🔵", "M": "🟡"}


def _cell(text: str) -> str:
    """Fetched text made inert for a Markdown table cell, where `|` ends the cell."""
    return md_text(text).replace("|", "\\|")


def _short_title(title: str) -> str:
    """"RoboRecover: Benchmarking ..." reads as RoboRecover once the table has named it."""
    head = title.split(": ", 1)[0]
    return head if len(head) <= 48 and head != title else fit(title, 520, 12)


def readme_block(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    """The day as the repository's front page shows it: every pick in one table, the
    reasons folded beneath it, then the charts."""
    ui, site = config.ui(lang), config.ui(lang)["site"]
    stem = ctx["date"]
    shown = ctx["picked"] or ctx["baseline"]
    names = site["evidence_names"]
    lines = [
        f"### {ui['today']} · {stem}",
        "",
        f"`{ui['generated']}: {ctx['generated']}` ｜ `{ui['window']}: {window_text(config, lang, ctx)}`",
        "",
        f"| # | {ui['evidence']} | {ui['pick']} | {ui['numbers']} |",
        "| :-: | :-: | --- | --- |",
    ]
    for rank, item in enumerate(shown, 1):
        evidence = item.evidence if item.evidence in EVIDENCE_DOT else "M"
        figures = "<br>".join(f"`{n.replace(' ', chr(0xA0))}`" for n in item.numbers[:2]) or "—"
        # the lane leads the line under the title: a column of its own left the titles no room
        under = " · ".join(part for part in (short_lane(config.lane_name(item.lane, lang)),
                                             item.publisher or item.source_id, item.published) if part)
        lines.append(
            f"| {rank:02d} | {EVIDENCE_DOT[evidence]}&nbsp;`{evidence}` | **[{_cell(item.title)}]({md_url(item.url)})**"
            f"<br><sub>{_cell(under)}</sub> | {figures} |")
    lines.extend(["", " · ".join(f"{EVIDENCE_DOT[key]} `{key}` {names[key]}" for key in ("O", "R", "M")), ""])
    whys = []
    for rank, item in enumerate(shown, 1):
        why, drafted = why_text(item, config, lang, ctx["curated"], ctx.get("notes"))
        if why:
            label = f"`{ui['llm_draft']}` " if drafted else ""
            whys.append(f"{rank}. **{md_text(_short_title(item.title))}** — {label}{why}")
    if whys:
        lines.extend([f"<details><summary><b>{ui['why']}</b> · 01–{len(whys):02d}</summary>", "", *whys, "", "</details>", ""])
    lines.extend(
        [
            f"**[{ui['today']} ›](radar/daily/{stem}.{lang}.md)** · "
            f"[{ui['weekly']} ›](radar/weekly/{iso_week(stem)[0]}.{lang}.md) · [{ui['history']} ›](radar/INDEX.md)",
            "",
            # the banner's radar already counts the lanes; the charts that are left read at full width
            f'<img src="assets/cadence.{lang}.svg" width="100%" alt="{ui["cadence"]}">',
            f'<img src="assets/evidence-mix.{lang}.svg" width="100%" alt="{ui["source_mix"]}">',
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
        "Subscribe · 订阅 · 購読: Atom [en](feed.xml) · [zh](feed.zh.xml) · [ja](feed.ja.xml)"
        " ｜ [JSON Feed](feed.json)",
        "",
    ]
    weeks = weekly_stems(root)
    if weeks:
        lines.extend([
            "## Weekly · 每周汇总 · 週間まとめ",
            "",
            "| Week | ZH | EN | JA |",
            "| --- | --- | --- | --- |",
        ])
        for stem in weeks[:60]:
            lines.append(f"| {stem} | [zh](weekly/{stem}.zh.md) | [en](weekly/{stem}.en.md) | "
                         f"[ja](weekly/{stem}.ja.md) |")
        lines.append("")
    lines.extend([
        "## Daily · 每日 · 日次",
        "",
        "| Date | Items | Lanes touched | ZH | EN | JA |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
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
            "chart_days": ctx.get("chart_days"),
        },
    )
    return path
