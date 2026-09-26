"""Site layer: the GitHub Pages landing page, one per language, and its social card.

The daily Markdown pages stay the record; this is the front door. It is written by
the same run from the same context, so the landing page and the day's pages cannot
disagree, and it is plain HTML with no front matter, which Jekyll copies as it is:
nothing fetched reaches Liquid. Every fetched string passes through `html.escape`.

A link shared on X, Slack, WeChat or LINE is previewed from its Open Graph tags, and
those previewers do not run scripts or read SVG, so the card is a PNG
(`assets/og.<lang>.png`) rendered once from `og_card` by `python3 -m pairadar.site
--og`, which needs a local Chrome. The daily run never calls a browser.

Where a link does not travel -- an image forwarded in WeChat, Xiaohongshu or LINE --
the reader draws the day's poster in their own browser (`assets/site.js`) from the
page's `share-data`, QR code included, and the day's digest is one copy away as text.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .config import LANGS, ROOT, Config, Item, load_config
from .distill import number_context, one_liner, strip_boilerplate
from .feeds import SITE_URL, atom_name, iso_week, landing_url
from .feeds import page_url as daily_url
from .qr import rows as qr_rows
from .qr import svg as qr_svg
from .render import why_text

HOME_URL = "https://github.com/noteflowai/physical-ai-radar"
PAGES = {"zh": "index.html", "en": "en/index.html", "ja": "ja/index.html"}
HTML_LANG = {"zh": "zh-Hans", "en": "en", "ja": "ja"}
OG_LOCALE = {"zh": "zh_CN", "en": "en_US", "ja": "ja_JP"}
SWITCH_LABEL = {"zh": "中文", "en": "EN", "ja": "日本語"}
# One colour per lane, by taxonomy order, so a lane wears the same colour in every
# language. Chosen to read on the dark plate; none is the alarm red.
LANE_COLORS = ("#8b5cf6", "#38bdf8", "#34d399", "#fb923c", "#f472b6", "#facc15", "#a3e635", "#22d3ee")
OG_SIZE = (1200, 630)
CADENCE_RUNS = 14


def e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def lane_color(config: Config, lane_id: str) -> str:
    ids = [lane["id"] for lane in config.lanes]
    return LANE_COLORS[ids.index(lane_id) % len(LANE_COLORS)] if lane_id in ids else LANE_COLORS[0]


def prefix(lang: str) -> str:
    """Relative path from a language's landing page back to the site root."""
    return "../" * PAGES[lang].count("/")


def page_url(lang: str) -> str:
    return landing_url(lang)


def _point(angle: float, radius: float) -> tuple[float, float]:
    return round(radius * math.sin(angle), 1), round(-radius * math.cos(angle), 1)


