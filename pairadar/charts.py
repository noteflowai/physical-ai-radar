"""Chart layer: hand-rolled SVG so the repo needs no plotting dependency.

Chart labels are kept in English on purpose: CJK glyphs would require shipping a font
into CI to render identically everywhere. The surrounding prose carries zh / ja.
"""
from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

INK = "#161d26"
MUTED = "#5f6b7a"
GRID = "#e3e9ef"
ACCENT = "#7300e5"
ACCENT_2 = "#41b1e8"
ACCENT_3 = "#eb003b"
BAR_MAX = 34.0
# Deterministic per-label colours: a lane keeps its colour across days, and sha1
# rather than hash() so the palette does not shuffle per process.
# ACCENT_3, the alarm red, is deliberately absent: it belongs to the media segment
# of the evidence strip, and a lane wearing it reads as a warning about the lane
# rather than as a count.
PALETTE = ("#7300e5", "#41b1e8", "#00a878", "#f07c00", "#8a6bff", "#0d8ecf", "#7cb342", "#00838f")
# GitHub renders READMEs in a dark theme for a large share of readers, where a hard
# white plate glares. One file adapts instead of shipping two.
STYLE = (
    "<style>\n"
    "  .plate { fill: #ffffff }\n"
    "  .ink { fill: #161d26 }\n"
    "  .muted { fill: #5f6b7a }\n"
    "  .grid { stroke: #e3e9ef }\n"
    "  @media (prefers-color-scheme: dark) {\n"
    "    .plate { fill: #0d1117 }\n"
    "    .ink { fill: #e6edf3 }\n"
    "    .muted { fill: #8b949e }\n"
    "    .grid { stroke: #30363d }\n"
    "  }\n"
    "</style>"
)
FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


# Rough advance width of the 12px UI sans stack, in pixels per character. Without a
# font library this is an estimate, deliberately on the generous side so a label that
# reports as fitting really does.
CHAR_PX = 6.8


def char_width(char: str, font_size: int = 12) -> float:
    """Estimated width of one glyph. A CJK character occupies about one em, roughly
    twice a Latin average, so counting characters would under-measure it by half."""
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return float(font_size)
    return CHAR_PX * font_size / 12


def text_width(text: str, font_size: int = 12) -> float:
    """Estimated rendered width, used to keep labels out of the plot area."""
    return sum(char_width(char, font_size) for char in text)


def fit(text: str, budget: float, font_size: int = 12) -> str:
    """Trim a label to a pixel budget, marking the cut. Characters are measured one at
    a time because a mixed label has no single character width to divide by."""
    if text_width(text, font_size) <= budget:
        return text
    room = budget - char_width("…", font_size)
    kept: list[str] = []
    used = 0.0
    for char in text:
        width = char_width(char, font_size)
        if used + width > room:
            break
        kept.append(char)
        used += width
    return "".join(kept).rstrip() + "…"


def color_for(label: str) -> str:
    """Stable colour for a label, so a lane looks the same from day to day."""
    return PALETTE[int(hashlib.sha1(label.encode("utf-8")).hexdigest()[:8], 16) % len(PALETTE)]


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _header(width: int, height: int, title: str, subtitle: str = "") -> list[str]:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{_escape(title)}">',
        STYLE,
        f'<rect class="plate" width="{width}" height="{height}"/>',
        f'<text x="20" y="30" font-family="{FONT}" font-size="16" font-weight="700" class="ink">{_escape(title)}</text>',
    ]
    if subtitle:
        parts.append(
            f'<text x="20" y="50" font-family="{FONT}" font-size="11" class="muted">{_escape(subtitle)}</text>'
        )
    return parts


def horizontal_bars(
    title: str,
    rows: Sequence[tuple[str, int]],
    subtitle: str = "",
    width: int = 720,
    label_width: int = 250,
    colors: Sequence[str] | None = None,
) -> str:
    """Horizontal bar chart, one row per label, values annotated at the bar end."""
    # Labels are drawn from x=20 and the bars start at label_width, so anything
    # wider than the gutter would be painted over the bars.
    gutter = label_width - 30
    rows = [(fit(label, gutter), max(0, int(value))) for label, value in rows]
    row_height = 26
    top = 68
    height = top + row_height * max(1, len(rows)) + 24
    peak = max([value for _, value in rows] + [1])
    plot_width = width - label_width - 70
    svg = _header(width, height, title, subtitle)
    for index, (label, value) in enumerate(rows):
        y = top + index * row_height
        bar = max(2, int(plot_width * value / peak)) if value else 2
        # Hashing a label cannot guarantee two lanes differ, and two lanes sharing a
        # colour in one chart is worse than any single colour choice. A caller that
        # knows the full set passes colours by position instead.
        color = colors[index % len(colors)] if colors else color_for(label)
        svg.append(
            f'<text x="20" y="{y + 13}" font-family="{FONT}" font-size="12" class="ink">{_escape(label)}</text>'
        )
        svg.append(
            f'<rect x="{label_width}" y="{y + 2}" width="{bar}" height="15" rx="3" fill="{color}" '
            f'fill-opacity="{0.85 if value else 0.25}"/>'
        )
        svg.append(
            f'<text x="{label_width + bar + 8}" y="{y + 14}" font-family="{FONT}" font-size="11" '
            f'class="muted">{value}</text>'
        )
    svg.append("</svg>")
    return "\n".join(svg)


