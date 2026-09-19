"""Fetch layer: arXiv API plus Atom/RSS feeds, standard library only.

Design rules:
- every network call is timeout-bounded and failure-tolerant; one dead source must
  never fail the daily run;
- no HTML scraping, only declared machine-readable endpoints;
- output is normalised into `Item` objects so downstream code never sees feed quirks.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import Config, Item

USER_AGENT = "physical-ai-radar/1.0 (+https://github.com/noteflowai/physical-ai-radar)"
ATOM = "{http://www.w3.org/2005/Atom}"
TIMEOUT = 25

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def clean_text(raw: str | None) -> str:
    """Strip markup and collapse whitespace so summaries stay single-line safe."""
    if not raw:
        return ""
    text = _TAGS.sub(" ", raw)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _WS.sub(" ", text).strip()


def http_get(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"[fetch] skip {url}: {exc}")
        return None


def parse_date(raw: str | None) -> str:
    """Normalise common feed date formats to YYYY-MM-DD; fall back to today."""
    if raw:
        candidate = raw.strip()
        formats = (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
        )
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", candidate)
        if match:
            return "-".join(match.groups())
    return datetime.now(timezone.utc).date().isoformat()


def fetch_arxiv(config: Config) -> list[Item]:
    """Query the arXiv Atom API for each configured search string."""
    cfg = config.sources.get("arxiv", {})
    if not cfg.get("enabled", False):
        return []
    per_query = max(1, int(cfg.get("max_results", 60)) // max(1, len(cfg.get("queries", []))))
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=int(cfg.get("lookback_days", 3)))
    items: list[Item] = []
    for query in cfg.get("queries", []):
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": per_query,
            }
        )
        payload = http_get(f"http://export.arxiv.org/api/query?{params}")
        if not payload:
            continue
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            print(f"[fetch] arxiv parse error: {exc}")
            continue
        for entry in root.findall(f"{ATOM}entry"):
            arxiv_id = clean_text(entry.findtext(f"{ATOM}id"))
            title = clean_text(entry.findtext(f"{ATOM}title"))
            summary = clean_text(entry.findtext(f"{ATOM}summary"))
            published = parse_date(entry.findtext(f"{ATOM}published"))
            if not title or not arxiv_id:
                continue
            if datetime.fromisoformat(published).date() < cutoff:
                continue
            short_id = arxiv_id.rstrip("/").split("/")[-1]
            items.append(
                Item(
                    id=f"arxiv:{short_id}",
                    title=title,
                    url=arxiv_id,
                    publisher=f"arXiv {short_id}",
                    source_id="arxiv",
                    evidence=cfg.get("evidence", "R"),
                    published=published,
                    summary=summary,
                )
            )
    return items


def _feed_entries(root: ET.Element) -> Iterable[tuple[str, str, str, str]]:
    """Yield (title, url, summary, date) for Atom or RSS 2.0 documents."""
    entries = root.findall(f"{ATOM}entry")
    if entries:
        for entry in entries:
            link = ""
            for candidate in entry.findall(f"{ATOM}link"):
                rel = candidate.get("rel", "alternate")
                if rel == "alternate":
                    link = candidate.get("href", "")
                    break
            summary = entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content") or ""
            published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
            yield (
                clean_text(entry.findtext(f"{ATOM}title")),
                link,
                clean_text(summary),
                parse_date(published),
            )
        return
    for item in root.findall(".//item"):
        summary = item.findtext("description") or ""
        yield (
            clean_text(item.findtext("title")),
            clean_text(item.findtext("link")),
            clean_text(summary),
            parse_date(item.findtext("pubDate")),
        )


def fetch_feeds(config: Config) -> list[Item]:
    items: list[Item] = []
    for feed in config.sources.get("feeds", []):
        payload = http_get(feed["url"])
        if not payload:
            continue
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            print(f"[fetch] feed parse error {feed['id']}: {exc}")
            continue
        for title, link, summary, published in _feed_entries(root):
            if not title or not link:
                continue
            items.append(
                Item(
                    id=f"{feed['id']}:{abs(hash(link)) % (10**10)}",
                    title=title,
                    url=link,
                    publisher=feed.get("publisher", feed["id"]),
                    source_id=feed["id"],
                    evidence=feed.get("evidence", "M"),
                    published=published,
                    summary=summary,
                )
            )
    return items


def baseline_items(config: Config) -> list[Item]:
    """Curated landmark entries; they ship with the repo and need no network."""
    items: list[Item] = []
    for raw in config.baseline.get("items", []):
        items.append(
            Item(
                id=f"baseline:{raw['id']}",
                title=raw["title"],
                url=raw["url"],
                publisher=raw.get("publisher", ""),
                source_id="baseline",
                evidence=raw.get("evidence", "M"),
                published=str(raw.get("date", "")),
                summary=raw.get("why", {}).get("en", ""),
                lane=raw.get("lane", "foundation"),
                numbers=list(raw.get("numbers", [])),
            )
        )
    return items


def fetch_all(config: Config, offline: bool = False) -> list[Item]:
    """Collect live items. In offline mode nothing is fetched (used by CI and tests)."""
    if offline:
        print("[fetch] offline mode: skipping network sources")
        return []
    items = fetch_arxiv(config) + fetch_feeds(config)
    print(f"[fetch] collected {len(items)} raw items")
    return items


def source_weight(config: Config, source_id: str) -> float:
    if source_id == "arxiv":
        return float(config.sources.get("arxiv", {}).get("weight", 1.0))
    for feed in config.sources.get("feeds", []):
        if feed["id"] == source_id:
            return float(feed.get("weight", 1.0))
    return 1.0


def metadata(config: Config) -> dict[str, Any]:
    feeds: list[dict[str, Any]] = list(config.sources.get("feeds", []))
    return {
        "feed_count": len(feeds),
        "arxiv_queries": len(config.sources.get("arxiv", {}).get("queries", [])),
    }