def radar_svg(config: Config, lang: str, lane_rows: list[tuple[str, int]], picked: list[Item],
              radius: float = 165.0, labels: bool = True) -> str:
    """Eight lanes as the spokes of a radar: the filled shape is the recent count per
    lane, and each of today's picks is a blip on its lane's spoke, nearer the rim the
    higher it scored. A blip links to its card."""
    lanes = [lane["id"] for lane in config.lanes]
    counts = dict(lane_rows)
    peak = max([counts.get(lane, 0) for lane in lanes] + [1])
    step = 2 * math.pi / len(lanes)
    # Labels need a wide margin; without them the chart can fill its box.
    box = "-340 -212 680 424" if labels else "%s %s %s %s" % (-radius - 12, -radius - 12, 2 * radius + 24, 2 * radius + 24)
    rim = " ".join("%s,%s" % _point(i * step, radius) for i in range(len(lanes)))
    out = [f'<svg class="radar" viewBox="{box}" role="img" '
           f'aria-label="{e(config.ui(lang)["lane_distribution"])}">',
           # The sweep fades from its leading edge back, across its 40 degrees.
           '<defs><linearGradient id="sweep-fade" gradientUnits="userSpaceOnUse" x1="0" y1="%s" x2="%s" y2="%s">'
           '<stop offset="0" stop-color="#34d399" stop-opacity="0"/><stop offset="1" stop-color="#34d399" '
           'stop-opacity=".4"/></linearGradient><clipPath id="rim"><polygon points="%s"/></clipPath></defs>'
           % (-radius, *_point(math.radians(40), radius), rim)]
    for ring in (0.25, 0.5, 0.75, 1.0):
        ring_points = " ".join("%s,%s" % _point(i * step, radius * ring) for i in range(len(lanes)))
        out.append(f'<polygon class="ring{" rim" if ring == 1.0 else ""}" points="{ring_points}"/>')
    for index, lane in enumerate(lanes):
        x, y = _point(index * step, radius)
        out.append(f'<line class="spoke" x1="0" y1="0" x2="{x}" y2="{y}"/>')
        if labels:
            lx, ly = _point(index * step, radius + 16)
            anchor = "middle" if abs(lx) < 1 else ("start" if lx > 0 else "end")
            baseline = "auto" if ly < -radius * 0.9 else ("hanging" if ly > radius * 0.9 else "middle")
            out.append(f'<text class="label" x="{lx}" y="{ly}" text-anchor="{anchor}" '
                       f'dominant-baseline="{baseline}" fill="{lane_color(config, lane)}">'
                       f'{e(config.chart_label(lane, lang))} <tspan class="count">{counts.get(lane, 0)}</tspan></text>')
    shape = " ".join("%s,%s" % _point(i * step, radius * max(counts.get(lane, 0) / peak, 0.04))
                     for i, lane in enumerate(lanes))
    out.append(f'<polygon class="area" points="{shape}"/>')
    edge = _point(math.radians(40), radius)
    wedge = "M0,0 L0,%s A%s,%s 0 0,1 %s,%s Z" % (-radius, radius, radius, *edge)
    out.append(f'<g clip-path="url(#rim)"><g class="sweep"><path d="{wedge}" fill="url(#sweep-fade)"/>'
               f'<line x1="0" y1="0" x2="{edge[0]}" y2="{edge[1]}" class="beam"/>'
               '<animateTransform attributeName="transform" type="rotate" from="0" to="360" '
               'dur="8s" repeatCount="indefinite"/></g></g>')
    top = max([item.score for item in picked] + [1.0])
    per_lane = {lane: sum(item.lane == lane for item in picked) for lane in lanes}
    seen: dict[str, int] = {}
    for rank, item in enumerate(picked, 1):
        if item.lane not in lanes:
            continue
        nth = seen.get(item.lane, 0)
        seen[item.lane] = nth + 1
        # picks sharing a lane fan out either side of its spoke
        angle = lanes.index(item.lane) * step + (nth - (per_lane[item.lane] - 1) / 2) * math.radians(10)
        x, y = _point(angle, radius * (0.35 + 0.55 * item.score / top))
        out.append(f'<a href="#p{rank}" class="blip" style="--lane:{lane_color(config, item.lane)};'
                   f'--delay:{rank * 0.35:.2f}s"><circle cx="{x}" cy="{y}" r="11" class="halo"/>'
                   f'<circle cx="{x}" cy="{y}" r="5"/><title>{e(item.title)}</title></a>')
    out.append('<circle class="core" r="3"/></svg>')
    return "\n".join(out)


def _numbers(item: Item) -> str:
    text = f"{item.title}. {strip_boilerplate(item.summary)}"
    if not item.numbers:
        return ""
    lead, *rest = item.numbers
    context = number_context(lead, text)
    quote = f'<span class="context">“{e(context)}”</span>' if context and context != lead else ""
    chips = "".join(f'<span class="chip">{e(value)}</span>' for value in rest[:3])
    return (f'<p class="figure"><strong>{e(lead)}</strong>{quote}</p>'
            + (f'<p class="chips">{chips}</p>' if chips else ""))