def cadence_bars(
    title: str,
    points: Sequence[tuple[str, int]],
    subtitle: str = "",
    width: int = 720,
    height: int = 220,
) -> str:
    """Vertical bars for the recent daily cadence (date -> item count)."""
    points = list(points)[-21:]
    left, right, top, bottom = 44, 16, 70, 34
    plot_w = width - left - right
    plot_h = height - top - bottom
    peak = max([value for _, value in points] + [1])
    svg = _header(width, height, title, subtitle)
    for fraction in (0, 0.5, 1.0):
        y = top + plot_h - plot_h * fraction
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid"/>')
        svg.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="{FONT}" font-size="10" '
            f'fill="{MUTED}">{int(peak * fraction)}</text>'
        )
    if points:
        mean = sum(value for _, value in points) / len(points)
        y_mean = top + plot_h - plot_h * mean / peak
        svg.append(f'<line x1="{left}" y1="{y_mean:.1f}" x2="{width - right}" y2="{y_mean:.1f}" '
                   f'stroke="{ACCENT_2}" stroke-width="1.2" stroke-dasharray="5 4" opacity="0.9"/>')
        # the line's key sits in the title row: inside the plot the latest bar covered it
        key = width - right - text_width(f"mean {mean:.1f}", 10) - 6
        svg.append(f'<line x1="{key - 22:.1f}" y1="26" x2="{key:.1f}" y2="26" stroke="{ACCENT_2}" '
                   f'stroke-width="1.2" stroke-dasharray="5 4"/>')
        svg.append(f'<text class="muted" x="{width - right}" y="30" text-anchor="end" '
                   f'font-family="{FONT}" font-size="10">mean {mean:.1f}</text>')
        slot = plot_w / len(points)
        # A fresh archive holds two runs. Without a cap each bar becomes a 200px
        # billboard and the chart reads as a bug rather than as two data points.
        bar_w = min(BAR_MAX, max(4.0, slot * 0.6))
        offset = (slot - bar_w) / 2
        for index, (label, value) in enumerate(points):
            bar_h = plot_h * value / peak
            x = left + slot * index + offset
            y = top + plot_h - bar_h
            latest = index == len(points) - 1
            svg.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(1.0, bar_h):.1f}" rx="2" '
                f'fill="{ACCENT}" fill-opacity="{0.95 if latest else 0.5}"/>'
            )
            if latest:
                # The run this chart was generated by; the one a reader is looking for.
                svg.append(
                    f'<text class="ink" x="{x + bar_w / 2:.1f}" y="{y - 5:.1f}" text-anchor="middle" '
                    f'font-family="{FONT}" font-size="10" font-weight="700">{value}</text>'
                )
            if index == 0 or index == len(points) - 1 or index == len(points) // 2:
                svg.append(
                    f'<text x="{x + bar_w / 2:.1f}" y="{height - 12}" text-anchor="middle" '
                    f'font-family="{FONT}" font-size="9" fill="{MUTED}">{_escape(label[5:])}</text>'
                )
    svg.append("</svg>")
    return "\n".join(svg)


def evidence_strip(
    title: str,
    mix: dict[str, int],
    subtitle: str = "",
    width: int = 720,
    height: int = 132,
) -> str:
    """Single segmented bar showing the O / R / M evidence composition."""
    order = [("O", "official (first-party)", ACCENT), ("R", "paper / preprint", ACCENT_2), ("M", "media / secondary", ACCENT_3)]
    total = sum(max(0, mix.get(key, 0)) for key, _, _ in order) or 1
    left, top, bar_h = 20, 70, 22
    plot_w = width - 40
    svg = _header(width, height, title, subtitle)
    x = float(left)
    for key, label, color in order:
        value = max(0, mix.get(key, 0))
        seg = plot_w * value / total
        if seg > 0:
            svg.append(
                f'<rect x="{x:.1f}" y="{top}" width="{seg:.1f}" height="{bar_h}" fill="{color}" fill-opacity="0.85"/>'
            )
            if seg > 42:
                share = f" · {round(100 * value / total)}%" if seg > 96 else ""
                svg.append(
                    f'<text x="{x + seg / 2:.1f}" y="{top + 15}" text-anchor="middle" font-family="{FONT}" '
                    f'font-size="11" font-weight="700" fill="#ffffff">{key} {value}{share}</text>'
                )
        x += seg
    legend_y = top + bar_h + 22
    lx = left
    for key, label, color in order:
        svg.append(f'<rect x="{lx}" y="{legend_y - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        svg.append(
            f'<text x="{lx + 15}" y="{legend_y}" font-family="{FONT}" font-size="10" fill="{MUTED}">'
            f'{_escape(f"{key} = {label}")}</text>'
        )
        lx += 220
    svg.append("</svg>")
    return "\n".join(svg)


def write_svg(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    return path


def write_all(
    assets_dir: Path,
    lane_rows: Iterable[tuple[str, int]],
    cadence: Iterable[tuple[str, int]],
    mix: dict[str, int],
    generated: str,
    titles: dict[str, str] | None = None,
    suffix: str = "",
) -> dict[str, Path]:
    """Render the three standing charts and return their paths.

    `titles` and `suffix` let one day produce one set per language. The glyphs come
    from the reader's browser because these are SVG text, so a localized label costs
    no bundled font and nothing about the render is less reproducible.
    """
    heading = {"lanes": "Lane distribution (tracked items)",
               "cadence": "Daily cadence (items cleared per run)",
               "mix": "Evidence mix"}
    heading.update(titles or {})
    subtitle = f"physical-ai-radar · generated {generated}"
    return {
        "lanes": write_svg(
            assets_dir / f"lane-distribution{suffix}.svg",
            horizontal_bars(heading["lanes"], list(lane_rows), subtitle,
                            colors=PALETTE),
        ),
        "cadence": write_svg(
            assets_dir / f"cadence{suffix}.svg",
            cadence_bars(heading["cadence"], list(cadence), subtitle),
        ),
        "evidence": write_svg(
            assets_dir / f"evidence-mix{suffix}.svg",
            evidence_strip(heading["mix"], mix, subtitle),
        ),
    }
