"""The banner at the top of each README: the day's radar as one SVG.

GitHub shows a README as the repository's front page, and an image there is the
first thing a visitor sees. This is the landing page's hero in one picture: the
date, the headline, the day's four counts and the lane radar with today's picks,
its sweep turning. It is a dark card with rounded corners, so it sits the same on
GitHub's light and dark themes, and it is redrawn with the charts, from the same
context, so a re-render reproduces it byte for byte.

Text is SVG text: the glyphs come from the reader's browser, as in the charts, and
the widths are estimates (charts.text_width) on the generous side.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .charts import text_width
from .config import Config
from .site import e, radar_svg

WIDTH, HEIGHT = 1280, 480
LEFT = 60
TEXT_WIDTH = 630
STATS_TOP = HEIGHT - 52 - 88
# (font size, line height, lines): the largest headline that fits whole
HEADLINES = ((52, 64, 2), (46, 56, 2), (40, 50, 3))
FONTS = {
    "zh": '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC"',
    "en": '"Noto Sans"',
    "ja": '"Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", "Noto Sans CJK JP", "Noto Sans JP"',
}
LATIN = 'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial'
WIDE = "\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef"
# one CJK character, a Latin word with its trailing space, or a run of spaces
TOKENS = re.compile(f"[{WIDE}]|[^\\s{WIDE}]+\\s*|\\s+")
CLOSING = set("，。、：；！？）」』・")
MONO = 'ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace'


def wrap(text: str, width: float, size: int, lines: int = 99) -> list[str]:
    """Break at spaces in Latin text and anywhere in CJK, at most `lines` lines,
    the last one ending in an ellipsis when the text runs over."""
    tokens = TOKENS.findall(text)
    out, line = [], ""
    for token in tokens:
        # closing punctuation stays with the text it closes
        if line.strip() and token not in CLOSING and text_width((line + token).rstrip(), size) > width:
            out.append(line.rstrip())
            line = token.lstrip()
        else:
            line += token
    if line.strip():
        out.append(line.rstrip())
    if len(out) <= lines:
        return out
    last = out[lines - 1]
    while last and text_width(last + "…", size) > width:
        last = last[:-1]
    return out[:lines - 1] + [last.rstrip(" ，,、") + "…"]


def stats(config: Config, lang: str, ctx: dict[str, Any]) -> list[tuple[str, str]]:
    site = config.ui(lang)["site"]
    fetch = ctx.get("fetch") or {}
    days = ctx.get("chart_days") or 7
    return [(str(len(ctx["picked"])), site["stat_picks"]),
            (str(fetch.get("items", "—")), site["stat_scanned"]),
            (f"{fetch.get('answered', '—')}/{fetch.get('attempted', '—')}", site["stat_sources"]),
            (str(sum(count for _, count in ctx["lane_rows"])), site["stat_week"].format(days=days))]


def banner_svg(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    ui, site, day = config.ui(lang), config.ui(lang)["site"], ctx["date"]
    sans = f"{LATIN}, {FONTS[lang]}, sans-serif"
    eyebrow = site["eyebrow"].format(day=day)
    pill = text_width(eyebrow, 17) + 58
    size, height, headline = next(
        ((size, height, lines) for size, height, count in HEADLINES
         if len(lines := wrap(site["headline"], TEXT_WIDTH, size)) <= count),
        (40, 50, wrap(site["headline"], TEXT_WIDTH, 40, 3)))
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" '
        f'height="{HEIGHT}" role="img" aria-label="{e(ui["title"])} · {e(day)} · {e(site["headline"])}">',
        f'<title>{e(ui["title"])} · {e(day)}</title>',
        "<style>"
        f"text{{font-family:{sans}}}"
        f".mono{{font-family:{MONO}}}"
        ".ring{fill:none;stroke:rgba(52,211,153,.18)}.rim{stroke:rgba(52,211,153,.35)}"
        ".spoke{stroke:rgba(148,163,184,.14)}"
        ".area{fill:rgba(139,92,246,.24);stroke:#a78bfa;stroke-width:1.5;stroke-linejoin:round}"
        ".beam{stroke:rgba(52,211,153,.8);stroke-width:1.5}"
        ".label{font-size:17px;font-weight:600}.label .count{fill:#8b98ad;font-weight:400}"
        ".core{fill:#34d399}.blip circle{fill:var(--lane)}"
        ".blip .halo{opacity:0;transform-box:fill-box;transform-origin:center;"
        "animation:blip 3.2s var(--delay) infinite}"
        "@keyframes blip{0%{opacity:.55;transform:scale(.4)}60%,100%{opacity:0;transform:scale(1.6)}}"
        ".live{animation:live 2.4s infinite}@keyframes live{50%{opacity:.35}}"
        "@media (prefers-reduced-motion:reduce){.blip .halo,.live{animation:none}.sweep{display:none}}"
        "</style>",
        '<defs>'
        '<radialGradient id="glow-a" cx="88%" cy="-10%" r="75%"><stop offset="0" stop-color="#8b5cf6" '
        'stop-opacity=".38"/><stop offset="1" stop-color="#8b5cf6" stop-opacity="0"/></radialGradient>'
        '<radialGradient id="glow-b" cx="0%" cy="100%" r="60%"><stop offset="0" stop-color="#34d399" '
        'stop-opacity=".18"/><stop offset="1" stop-color="#34d399" stop-opacity="0"/></radialGradient>'
        '<linearGradient id="shine" x1="0" y1="0" x2="1" y2="0"><stop offset=".05" stop-color="#ffffff"/>'
        '<stop offset=".6" stop-color="#c4b5fd"/><stop offset="1" stop-color="#6ee7b7"/></linearGradient>'
        '<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" '
        'fill="none" stroke="rgba(148,163,184,.07)"/></pattern>'
        f'<clipPath id="card"><rect width="{WIDTH}" height="{HEIGHT}" rx="24"/></clipPath>'
        '</defs>',
        '<g clip-path="url(#card)">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#05070d"/>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#grid)"/>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glow-a)"/>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glow-b)"/>',
        '</g>',
        f'<rect x=".5" y=".5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="24" fill="none" '
        'stroke="rgba(148,163,184,.22)"/>',
        # the eyebrow: a live dot and the day
        f'<rect x="{LEFT}" y="56" width="{pill:.0f}" height="38" rx="19" fill="rgba(52,211,153,.10)" '
        'stroke="rgba(52,211,153,.35)"/>',
        f'<circle class="live" cx="{LEFT + 22}" cy="75" r="6" fill="#34d399"/>',
        f'<text class="mono" x="{LEFT + 38}" y="81" font-size="17" font-weight="600" fill="#6ee7b7">'
        f'{e(eyebrow)}</text>',
    ]
    y = 104 + size
    for line in headline:
        out.append(f'<text x="{LEFT}" y="{y}" font-size="{size}" font-weight="800" fill="url(#shine)">{e(line)}</text>')
        y += height
    # the tagline takes what room the headline leaves above the counts
    y += 50 - height
    room = (STATS_TOP - 18 - y) // 28 + 1
    for line in wrap(site["og_tagline"], TEXT_WIDTH, 20, room):
        out.append(f'<text x="{LEFT}" y="{y}" font-size="20" fill="#9fb0c6">{e(line)}</text>')
        y += 28
    # the day in four numbers, the boxes as wide as the longest label
    counts = stats(config, lang, ctx)
    gap = 12
    box_w = min(max(138, *(text_width(label, 15) + 30 for _, label in counts)), (TEXT_WIDTH - 3 * gap) / 4)
    for index, (value, label) in enumerate(counts):
        x = LEFT + index * (box_w + gap)
        out.append(f'<rect x="{x:.0f}" y="{STATS_TOP}" width="{box_w:.0f}" height="88" rx="14" '
                   'fill="rgba(16,24,43,.85)" stroke="rgba(148,163,184,.18)"/>')
        out.append(f'<text x="{x + 15:.0f}" y="{STATS_TOP + 30}" font-size="15" fill="#8b98ad">{e(label)}</text>')
        out.append(f'<text class="mono" x="{x + 15:.0f}" y="{STATS_TOP + 70}" font-size="32" font-weight="700" '
                   f'fill="#e6edf3">{e(value)}</text>')
    # the radar, as on the landing page
    # a smaller radius than the page's, so the lane names fit inside the card;
    # the English names are the longest and pull it in further
    radar = radar_svg(config, lang, ctx["lane_rows"], ctx["picked"], radius=108 if lang == "en" else 120, short=True)
    out.append(radar.replace('<svg class="radar"', '<svg class="radar" x="684" y="60" width="560" height="360" '
                             'overflow="visible"', 1))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_banners(config: Config, ctx: dict[str, Any], assets: Path, langs: tuple[str, ...]) -> list[Path]:
    written = []
    for lang in langs:
        path = assets/f"banner.{lang}.svg"
        path.write_text(banner_svg(config, lang, ctx), encoding="utf-8")
        written.append(path)
    return written