def card(config: Config, lang: str, ctx: dict[str, Any], item: Item, rank: int) -> str:
    ui, site = config.ui(lang), config.ui(lang)["site"]
    why, drafted = why_text(item, config, lang, ctx["curated"], ctx.get("notes"))
    evidence = item.evidence if item.evidence in site["evidence_names"] else "M"
    excerpt = one_liner(item.summary) if item.source_id != "baseline" else ""
    share_text = site["share_text"].format(day=ctx["date"], title=item.title)
    share_url = daily_url(ctx["date"], lang)
    intent = "https://twitter.com/intent/tweet?" + urlencode({"text": share_text, "url": share_url})
    draft_label = f'<span class="draft">{e(ui["llm_draft"])}</span> ' if drafted else ""
    signals = "".join(f'<li>{e(ui["signals"].get(signal, signal))}</li>' for signal in item.signals)
    return "\n".join(part for part in (
        f'<article class="card" id="p{rank}" data-lane="{e(item.lane)}" style="--lane:{lane_color(config, item.lane)}">',
        '<header class="meta">',
        f'<span class="rank">{rank:02d}</span>',
        f'<span class="lane">{e(config.lane_name(item.lane, lang))}</span>',
        f'<span class="ev ev-{evidence}" title="{e(ui["evidence_legend"])}">'
        f'{evidence} · {e(site["evidence_names"][evidence])}</span>',
        '</header>',
        f'<h3><a href="{e(item.url)}" rel="noopener" target="_blank">{e(item.title)}</a></h3>',
        _numbers(item),
        f'<p class="why">{draft_label}{e(why)}</p>' if why else "",
        f'<p class="excerpt">{e(excerpt)}</p>' if excerpt else "",
        f'<ul class="signals">{signals}</ul>' if signals else "",
        '<footer>',
        f'<span class="source">{e(item.publisher or item.source_id)} · {e(item.published)}</span>',
        f'<span class="actions"><button type="button" class="share" data-url="{e(share_url)}" '
        f'data-text="{e(share_text)}" data-done="{e(site["copied"])}">{e(site["share"])}</button>'
        f'<a class="x" href="{e(intent)}" rel="noopener" target="_blank">{e(site["post_x"])}</a></span>',
        '</footer>',
        '</article>',
    ) if part)


def cadence_svg(cadence: list[tuple[str, int]]) -> str:
    """The last runs as bars, each with its count above and its date below; the
    newest bar is lit."""
    runs = cadence[-CADENCE_RUNS:]
    if not runs:
        return ""
    peak = max([count for _, count in runs] + [1])
    # A fixed width, so a short history draws narrower bars rather than larger text.
    slot = 520 / CADENCE_RUNS
    bars = []
    for index, (day, count) in enumerate(runs):
        height = max(2.0, 70 * count / peak)
        x = index * slot
        bars.append(f'<g><rect x="{x + 4:.1f}" y="{92 - height:.1f}" width="{slot - 8:.1f}" '
                    f'height="{height:.1f}" rx="4"/><text x="{x + slot / 2:.1f}" y="{86 - height:.1f}" '
                    f'class="n">{count}</text><text x="{x + slot / 2:.1f}" y="110" class="d">'
                    f'{e(day[5:])}</text><title>{e(day)}: {count}</title></g>')
    return (f'<svg class="cadence" viewBox="0 0 520 116" role="img" '
            f'aria-label="cadence">' + "".join(bars) + "</svg>")


def lane_legend(config: Config, lang: str, lane_rows: list[tuple[str, int]]) -> str:
    """The radar's labels as a list, for screens too narrow to read them on the chart."""
    counts = dict(lane_rows)
    return '<ul class="lane-legend">' + "".join(
        f'<li style="--lane:{lane_color(config, lane["id"])}">{e(config.chart_label(lane["id"], lang))} '
        f'<b>{counts.get(lane["id"], 0)}</b></li>' for lane in config.lanes) + "</ul>"


def mix_bar(config: Config, lang: str, mix: dict[str, int]) -> str:
    names = config.ui(lang)["site"]["evidence_names"]
    total = sum(mix.get(key, 0) for key in names) or 1
    segments = "".join(
        f'<span class="ev-{key}" style="flex:{mix.get(key, 0)}" title="{e(names[key])}: {mix.get(key, 0)}"></span>'
        for key in names if mix.get(key, 0))
    legend = "".join(f'<li><b class="ev-{key}">{round(100 * mix.get(key, 0) / total)}%</b>'
                     f'<span>{key} · {e(names[key])}</span></li>' for key in names)
    return f'<div class="mix">{segments}</div><ul class="mix-legend">{legend}</ul>'


