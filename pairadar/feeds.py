"""Subscribable output: a JSON Feed, Atom feeds per language and weekly roundups.

The radar is published once a day into a Git repository, which is a poor thing to
follow unless you already watch the repository. These outputs let a reader subscribe
in any feed reader, and let someone who does not want a daily digest read one page a
week.

`radar/feed.json` is both the published JSON Feed 1.1 document and the store. Each
item carries a `_radar` extension object (JSON Feed reserves `_`-prefixed keys for
this) with everything needed to render the Atom feeds and the weekly pages in any
language, so no other state file is needed. A run replaces that day's entries, so
re-running a day never duplicates it.

The feeds carry what the pipeline decided -- title, lane, evidence, numbers, signals,
the source excerpt -- and link to the day's page for the "why it matters" line.
Keeping that line out has two effects. A drafted line, which must always be shown
with its label, never appears unlabelled in someone's reader. And `--rerender`, which
only rewrites the day's pages, never has to touch the feeds.
"""
from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import LANGS, README_FILES, ROOT, Config, Item, dump_json, md_text, md_url
from .distill import one_liner, url_key

REPO = "noteflowai/physical-ai-radar"
HOME_URL = f"https://github.com/{REPO}"
# GitHub Pages serves the committed feeds from main with XML and JSON content
# types and a CDN in front, which raw.githubusercontent.com does not.
SITE_URL = "https://noteflowai.github.io/physical-ai-radar"
TAG_PREFIX = "tag:noteflowai.github.io,2026:physical-ai-radar"
JSON_FEED_VERSION = "https://jsonfeed.org/version/1.1"
ATOM_NS = "http://www.w3.org/2005/Atom"
XML_NS = "http://www.w3.org/XML/1998/namespace"

FEED_JSON = "feed.json"
KEEP_DAYS = 30
KEEP_ITEMS = 100


def atom_name(lang: str) -> str:
    """English is the unsuffixed default, the name readers try first."""
    return "feed.xml" if lang == "en" else f"feed.{lang}.xml"


def page_url(day: str, lang: str) -> str:
    return f"{HOME_URL}/blob/main/radar/daily/{day}.{lang}.md"


def entry_id(item_id: str) -> str:
    """A tag URI (RFC 4151) from the content-derived item id: stable across runs."""
    return f"{TAG_PREFIX}:{item_id}"


def rfc3339(generated: str) -> str:
    """"2026-09-25 01:40 UTC" -> "2026-09-25T01:40:00Z"."""
    try:
        moment = datetime.strptime(generated, "%Y-%m-%d %H:%M UTC")
    except ValueError:
        moment = datetime.fromisoformat(generated[:10])
    return moment.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_week(day: str) -> tuple[str, date, date]:
    """("2026-W39", Monday, Sunday) for the ISO week that holds `day`."""
    moment = date.fromisoformat(day)
    year, week, weekday = moment.isocalendar()
    monday = moment - timedelta(days=weekday - 1)
    return f"{year}-W{week:02d}", monday, monday + timedelta(days=6)


# --- the store ---------------------------------------------------------------------

