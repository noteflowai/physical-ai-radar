"""Fetch layer: arXiv API plus Atom/RSS feeds, standard library only.

Design rules:
- every network call is timeout-bounded and failure-tolerant; one dead source must
  never fail the daily run;
- no HTML scraping, only declared machine-readable endpoints;
- output is normalised into `Item` objects so downstream code never sees feed quirks.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import Config, Item

USER_AGENT = "physical-ai-radar/1.0 (+https://github.com/noteflowai/physical-ai-radar)"
ATOM = "{http://www.w3.org/2005/Atom}"
TIMEOUT = 25
# arXiv asks API callers to leave about three seconds between requests; six
# queries fired back to back is exactly the burst that earns a rate-limit reply.
ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
ARXIV_DELAY = 3.0
RETRIES = 2
RETRY_DELAY = 2.0

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


def http_get(url: str, retries: int = RETRIES) -> bytes | None:
    """Fetch a URL, retrying transient failures; never raise into the caller.

    Feeds and the arXiv API both fail intermittently, and a single timeout or
    rate-limit reply used to drop a whole source for the day on first contact.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                print(f"[fetch] retry {url}: {exc}")
                time.sleep(RETRY_DELAY)
    print(f"[fetch] skip {url}: {last}")
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


def fetch_arxiv(config: Config) -> tuple[list[Item], bool]:
    """Query the arXiv Atom API for each configured search string.

    Returns the items and whether any query answered, so a total arXiv outage is
    reported rather than looking like a quiet day.
    """
    cfg = config.sources.get("arxiv", {})
    if not cfg.get("enabled", False):
        return [], True
    per_query = max(1, int(cfg.get("max_results", 60)) // max(1, len(cfg.get("queries", []))))
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=int(cfg.get("lookback_days", 3)))
    items: list[Item] = []
    failures = 0
    for index, query in enumerate(cfg.get("queries", [])):
        if index:
            time.sleep(ARXIV_DELAY)
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": per_query,
            }
        )
        payload = http_get(f"{ARXIV_ENDPOINT}?{params}")
        if not payload:
            failures += 1
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
    queries = len(cfg.get("queries", []))
    if failures:
        print(f"[fetch] arxiv: {failures}/{queries} queries unanswered")
    return items, failures < queries


def link_digest(link: str) -> str:
    """Stable short id for a feed entry.

    Python randomises str.hash per process, so hashing the link with the builtin
    produced a different id on every run: the same article could never be
    recognised across days, and radar/latest.json churned even when nothing
    changed. sha1 here is an identity, not a security boundary.
    """
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


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


def fetch_feeds(config: Config) -> tuple[list[Item], list[str]]:
    """Read every configured feed, collecting the ids of those that did not answer."""
    items: list[Item] = []
    failed: list[str] = []
    for feed in config.sources.get("feeds", []):
        payload = http_get(feed["url"])
        if not payload:
            failed.append(feed["id"])
            continue
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            print(f"[fetch] feed parse error {feed['id']}: {exc}")
            failed.append(feed["id"])
            continue
        for title, link, summary, published in _feed_entries(root):
            if not title or not link:
                continue
            items.append(
                Item(
                    id=f"{feed['id']}:{link_digest(link)}",
                    title=title,
                    url=link,
                    publisher=feed.get("publisher", feed["id"]),
                    source_id=feed["id"],
                    evidence=feed.get("evidence", "M"),
                    published=published,
                    summary=summary,
                )
            )
    return items, failed


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
                # A curated lane is an editorial decision; the classifier must not
                # overrule it, and several entries carry no lane keyword at all.
                lane_locked="lane" in raw,
                numbers=list(raw.get("numbers", [])),
            )
        )
    return items


@dataclass
class FetchReport:
    """What the network round actually returned, so degradation stays visible."""

    items: list[Item] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def answered(self) -> list[str]:
        return [source for source in self.attempted if source not in self.failed]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.source_id] = counts.get(item.source_id, 0) + 1
        return {
            "items": len(self.items),
            "attempted": len(self.attempted),
            "answered": len(self.answered),
            "failed": sorted(self.failed),
            "per_source": counts,
        }


def fetch_all(config: Config, offline: bool = False) -> FetchReport:
    """Collect live items. In offline mode nothing is fetched (used by CI and tests)."""
    if offline:
        print("[fetch] offline mode: skipping network sources")
        return FetchReport()
    report = FetchReport()
    if config.sources.get("arxiv", {}).get("enabled", False):
        report.attempted.append("arxiv")
        arxiv_items, answered = fetch_arxiv(config)
        report.items.extend(arxiv_items)
        if not answered:
            report.failed.append("arxiv")
    feed_items, failed = fetch_feeds(config)
    report.attempted.extend(feed["id"] for feed in config.sources.get("feeds", []))
    report.items.extend(feed_items)
    report.failed.extend(failed)
    print(f"[fetch] collected {len(report.items)} raw items from "
          f"{len(report.answered)}/{len(report.attempted)} sources")
    if report.failed:
        print(f"[fetch] no answer from: {', '.join(sorted(report.failed))}")
    return report


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