def head(config: Config, lang: str, ctx: dict[str, Any], title: str, description: str) -> str:
    base = prefix(lang)
    alternates = "\n".join(f'<link rel="alternate" hreflang="{HTML_LANG[other]}" href="{page_url(other)}">'
                           for other in LANGS)
    feeds = "\n".join(f'<link rel="alternate" type="application/atom+xml" title="{e(config.ui(other)["title"])}" '
                      f'href="{base}radar/{atom_name(other)}">' for other in LANGS)
    image = f"{SITE_URL}/assets/og.{lang}.png"
    ld = json_script({
        "@context": "https://schema.org", "@type": "ItemList", "name": title,
        "url": page_url(lang), "dateModified": ctx["date"],
        "itemListElement": [{"@type": "ListItem", "position": rank, "url": item.url, "name": item.title}
                            for rank, item in enumerate(ctx["picked"], 1)],
    })
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{page_url(lang)}">
{alternates}
<link rel="alternate" hreflang="x-default" href="{page_url('en')}">
{feeds}
<link rel="alternate" type="application/feed+json" title="JSON Feed" href="{base}radar/feed.json">
<link rel="icon" href="{base}assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{base}assets/site.css">
<meta name="theme-color" content="#05070d">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{e(config.ui(lang)['title'])}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{page_url(lang)}">
<meta property="og:locale" content="{OG_LOCALE[lang]}">
<meta property="og:image" content="{image}">
<meta property="og:image:width" content="{OG_SIZE[0]}">
<meta property="og:image:height" content="{OG_SIZE[1]}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(description)}">
<meta name="twitter:image" content="{image}">
<script type="application/ld+json">{ld}</script>"""


def nav(config: Config, lang: str, day: str) -> str:
    ui, site, base = config.ui(lang), config.ui(lang)["site"], prefix(lang)
    switch = "".join(
        f'<a href="{base}{PAGES[other].removesuffix("index.html")}"'
        + (' aria-current="page"' if other == lang else "") + f' hreflang="{HTML_LANG[other]}">{SWITCH_LABEL[other]}</a>'
        for other in LANGS)
    return f"""<nav class="top">
<a class="brand" href="{base}{PAGES[lang].removesuffix('index.html') or './'}"><img src="{base}assets/favicon.svg" alt="" width="26" height="26"><span>{e(ui['title'])}</span></a>
<span class="links"><a href="{base}radar/daily/{day}.{lang}.html">{e(ui['today'])}</a><a href="{base}radar/weekly/{iso_week(day)[0]}.{lang}.html">{e(ui['weekly'])}</a><a href="{base}radar/INDEX.html">{e(ui['history'])}</a><a href="{base}radar/{atom_name(lang)}">{e(site['nav_feeds'])}</a><a href="{HOME_URL}">GitHub</a></span>
<span class="switch">{switch}</span>
</nav>"""


def short_lane(name: str) -> str:
    """A lane's name without its parenthesised gloss, for chips and the poster."""
    return re.split(r"\s*[（(]", name, maxsplit=1)[0]


def json_script(payload: Any) -> str:
    """JSON for a <script> element: inside it only "<" can end the element, and
    escaped, the JSON reads the same."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def digest(config: Config, lang: str, day: str, shown: list[Item]) -> str:
    """The day as plain text, to paste where a link preview would be lost: a chat,
    a mail, a slide."""
    ui, site = config.ui(lang), config.ui(lang)["site"]
    lines = [f"{ui['title']} · {day}", site["headline"], ""]
    for rank, item in enumerate(shown, 1):
        lines.append(f"{rank:02d} [{short_lane(config.lane_name(item.lane, lang))} · {item.evidence}] {item.title}")
        lines.append(f"   {item.url}")
    lines += ["", f"{site['digest_more']}: {daily_url(day, lang)}"]
    return "\n".join(lines)


def share_data(config: Config, lang: str, ctx: dict[str, Any], shown: list[Item]) -> dict[str, Any]:
    """Everything the poster needs, so the script never reads it back out of the page."""
    ui, site, day = config.ui(lang), config.ui(lang)["site"], ctx["date"]
    url = daily_url(day, lang)
    return {
        "lang": lang, "date": day, "url": url, "title": ui["title"], "headline": site["headline"],
        "scan": site["poster_scan"], "site": SITE_URL.removeprefix("https://"),
        "evidence": site["evidence_names"], "qr": qr_rows(url),
        "digest": digest(config, lang, day, shown),
        "picks": [{"rank": rank, "title": item.title, "lane": short_lane(config.lane_name(item.lane, lang)),
                   "color": lane_color(config, item.lane), "evidence": item.evidence,
                   "figure": item.numbers[0] if item.numbers else ""}
                  for rank, item in enumerate(shown, 1)],
    }


def toolbar(config: Config, lang: str, shown: list[Item]) -> str:
    """Lane filters for the cards, and the two ways to take the day elsewhere."""
    site = config.ui(lang)["site"]
    counts: dict[str, int] = {}
    for item in shown:
        counts[item.lane] = counts.get(item.lane, 0) + 1
    chips = [f'<button type="button" class="chip-filter" data-lane="" aria-pressed="true">'
             f'{e(site["filter_all"])} <b>{len(shown)}</b></button>']
    chips += [f'<button type="button" class="chip-filter" data-lane="{e(lane["id"])}" aria-pressed="false" '
              f'style="--lane:{lane_color(config, lane["id"])}">{e(short_lane(config.lane_name(lane["id"], lang)))} '
              f'<b>{counts[lane["id"]]}</b></button>'
              for lane in config.lanes if lane["id"] in counts]
    filters = (f'<div class="filters" role="group" aria-label="{e(site["filter_label"])}">{"".join(chips)}</div>'
               if len(counts) > 1 else "")
    return (f'<div class="toolbar">{filters}<div class="tools">'
            f'<button type="button" class="button digest" data-done="{e(site["digest_copied"])}">'
            f'{e(site["copy_digest"])}</button>'
            f'<button type="button" class="button primary poster">{e(site["poster"])}</button></div></div>')


def poster_dialog(config: Config, lang: str, day: str) -> str:
    site = config.ui(lang)["site"]
    return f"""<dialog class="poster-dialog" aria-labelledby="poster-title">