def load_store(path: Path) -> list[dict[str, Any]]:
    """The items of a previously written feed.json; anything unreadable starts fresh."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if payload.get("version") != JSON_FEED_VERSION:
        return []
    return [entry["_radar"] for entry in payload.get("items", []) if isinstance(entry.get("_radar"), dict)]


def record(item: Item, day: str, published: str) -> dict[str, Any]:
    """The language-neutral part of an entry, as stored under `_radar`."""
    return {
        "date": day,
        "id": item.id,
        "url": item.url,
        "title": item.title,
        "publisher": item.publisher or item.source_id,
        "source_id": item.source_id,
        "evidence": item.evidence,
        "lane": item.lane,
        "published": item.published,
        "radar_published": published,
        "numbers": list(item.numbers),
        "signals": list(item.signals),
        "excerpt": one_liner(item.summary) if item.source_id != "baseline" else "",
    }


def upsert(store: Iterable[dict[str, Any]], picked: Iterable[Item], day: str, generated: str,
           keep_days: int = KEEP_DAYS, keep_items: int = KEEP_ITEMS) -> list[dict[str, Any]]:
    """Replace `day`'s records with today's picks, newest first, trimmed.

    A link already delivered on another day is not delivered again: the repeat guard
    only covers seven days, and a reader should not see the same item twice within
    the feed's thirty.
    """
    kept = [entry for entry in store if entry["date"] != day]
    delivered = {url_key(entry["url"]) for entry in kept}
    published = rfc3339(generated)
    fresh = []
    for item in picked:
        key = url_key(item.url)
        if key not in delivered:
            delivered.add(key)
            fresh.append(record(item, day, published))
    horizon = (date.fromisoformat(day) - timedelta(days=keep_days)).isoformat()
    # Stable sort: within a day the shortlist's rank order survives.
    merged = sorted(fresh + kept, key=lambda entry: entry["date"], reverse=True)
    return [entry for entry in merged if entry["date"] > horizon][:keep_items]


# --- rendering ---------------------------------------------------------------------

def _evidence(entry: dict[str, Any]) -> str:
    return f"[{entry['evidence']}]"


def _facts(config: Config, lang: str, entry: dict[str, Any]) -> list[tuple[str, str]]:
    ui = config.ui(lang)
    facts = [
        (ui["lane"], config.lane_name(entry["lane"], lang)),
        (ui["evidence"], _evidence(entry)),
        (ui["source"], f"{entry['publisher']} · {entry['published']}"),
    ]
    if entry["numbers"]:
        facts.append((ui["numbers"], " · ".join(entry["numbers"])))
    if entry["signals"]:
        facts.append((ui["signals_label"],
                      " · ".join(ui["signals"].get(signal, signal) for signal in entry["signals"])))
    return facts


def content_html(config: Config, lang: str, entry: dict[str, Any]) -> str:
    ui = config.ui(lang)
    parts = ["<p>" + "<br>".join(f"<b>{html.escape(label)}</b>: {html.escape(value)}"
                                 for label, value in _facts(config, lang, entry)) + "</p>"]
    if entry["excerpt"]:
        parts.append(f"<blockquote>{html.escape(entry['excerpt'])}</blockquote>")
    link = html.escape(page_url(entry["date"], lang), quote=True)
    parts.append(f'<p><a href="{link}">{html.escape(ui["day_radar"].format(day=entry["date"]))}</a></p>')
    return "\n".join(parts)


def content_text(config: Config, lang: str, entry: dict[str, Any]) -> str:
    ui = config.ui(lang)
    lines = [f"{label}: {value}" for label, value in _facts(config, lang, entry)]
    if entry["excerpt"]:
        lines.extend(["", entry["excerpt"]])
    lines.extend(["", f"{ui['day_radar'].format(day=entry['date'])}: {page_url(entry['date'], lang)}"])
    return "\n".join(lines)


def json_feed(config: Config, store: list[dict[str, Any]], lang: str = "en") -> dict[str, Any]:
    ui = config.ui(lang)
    items = []
    for entry in store:
        items.append({
            "id": entry_id(entry["id"]),
            "url": entry["url"],
            "title": entry["title"],
            "content_html": content_html(config, lang, entry),
            "content_text": content_text(config, lang, entry),
            "summary": entry["excerpt"] or entry["title"],
            "date_published": entry["radar_published"],
            "authors": [{"name": entry["publisher"]}],
            "tags": [config.lane_name(entry["lane"], lang), _evidence(entry)],
            "language": lang,
            "_radar": entry,
        })
    return {
        "version": JSON_FEED_VERSION,
        "title": ui["title"],
        "home_page_url": HOME_URL,
        "feed_url": f"{SITE_URL}/radar/{FEED_JSON}",
        "description": ui["tagline"],
        "language": lang,
        "authors": [{"name": ui["title"], "url": HOME_URL}],
        "items": items,
    }


def atom_feed(config: Config, store: list[dict[str, Any]], lang: str, generated: str) -> str:
    """An RFC 4287 feed. Titles are never translated; the labels around them are."""
    ui = config.ui(lang)
    ET.register_namespace("", ATOM_NS)

    def sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        node = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        if text is not None:
            node.text = text
        return node

    feed = ET.Element(f"{{{ATOM_NS}}}feed", {f"{{{XML_NS}}}lang": lang})
    sub(feed, "id", f"{TAG_PREFIX}:feed:{lang}")
    sub(feed, "title", ui["title"])
    sub(feed, "subtitle", ui["tagline"])
    sub(feed, "updated", store[0]["radar_published"] if store else rfc3339(generated))
    sub(feed, "link", rel="self", type="application/atom+xml", href=f"{SITE_URL}/radar/{atom_name(lang)}")
    sub(feed, "link", rel="alternate", type="text/html", href=f"{HOME_URL}/blob/main/{README_FILES[lang]}")
    author = sub(feed, "author")
    sub(author, "name", ui["title"])
    sub(author, "uri", HOME_URL)
    sub(feed, "generator", "pairadar", uri=HOME_URL)
    for entry in store:
        node = sub(feed, "entry")
        sub(node, "id", entry_id(entry["id"]))
        sub(node, "title", entry["title"])
        sub(node, "link", rel="alternate", href=entry["url"])
        sub(node, "link", rel="related", type="text/html", href=page_url(entry["date"], lang))
        sub(node, "published", entry["radar_published"])
        sub(node, "updated", entry["radar_published"])
        writer = sub(node, "author")
        sub(writer, "name", entry["publisher"])
        sub(node, "category", term=entry["lane"], label=config.lane_name(entry["lane"], lang))
        sub(node, "category", term=entry["evidence"], label=_evidence(entry))
        if entry["excerpt"]:
            sub(node, "summary", entry["excerpt"], type="text")
        sub(node, "content", content_html(config, lang, entry), type="html")
    ET.indent(feed)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


WEEKLY_SWITCH = {
    "zh": "语言：中文 · [EN](./{stem}.en.md) · [JA](./{stem}.ja.md)",
    "en": "Language: [ZH](./{stem}.zh.md) · EN · [JA](./{stem}.ja.md)",
    "ja": "言語：[ZH](./{stem}.zh.md) · [EN](./{stem}.en.md) · 日本語",
}


def render_weekly(config: Config, lang: str, day: str, store: list[dict[str, Any]]) -> str:
    """One page per ISO week: every pick published that week, grouped by lane."""
    ui = config.ui(lang)
    stem, monday, sunday = iso_week(day)
    week = [entry for entry in store if monday.isoformat() <= entry["date"] <= sunday.isoformat()]
    mix = {tag: sum(1 for entry in week if entry["evidence"] == tag) for tag in ("O", "R", "M")}
    lines = [
        f"# {ui['title']} · {ui['weekly']} · {stem}",
        "",
        WEEKLY_SWITCH[lang].format(stem=stem),
        "",
        f"> {ui['weekly_intro'].format(count=len(week), start=monday.isoformat(), end=sunday.isoformat())}",
        "",
        f"- {ui['evidence_legend']}",
        f"- `[O]` {mix['O']} · `[R]` {mix['R']} · `[M]` {mix['M']}",
        "",
    ]
    if not week:
        lines.extend([ui["no_items"], ""])
    for lane in config.lanes:
        rows = sorted((entry for entry in week if entry["lane"] == lane["id"]),
                      key=lambda entry: entry["date"])
        if not rows:
            continue
        lines.extend([f"## {config.lane_name(lane['id'], lang)} ({len(rows)})", ""])
        for entry in rows:
            numbers = f" ｜ {' · '.join(f'`{n}`' for n in entry['numbers'][:3])}" if entry["numbers"] else ""
            lines.append(
                f"- `{_evidence(entry)}` **[{md_text(entry['title'])}]({md_url(entry['url'])})** — "
                f"{md_text(entry['publisher'])}"
                f"{numbers} ｜ [{entry['date']}](../daily/{entry['date']}.{lang}.md)"
            )
        lines.append("")
    lines.extend([
        "---",
        "",
        f"*{ui['disclaimer']}*",
        "",
        f"[{ui['methodology']}](../../docs/METHODOLOGY.md) · [{ui['history']}](../INDEX.md)",
        "",
    ])
    return "\n".join(lines)


def write_feeds(config: Config, ctx: dict[str, Any], root: Path = ROOT,
                source: Path = ROOT) -> list[Path]:
    """Upsert the day into the store read from `source`, then write every feed and
    the day's weekly pages under `root`."""
    store = upsert(load_store(source/"radar"/FEED_JSON), ctx["picked"], ctx["date"], ctx["generated"])
    radar = root/"radar"
    written = [radar/FEED_JSON]
    dump_json(written[0], json_feed(config, store))
    for lang in LANGS:
        path = radar/atom_name(lang)
        path.write_text(atom_feed(config, store, lang, ctx["generated"]), encoding="utf-8")
        written.append(path)
    stem, _, _ = iso_week(ctx["date"])
    weekly = radar/"weekly"
    weekly.mkdir(parents=True, exist_ok=True)
    for lang in LANGS:
        path = weekly/f"{stem}.{lang}.md"
        path.write_text(render_weekly(config, lang, ctx["date"], store), encoding="utf-8")
        written.append(path)
    return written


def weekly_stems(root: Path = ROOT) -> list[str]:
    """Published weeks under `root`, newest first."""
    return sorted({path.name.split(".")[0] for path in (root/"radar"/"weekly").glob("*-W*.en.md")},
                  reverse=True)
