"""Fetch layer: arXiv API plus Atom/RSS feeds, standard library only.

Design rules:
- every network call is timeout-bounded and failure-tolerant; one dead source must
  never fail the daily run;
- no HTML scraping, only declared machine-readable endpoints;
- output is normalised into `Item` objects so downstream code never sees feed quirks.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

from .config import Config, Item

USER_AGENT = "physical-ai-radar/1.0 (+https://github.com/noteflowai/physical-ai-radar)"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
TIMEOUT = 25
# arXiv asks API callers to leave about three seconds between requests; six
# queries fired back to back is exactly the burst that earns a rate-limit reply.
ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
ARXIV_RSS = "https://rss.arxiv.org/rss"
ARXIV_DELAY = 3.0
# Every category-feed description opens with "arXiv:2609.01234v1 Announce Type: new
# Abstract:". The announce type decides whether the paper is new at all, and the rest
# is boilerplate that would otherwise lead every excerpt in the digest.
_ANNOUNCE = re.compile(r"^\s*arXiv:\S+\s+Announce Type:\s*(\S+)\s*(?:Abstract:\s*)?", re.I)
RETRIES = 2
# At least arXiv's spacing, so a retry is never the burst that earned the refusal.
RETRY_DELAY = 3.0
MAX_RETRY_AFTER = 30.0
# A 4xx other than these is an answer, not a hiccup: arXiv's 406 comes back the same
# on every attempt, and retrying it only doubled the requests that earned it.
RETRYABLE_STATUS = {408, 425, 429}
# Trade-press feeds ship the whole article as the description -- IEEE Spectrum's run to
# 8,000 characters -- while official blogs send a paragraph or nothing. Scoring every
# keyword in a full article let a "Video Friday" round-up outscore a focused post, and
# stored the article in radar/latest.json. About an arXiv abstract's length is kept.
MAX_SUMMARY_CHARS = 2000
# A Chinese or Japanese character carries about what three English letters do, so the
# same number of characters would hold three times the keywords.
CJK_SUMMARY_CHARS = MAX_SUMMARY_CHARS // 3

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def clean_text(raw: str | None) -> str:
    """Strip markup, decode entities once and collapse whitespace.

    Tags go first, then entities: decoding first turned an escaped `&lt;details&gt;`
    -- text about a tag -- into a tag, and it was stripped or, worse, published. The
    result is plain text; whatever writes it into Markdown escapes it there.
    """
    if not raw:
        return ""
    text = html.unescape(_TAGS.sub(" ", raw))
    return _WS.sub(" ", text).strip()


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        wait = float((exc.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return RETRY_DELAY
    return min(max(wait, RETRY_DELAY), MAX_RETRY_AFTER)


def http_get(url: str, retries: int = RETRIES) -> bytes | None:
    """Fetch a URL, retrying transient failures; never raise into the caller.

    Feeds and the arXiv API both fail intermittently, and a single timeout or
    rate-limit reply used to drop a whole source for the day on first contact.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        delay = RETRY_DELAY
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if 400 <= exc.code < 500 and exc.code not in RETRYABLE_STATUS:
                break
            delay = _retry_after(exc)
        # http.client raises its own family for a garbled status line or a body cut
        # short, and none of it is an OSError; one such reply used to end the whole run.
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
                ValueError) as exc:
            last = exc
        if attempt < retries:
            print(f"[fetch] retry {url}: {last}")
            time.sleep(delay)
    print(f"[fetch] skip {url}: {last}")
    return None


def _utc_day(moment: datetime) -> str:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.date().isoformat()