<div class="poster-head"><h2 id="poster-title">{e(site['poster_title'])}</h2><button type="button" class="close" aria-label="{e(site['close'])}">×</button></div>
<img alt="{e(site['poster_title'])}" width="1080" height="1440">
<p class="hint">{e(site['poster_hint'])}</p>
<p class="poster-actions"><a class="button primary download" download="physical-ai-radar-{day}.{lang}.png">{e(site['download'])}</a><button type="button" class="button share-image" hidden>{e(site['share_image'])}</button></p>
</dialog>"""


def render_landing(config: Config, lang: str, ctx: dict[str, Any]) -> str:
    ui, site, base, day = config.ui(lang), config.ui(lang)["site"], prefix(lang), ctx["date"]
    picked = ctx["picked"]
    shown = picked or ctx["baseline"]
    days = ctx.get("chart_days") or 7
    fetch = ctx.get("fetch") or {}
    week_total = sum(count for _, count in ctx["lane_rows"])
    title = f"{ui['title']} · {day}"
    description = ui["tagline"]
    if picked:
        description = f"{site['picks_heading'].format(day=day)}: " + " · ".join(item.title for item in picked[:3])
    stats = [(len(picked), site["stat_picks"]), (fetch.get("items", "—"), site["stat_scanned"]),
             (f"{fetch.get('answered', '—')}/{fetch.get('attempted', '—')}", site["stat_sources"]),
             (week_total, site["stat_week"].format(days=days))]
    hero_text = site["share_text"].format(day=day, title=shown[0].title if shown else ui["title"])
    heading = site["picks_heading"].format(day=day) if picked else site["baseline_heading"]
    cards = "\n".join(card(config, lang, ctx, item, rank) for rank, item in enumerate(shown, 1))
    return f"""<!doctype html>
