"""Chart layer: hand-rolled SVG so the repo needs no plotting dependency.

Chart labels are kept in English on purpose: CJK glyphs would require shipping a font
into CI to render identically everywhere. The surrounding prose carries zh / ja.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

INK = "#161d26"
MUTED = "#5f6b7a"
GRID = "#e3e9ef"
ACCENT = "#7300e5"
ACCENT_2 = "#41b1e8"
ACCENT_3 = "#eb003b"
FONT = "ui-sans-serif, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


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
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="20" y="30" font-family="{FONT}" font-size="16" font-weight="700" fill="{INK}">{_escape(title)}</text>',
    ]
    if subtitle:
        parts.append(
            f'<text x="20" y="50" font-family="{FONT}" font-size="11" fill="{MUTED}">{_escape(subtitle)}</text>'
        )
    return parts


def horizontal_bars(
    title: str,
    rows: Sequence[tuple[str, int]],
    subtitle: str = "",
    width: int = 720,
    label_width: int = 250,
) -> str:
    """Horizontal bar chart, one row per label, values annotated at the bar end."""
    rows = [(label, max(0, int(value))) for label, value in rows]
    row_height = 26
    top = 68
    height = top + row_height * max(1, len(rows)) + 24
    peak = max([value for _, value in rows] + [1])
    plot_width = width - label_width - 70
    svg = _header(width, height, title, subtitle)
    for index, (label, value) in enumerate(rows):
        y = top + index * row_height
        bar = max(2, int(plot_width * value / peak)) if value else 2
        color = ACCENT if index % 3 == 0 else (ACCENT_2 if index % 3 == 1 else ACCENT_3)
        svg.append(
            f'<text x="20" y="{y + 13}" font-family="{FONT}" font-size="12" fill="{INK}">{_escape(label)}</text>'
        )
        svg.append(
            f'<rect x="{label_width}" y="{y + 2}" width="{bar}" height="15" rx="3" fill="{color}" '
            f'fill-opacity="{0.85 if value else 0.25}"/>'
        )
        svg.append(
            f'<text x="{label_width + bar + 8}" y="{y + 14}" font-family="{FONT}" font-size="11" '
            f'fill="{MUTED}">{value}</text>'
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
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="{GRID}"/>')
        svg.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-family="{FONT}" font-size="10" '
            f'fill="{MUTED}">{int(peak * fraction)}</text>'
        )
    if points:
        slot = plot_w / len(points)
        bar_w = max(4.0, slot * 0.6)
        for index, (label, value) in enumerate(points):
            bar_h = plot_h * value / peak
            x = left + slot * index + (slot - bar_w) / 2
            y = top + plot_h - bar_h
            svg.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(1.0, bar_h):.1f}" rx="2" '
                f'fill="{ACCENT}" fill-opacity="0.85"/>'
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
                svg.append(
                    f'<text x="{x + seg / 2:.1f}" y="{top + 15}" text-anchor="middle" font-family="{FONT}" '
                    f'font-size="11" font-weight="700" fill="#ffffff">{key} {value}</text>'
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
) -> dict[str, Path]:
    """Render the three standing charts and return their paths."""
    subtitle = f"physical-ai-radar · generated {generated}"
    return {
        "lanes": write_svg(
            assets_dir / "lane-distribution.svg",
            horizontal_bars("Lane distribution (tracked items)", list(lane_rows), subtitle),
        ),
        "cadence": write_svg(
            assets_dir / "cadence.svg",
            cadence_bars("Daily cadence (items cleared per run)", list(cadence), subtitle),
        ),
        "evidence": write_svg(
            assets_dir / "evidence-mix.svg",
            evidence_strip("Evidence mix", mix, subtitle),
        ),
    }