def parse_date(raw: str | None) -> str:
    """Normalise a feed date to its UTC day, YYYY-MM-DD; "" when it cannot be read.

    RFC 822 dates come in many shapes -- named zones such as PST, no seconds, no
    weekday -- and an unreadable one used to become today: a post from August was
    published as fresh news with full recency. An empty date is dropped by prefilter
    instead. Offsets are folded to UTC, the zone every window on the page is stated in.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    try:
        return _utc_day(datetime.fromisoformat(re.sub(r"Z$", "+00:00", candidate)))
    except ValueError:
        pass
    try:
        return _utc_day(parsedate_to_datetime(candidate))
    except (TypeError, ValueError, IndexError):
        pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", candidate)
    if match:
        try:
            return date(*map(int, match.groups())).isoformat()
        except ValueError:
            return ""
    return ""


_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?$")


def arxiv_identity(link: str) -> tuple[str, str]:
    """(versionless id, canonical abstract URL) for an arXiv link.

    The API reports `http://arxiv.org/abs/2609.01234v1`, the category feeds
    `https://arxiv.org/abs/2609.01234`. Both paths now name a paper the same way, so
    its id and link are stable whichever one answered that day.
    """
    tail = link.rstrip("/").split("/")[-1]
    match = _ARXIV_ID.search(tail)
    short_id = match.group(1) if match else tail
    return short_id, f"https://arxiv.org/abs/{short_id}"


def fetch_arxiv(config: Config, reference: date | None = None,
                report: "FetchReport | None" = None) -> tuple[list[Item], bool]:
    """Query the arXiv Atom API for each configured search string.

    Returns the items and whether any query answered, so a total arXiv outage is
    reported rather than looking like a quiet day. When the category feeds stand in
    for the API, `report.fallback` says so: the day is served, but the API is down.

    The cutoff counts back from `reference`, the day being generated, so a rerun of
    an earlier day looks at that day's papers rather than today's.
    """
    cfg = config.sources.get("arxiv", {})
    if not cfg.get("enabled", False):
        return [], True
    per_query = max(1, int(cfg.get("max_results", 60)) // max(1, len(cfg.get("queries", []))))
    reference = reference or datetime.now(timezone.utc).date()
    cutoff = reference - timedelta(days=int(cfg.get("lookback_days", 3)))
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
            # A rate-limit or maintenance page comes back as HTML with a 200; it is
            # no answer, and counting it as one kept the category feeds from taking over.
            print(f"[fetch] arxiv parse error: {exc}")
            failures += 1
            continue
        # The API reports a bad query as an entry whose id is .../api/errors#...,
        # and a query sorted by submission date always has recent papers: an empty
        # or error-only reply is a failure, not a quiet day.
        entries = [entry for entry in root.findall(f"{ATOM}entry")
                   if "/abs/" in (entry.findtext(f"{ATOM}id") or "")]
        if not entries:
            print(f"[fetch] arxiv: query {index + 1} returned no papers")
            failures += 1
            continue
        for entry in entries:
            arxiv_id = clean_text(entry.findtext(f"{ATOM}id"))
            title = clean_text(entry.findtext(f"{ATOM}title"))
            summary = clean_text(entry.findtext(f"{ATOM}summary"))
            published = parse_date(entry.findtext(f"{ATOM}published"))
            if not title or not arxiv_id or not published:
                continue
            if date.fromisoformat(published) < cutoff:
                continue
            short_id, link = arxiv_identity(arxiv_id)
            items.append(
                Item(
                    id=f"arxiv:{short_id}",
                    title=title,
                    url=link,
                    publisher=f"arXiv {short_id}",
                    source_id="arxiv",
                    evidence=cfg.get("evidence", "R"),
                    published=published,
                    summary=summary,
                    # Every configured query is scoped to cat:cs.RO.
                    topical=True,
                )
            )
    queries = len(cfg.get("queries", []))
    if failures:
        print(f"[fetch] arxiv: {failures}/{queries} queries unanswered")
    if queries and failures == queries:
        # The keyword API is gone from this host's point of view. Category feeds carry
        # no query, so precision now comes from the lane keywords downstream, which
        # already require a hit before anything is picked.
        rss_items, rss_answered = fetch_arxiv_rss(cfg, cutoff)
        if rss_answered:
            # A feed that answers with nothing is arXiv being closed, not arXiv being
            # broken: it declares skipDays for Saturday and Sunday. Reporting a weekend
            # as a failed source would make health.py cry outage every week.
            print(f"[fetch] arxiv: {len(rss_items)} items from the category feeds instead")
            if report is not None:
                report.fallback.append("arxiv")
            return rss_items, True
    return items, failures < queries


def fetch_arxiv_rss(cfg: dict, cutoff: date) -> tuple[list[Item], bool]:
    """Read the arXiv category RSS feeds, used when the Atom API answers nothing.

    Returns the items and whether any feed answered at all, because an empty feed on a
    Saturday is a closed archive rather than a broken source.
    """
    items: dict[str, Item] = {}
    answered = False
    topical = set(cfg.get("rss_topical", ["cs.RO"]))
    for index, category in enumerate(cfg.get("rss_categories", [])):
        if index:
            time.sleep(ARXIV_DELAY)
        payload = http_get(f"{ARXIV_RSS}/{category}")
        if not payload:
            continue
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            print(f"[fetch] arxiv rss parse error ({category}): {exc}")
            continue
        answered = True
        channel_date = root.findtext("./channel/pubDate")
        for entry in root.findall(".//item"):
            link = clean_text(entry.findtext("link"))
            title = clean_text(entry.findtext("title"))
            summary = clean_text(entry.findtext("description"))
            announce = clean_text(entry.findtext(f"{ARXIV_NS}announce_type"))
            match = _ANNOUNCE.match(summary)
            if match:
                announce = announce or match.group(1)
                summary = summary[match.end():]
            # "replace" and "replace-cross" re-announce a revised version of an old
            # paper. The API path never saw them, because it reads the first-version
            # date; here they would carry today's date and pass as news.
            if announce.lower().startswith("replace"):
                continue
            # Items may carry no date of their own; the channel speaks for the
            # announcement day.
            published = parse_date(entry.findtext("pubDate")) or parse_date(channel_date)
            if not title or not link or not published:
                continue
            if date.fromisoformat(published) < cutoff:
                continue
            short_id, link = arxiv_identity(link)
            if short_id in items:
                # A cross-list arrives once per category; it is on-topic if any of
                # them is a Physical AI category.
                items[short_id].topical = items[short_id].topical or category in topical
                continue
            items[short_id] = Item(
                id=f"arxiv:{short_id}",
                title=title,
                url=link,
                publisher=f"arXiv {short_id}",
                source_id="arxiv",
                evidence=cfg.get("evidence", "R"),
                published=published,
                summary=summary,
                # cs.AI and cs.LG carry far more chatbots than robots; papers from
                # those listings have to name an anchor term to count as on-topic.
                topical=category in topical,
            )
    return list(items.values()), answered


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
    cap = int(config.sources.get("filters", {}).get("max_entries_per_feed", 100))
    for feed in config.sources.get("feeds", []):
        limit = CJK_SUMMARY_CHARS if feed.get("lang") in ("zh", "ja") else MAX_SUMMARY_CHARS
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
        # Some feeds carry their whole archive -- the Hugging Face blog ships every
        # post it has ever published, about 870 of them. Only the newest can pass the
        # age filter anyway, and counting the rest made the fetch report misleading.
        entries = sorted(
            (entry for entry in _feed_entries(root) if entry[0] and entry[1]),
            key=lambda entry: entry[3], reverse=True,
        )[:cap]
        for title, link, summary, published in entries:
            if len(summary) > limit:
                cut = summary[:limit]
                head = cut.rsplit(" ", 1)[0]
                summary = (head if len(head) > limit // 2 else cut) + " …"
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
                    topical=bool(feed.get("topical", False)),
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
    # Answered, but only through a stand-in endpoint: served today, broken all the same.
    fallback: list[str] = field(default_factory=list)

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
            "fallback": sorted(self.fallback),
            "per_source": counts,
        }


def fetch_all(config: Config, offline: bool = False, reference: date | None = None) -> FetchReport:
    """Collect live items. In offline mode nothing is fetched (used by CI and tests)."""
    if offline:
        print("[fetch] offline mode: skipping network sources")
        return FetchReport()
    report = FetchReport()
    if config.sources.get("arxiv", {}).get("enabled", False):
        report.attempted.append("arxiv")
        arxiv_items, answered = fetch_arxiv(config, reference, report)
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
    if report.fallback:
        print(f"[fetch] served by a fallback: {', '.join(sorted(report.fallback))}")
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