<html lang="{HTML_LANG[lang]}">
<head>
{head(config, lang, ctx, title, description)}
</head>
<body>
<a class="skip" href="#main">{e(heading)}</a>
{nav(config, lang, day)}
<header class="hero">
<div class="pitch">
<p class="eyebrow"><span class="live"></span>{e(site['eyebrow'].format(day=day))}</p>
<h1>{e(site['headline'])}</h1>
<p class="tagline">{e(ui['tagline'])}</p>
<p class="cta"><a class="button primary" href="{base}radar/daily/{day}.{lang}.html">{e(site['cta_today'])} →</a><a class="button" href="{base}radar/{atom_name(lang)}">{e(site['cta_feed'])}</a><a class="button" href="{HOME_URL}">★ {e(site['cta_star'])}</a><button type="button" class="button share" data-url="{e(daily_url(day, lang))}" data-text="{e(hero_text)}" data-done="{e(site['copied'])}">{e(site['share_today'])}</button></p>
<dl class="stats">{''.join(f'<div><dt>{e(label)}</dt><dd>{e(value)}</dd></div>' for value, label in stats)}</dl>
</div>
<figure class="scope">
{radar_svg(config, lang, ctx['lane_rows'], picked)}
{lane_legend(config, lang, ctx['lane_rows'])}
<figcaption>{e(site['radar_caption'].format(days=days))}</figcaption>
</figure>
</header>
<main id="main">
<section class="picks" id="picks">
<h2>{e(heading)}</h2>
<p class="legend">{e(ui['evidence_legend'])}</p>
{toolbar(config, lang, shown)}
<div class="grid">
{cards}
</div>
</section>
<section class="trend">
<div><h2>{e(site['trend_heading'])}</h2>{cadence_svg(ctx['cadence'])}</div>
<div><h2>{e(chart_heading(ui, days))}</h2>{mix_bar(config, lang, ctx['mix'])}</div>
<div class="scan"><h2>{e(site['scan_heading'])}</h2>{qr_svg(daily_url(day, lang), site['scan_heading'])}<p>{e(site['scan_hint'])}</p></div>
</section>
</main>
<footer class="foot">
<p>{e(ui['disclaimer'])}</p>
<p><a href="{HOME_URL}/blob/main/docs/METHODOLOGY.md">{e(ui['methodology'])}</a> · <a href="{base}radar/INDEX.html">{e(ui['history'])}</a> · <a href="{base}radar/feed.json">JSON Feed</a> · {e(site['license'])} · {e(ui['generated'])} {e(ctx['generated'])}</p>
</footer>
{poster_dialog(config, lang, day)}
<script type="application/json" id="share-data">{json_script(share_data(config, lang, ctx, shown))}</script>
<script src="{base}assets/site.js" defer></script>
</body>
</html>
"""


def chart_heading(ui: dict[str, Any], days: int) -> str:
    return f"{ui['source_mix']} ({ui['chart_window'].format(days=days)})"


def write_site(config: Config, ctx: dict[str, Any], root: Path = ROOT) -> list[Path]:
    written = []
    for lang in LANGS:
        path = root/PAGES[lang]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_landing(config, lang, ctx), encoding="utf-8")
        written.append(path)
    return written


def og_card(config: Config, lang: str, lane_rows: list[tuple[str, int]]) -> str:
    """The 1200×630 social card, as a page for a headless browser to photograph."""
    ui, site = config.ui(lang), config.ui(lang)["site"]
    css = (ROOT/"assets"/"site.css").read_text(encoding="utf-8")
    return f"""<!doctype html><html lang="{HTML_LANG[lang]}"><head><meta charset="utf-8">
<style>{css}</style></head>
<body class="og"><div class="og-card">
<div class="og-text"><p class="eyebrow"><span class="live"></span>{e(ui['title'])}</p>
<h1>{e(site['headline'])}</h1><p class="tagline">{e(site['og_tagline'])}</p>
<p class="og-url">noteflowai.github.io/physical-ai-radar</p></div>
{radar_svg(config, lang, lane_rows, [], radius=150, labels=False)}
</div></body></html>"""


def render_og(config: Config, root: Path = ROOT) -> list[Path]:
    browser = next((path for name in ("google-chrome", "chromium", "chromium-browser")
                    if (path := shutil.which(name))), None)
    if not browser:
        raise SystemExit("rendering the social card needs Chrome or Chromium on PATH")
    snapshot = json.loads((root/"radar"/"latest.json").read_text(encoding="utf-8"))
    lane_rows = list(snapshot.get("lane_counts", {}).items())
    written = []
    with tempfile.TemporaryDirectory() as tmp:
        for lang in LANGS:
            page = Path(tmp)/f"og.{lang}.html"
            page.write_text(og_card(config, lang, lane_rows), encoding="utf-8")
            target = root/"assets"/f"og.{lang}.png"
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--window-size={OG_SIZE[0]},{OG_SIZE[1]}", f"--screenshot={target}",
                            page.as_uri()], check=True, capture_output=True, timeout=120)
            written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the social card for the Pages site.")
    parser.add_argument("--og", action="store_true", help="photograph og_card into assets/og.<lang>.png")
    args = parser.parse_args(argv)
    if not args.og:
        parser.print_help()
        return 1
    for path in render_og(load_config()):
        print(f"[site] wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
