"""Fetch layer: arXiv API plus Atom/RSS feeds, standard library only.

Design rules:
- every network call is timeout-bounded and failure-tolerant; one dead source must
  never fail the daily run;
- no HTML scraping, only declared machine-readable endpoints;
- output is normalised into `Item` objects so downstream code never sees feed quirks.

One bounded exception, `page_watch` (`fetch_page_watch`): an official, first-party
source that publishes no feed at all (Figure, 1X, Physical Intelligence, Unitree, and
the pages in `standards_watch`) may be read as a page, and only through a self-hosted
Firecrawl listening on a loopback address (127.0.0.1, localhost or ::1). This module
never requests those pages itself, never follows a redirect off the loopback service,
ignores proxy settings for it, and refuses any other endpoint. Media and research
sources, arXiv and every feed above are unchanged: if a source has a feed, the feed is
what is read. robots.txt is honoured for every watched page. What a page says is
untrusted text: it is reduced to plain text, cut to length and then passes through the
same relevance gate, evidence scoring, deduplication and editorial review as a feed
entry. When the local Firecrawl does not answer, the channel is skipped and recorded as
unavailable; the run goes on.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import json
import os
import re
import time
import urllib.robotparser
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
# RSS 1.0 (RDF): Nature's journal feeds. Items are siblings of the channel, in their own
# namespace, dated by Dublin Core.
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
# The product token robots.txt rules are matched against (the start of USER_AGENT).
ROBOTS_TOKEN = "physical-ai-radar"
# Two requests to one host are at least this far apart (GitHub serves three release feeds).
HOST_SPACING = 1.0
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


def http_get(url: str, retries: int = RETRIES, report: dict[str, Any] | None = None) -> bytes | None:
    """Fetch a URL, retrying transient failures; never raise into the caller.

    Feeds and the arXiv API both fail intermittently, and a single timeout or
    rate-limit reply used to drop a whole source for the day on first contact.
    `report`, when given, receives the final HTTP `status` (None when no HTTP answer
    came at all): robots.txt handling depends on it.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        delay = RETRY_DELAY
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if report is not None:
                    report["status"] = getattr(response, "status", 200)
                return response.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if report is not None:
                report["status"] = exc.code
            if 400 <= exc.code < 500 and exc.code not in RETRYABLE_STATUS:
                break
            delay = _retry_after(exc)
        # http.client raises its own family for a garbled status line or a body cut
        # short, and none of it is an OSError; one such reply used to end the whole run.
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
                ValueError) as exc:
            last = exc
            if report is not None:
                report["status"] = None
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
    """Yield (title, url, summary, date) for Atom, RSS 2.0 or RSS 1.0 (RDF) documents."""
    rdf_items = root.findall(f"{RSS1}item")
    if rdf_items:
        for item in rdf_items:
            summary = item.findtext(f"{RSS1}description") or item.findtext(f"{CONTENT}encoded") or ""
            yield (
                clean_text(item.findtext(f"{RSS1}title") or item.findtext(f"{DC}title")),
                clean_text(item.findtext(f"{RSS1}link")),
                clean_text(summary),
                parse_date(item.findtext(f"{DC}date")),
            )
        return
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


_LAST_REQUEST: dict[str, float] = {}


def polite(url: str) -> None:
    """Leave HOST_SPACING seconds between two requests to the same host."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    wait = _LAST_REQUEST.get(host, -HOST_SPACING) + HOST_SPACING - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST[host] = time.monotonic()


ALLOW, DISALLOW, FORBIDDEN, UNREACHABLE = "allow", "disallowed", "forbidden", "unreachable"


def _robots_get(url: str) -> tuple[int | None, bytes | None]:
    report: dict[str, Any] = {}
    body = http_get(url, report=report)
    return (report.get("status", 200 if body is not None else None), body)


class RobotsCache:
    """robots.txt, read once per host per run, for every feed and signal endpoint.

    The outcome follows RFC 9309 section 2.3.1 (and Python's RobotFileParser):
    - 2xx: the rules decide; one that names this agent or `*` and disallows the URL
      keeps it from being requested (the Science Robotics eTOC is under `/action/`);
    - 401 or 403: everything on the host is `forbidden`;
    - any other 4xx (404, 410...): there are no rules, everything is allowed;
    - 5xx, 429 after its retry, no HTTP answer at all, or anything else that cannot be
      classified: the host is `unreachable` and nothing on it is requested this run.
    `verdict()` returns `allow` or the reason a URL is refused (`disallowed` by a rule,
    `forbidden`, `unreachable`). Every refused feed is `robots_skipped` with its reason,
    never `failed`: the source-repair job cannot fix a robots.txt (01's decision,
    2026-10-25); `pairadar.health` reports the reasons on their own.
    `getter(url) -> (status, body)` is injectable for tests.
    """

    def __init__(self, getter=None):
        self.getter = getter or _robots_get
        self.hosts: dict[str, Any] = {}

    def verdict(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin not in self.hosts:
            polite(origin)
            status, body = self.getter(origin + "/robots.txt")
            if status is not None and 200 <= status < 300 and body is not None:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(body[:MAX_ROBOTS_CHARS].decode("utf-8", "replace").splitlines())
                self.hosts[origin] = parser
            elif status in (401, 403):
                print(f"[fetch] robots.txt for {origin} answered {status}: host forbidden")
                self.hosts[origin] = FORBIDDEN
            elif status is not None and 400 <= status < 500 and status != 429:
                self.hosts[origin] = ALLOW
            else:
                print(f"[fetch] robots.txt for {origin} unreachable ({status}): host not requested this run")
                self.hosts[origin] = UNREACHABLE
        rule = self.hosts[origin]
        if isinstance(rule, str):
            return rule
        return ALLOW if rule.can_fetch(ROBOTS_TOKEN, url) else DISALLOW

    def allows(self, url: str) -> bool:
        return self.verdict(url) == ALLOW


def _on_topic(title: str, summary: str, feed: dict[str, Any], config: Config) -> bool:
    """The feed's own `keywords`, else (with `require_anchor`) the taxonomy anchors."""
    from .distill import count_terms  # distill imports this module
    terms = feed.get("keywords") or config.taxonomy.get("anchors", [])
    return count_terms(f"{title}. {summary}", terms) > 0


def fetch_feeds(config: Config, robots: RobotsCache | None = None,
                skipped: list[str] | None = None,
                reasons: dict[str, str] | None = None) -> tuple[list[Item], list[str]]:
    """Read every configured feed, collecting the ids of those that did not answer.

    `robots`, when given, is consulted first; a feed it refuses (disallowed, forbidden,
    or a host whose robots.txt is unreachable) is not requested and goes to `skipped`
    with its reason in `reasons`, never to `failed`.
    A feed with `keywords` keeps only entries naming one of them, before the per-feed
    cap: OpenAI's feed carries 1,300 posts, nearly none about robots, and the cap would
    otherwise fill with them. The taxonomy anchors are too loose there -- "cosmos" (an
    NVIDIA world model) let an Azure Cosmos DB post through -- so a general source names
    its own list; `require_anchor` alone uses the anchors. `max_entries` lowers the cap
    for one feed (a release feed: the newest release only). `title_prefix` names the
    project in front of a bare release title ("3.17.0" -> "MuJoCo 3.17.0").
    """
    items: list[Item] = []
    failed: list[str] = []
    cap = int(config.sources.get("filters", {}).get("max_entries_per_feed", 100))
    for feed in config.sources.get("feeds", []):
        limit = CJK_SUMMARY_CHARS if feed.get("lang") in ("zh", "ja") else MAX_SUMMARY_CHARS
        if robots is not None:
            verdict = robots.verdict(feed["url"])
            if verdict != ALLOW:
                print(f"[fetch] robots.txt ({verdict}) refuses {feed['id']}: skipped")
                if skipped is not None:
                    skipped.append(feed["id"])
                if reasons is not None:
                    reasons[feed["id"]] = verdict
                continue
            polite(feed["url"])
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
        entries = [entry for entry in _feed_entries(root) if entry[0] and entry[1]]
        if feed.get("keywords") or feed.get("require_anchor"):
            entries = [entry for entry in entries if _on_topic(entry[0], entry[2], feed, config)]
        entries = sorted(entries, key=lambda entry: entry[3], reverse=True)[:min(cap, int(feed.get("max_entries", cap)))]
        prefix = str(feed.get("title_prefix", "")).strip()
        for title, link, summary, published in entries:
            if prefix and not title.lower().startswith(prefix.lower()):
                title = f"{prefix} {title}"
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


# --- page_watch: official sources without a feed, through the local Firecrawl --------

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
FIRECRAWL_ENV = "RADAR_FIRECRAWL_URL"
DEFAULT_FIRECRAWL = "http://127.0.0.1:3002"
# The baseline in radar/page-watch.json; distinct from the freshness evidence schema
# (pairadar-page-watch-v1, written by cli.write_page_watch_evidence) on purpose.
PAGE_STATE_SCHEMA = "pairadar-page-watch-baseline-2"
# A Firecrawl reply with one of these is the service (or its gateway) being down.
SERVICE_DOWN_STATUS = frozenset({502, 503, 504})
# This many page failures in a row answered with a 5xx and the channel is treated as
# unavailable: a broken service is not tried against every remaining source.
MAX_CONSECUTIVE_5XX = 3
# Where firecrawl_post records the HTTP status of a failed-page reply.
HTTP_STATUS_KEY = "_http_status"
# Page-mode lines that only say when the page was last touched; a change in them is
# not news. (Numbers elsewhere -- stage codes, application dates -- are the substance
# of a standards page and still count.)
_STAMP_LINE = re.compile(
    r"^\W*(last\s+)?(updated|modified|reviewed|revised|published)\b|©|\bcopyright\b|"
    r"^\W*(page\s+)?last\s+(change|update)", re.I)
# Same tag on every request, so the radar's change history never mixes with anything
# else that uses the same Firecrawl.
CHANGE_TAG = "physical-ai-radar"
PAGE_MODES = ("listing", "page")
# Firecrawl's own robots check is a per-team flag, not a request option, so the radar
# checks robots.txt itself; these are the agents the request goes out as.
ROBOTS_AGENTS = ("physical-ai-radar", "FirecrawlAgent")
MAX_RESPONSE_BYTES = 4_000_000
MAX_MARKDOWN_CHARS = 200_000
MAX_ROBOTS_CHARS = 500_000
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 500
MAX_STATE_LINKS = 300
MAX_STATE_LINES = 2000
# Link text that names no post; such a link is only used if the same URL also
# appears with a real title.
_GENERIC_LINK = re.compile(
    r"^(read|learn|see|view|find out|discover|explore|watch)( (more|all|the post|article|story|now))?\W*$"
    r"|^(more|details|continue reading|here|link|next|previous|older posts?|newer posts?|all news|news|blog)\W*$",
    re.I)
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_MD_LINK = re.compile(r"\[([^\]\n]{0,400})\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
_MD_MARKS = re.compile(r"^[\s>#*\-+|`~=_]+|[*_`~|]+")
_MONTHS = {name: index for index, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
     ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
     ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")), start=1) for name in names}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE_PATTERNS = (
    (re.compile(r"\b(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})"), "ymd"),
    (re.compile(_MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I), "mdy"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+(\d{4})\b", re.I), "dmy"),
    (re.compile(_MONTH + r"\s+(\d{1,2})\s*['’](\d{2})\b", re.I), "md'y"),
)


class FirecrawlUnavailable(Exception):
    """The local Firecrawl did not answer as a service: refused, timed out, 5xx."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A loopback service that redirects is not followed anywhere, least of all off-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_FIRECRAWL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def loopback_endpoint(raw: str | None) -> str | None:
    """The Firecrawl base URL when it names a loopback host, else None.

    Only http(s), no credentials, no path beyond "/", no query; the host must be
    exactly 127.0.0.1, localhost or ::1. Anything else -- a LAN address, a cloud API,
    a name that merely resolves to loopback today -- is refused.
    """
    try:
        parsed = urllib.parse.urlsplit((raw or "").strip())
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("http", "https") or host not in LOOPBACK_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return None
    netloc = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{netloc}" + (f":{port}" if port else "")


def page_watch_settings(config: Config) -> dict[str, Any]:
    raw = config.sources.get("page_watch_settings", {}) or {}

    def bounded(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, low), high)

    return {
        "enabled": bool(raw.get("enabled", False)),
        "endpoint": os.environ.get(FIRECRAWL_ENV) or raw.get("endpoint") or DEFAULT_FIRECRAWL,
        "timeout": bounded("timeout_seconds", 60, 5, 120),
        "budget": bounded("budget_seconds", 600, 10, 1800),
        "max_entries": int(bounded("max_entries", 12, 1, 50)),
        "max_items": int(bounded("max_items_per_source", 3, 1, 10)),
    }


def _public_https(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or "." not in host or host in LOOPBACK_HOSTS:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    # IP literals are never a publisher's news page; they are how SSRF starts.
    return not re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host)


def page_watch_problems(sources: dict[str, Any]) -> list[str]:
    """Schema problems in `page_watch`, one line each; empty when the list is sound."""
    problems: list[str] = []
    entries = sources.get("page_watch", [])
    if not isinstance(entries, list):
        return ["page_watch must be a list"]
    standards = {watch.get("id"): watch for watch in sources.get("standards_watch", [])}
    taken = {"arxiv", "baseline", *(feed.get("id") for feed in sources.get("feeds", []))}
    for index, entry in enumerate(entries):
        where = f"page_watch[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: not an object")
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", entry_id):
            problems.append(f"{where}: id must be lowercase letters, digits and dashes")
        elif entry_id in taken:
            problems.append(f"{where}: id {entry_id} is already used")
        else:
            taken.add(entry_id)
            where = f"page_watch {entry_id}"
        if entry.get("evidence") != "O":
            problems.append(f"{where}: page_watch is for first-party official sources, evidence must be O")
        weight = entry.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 2:
            problems.append(f"{where}: weight must be a number in (0, 2]")
        mode = entry.get("mode", "listing")
        if mode not in PAGE_MODES:
            problems.append(f"{where}: mode must be one of {', '.join(PAGE_MODES)}")
        if "url" in entry and "standards_watch" in entry:
            problems.append(f"{where}: give url or standards_watch, not both")
        if "standards_watch" in entry:
            if entry["standards_watch"] not in standards:
                problems.append(f"{where}: no standards_watch entry {entry['standards_watch']}")
        elif not _public_https(str(entry.get("url", ""))):
            problems.append(f"{where}: url must be a public https URL")
        if mode == "listing" and not str(entry.get("link_prefix", "")).startswith("/"):
            problems.append(f"{where}: a listing needs a link_prefix path starting with /")
        hosts = entry.get("hosts", [])
        if not isinstance(hosts, list) or not all(isinstance(host, str) and "." in host for host in hosts):
            problems.append(f"{where}: hosts must be a list of domain names")
    return problems


def _resolve_page_watch(sources: dict[str, Any]) -> list[dict[str, Any]]:
    """Entries with url and title filled in; an invalid entry is left out, not fatal."""
    standards = {watch.get("id"): watch for watch in sources.get("standards_watch", [])}
    bad = {line.split(":", 1)[0] for line in page_watch_problems(sources)}
    resolved = []
    for index, entry in enumerate(sources.get("page_watch", [])):
        if not isinstance(entry, dict):
            continue
        if f"page_watch {entry.get('id')}" in bad or f"page_watch[{index}]" in bad:
            print(f"[fetch] page_watch: ignoring invalid entry {entry.get('id', index)}")
            continue
        entry = dict(entry)
        watch = standards.get(entry.get("standards_watch"))
        if watch:
            entry["url"] = watch["url"]
            entry.setdefault("title", watch.get("title", ""))
        resolved.append(entry)
    return resolved


def firecrawl_post(endpoint: str, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST JSON to the loopback Firecrawl and return its JSON reply.

    Raises FirecrawlUnavailable when the service itself is not there or not well:
    refused, timed out, 502/503/504, a 5xx that is not JSON or carries no Firecrawl
    error `code` (or `UNKNOWN_ERROR`), 401, 429, a redirect or a reply that is not
    JSON. A 4xx about the target page (408 included: Firecrawl's own scrape timeout)
    and a 5xx with a Firecrawl error code such as SCRAPE_SITE_ERROR are returned as a
    failed reply carrying the HTTP status under `HTTP_STATUS_KEY`; the caller records
    that page as failed and goes on.
    """
    base = loopback_endpoint(endpoint)
    if base is None:
        raise FirecrawlUnavailable("endpoint is not a loopback address")
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with _FIRECRAWL_OPENER.open(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in SERVICE_DOWN_STATUS or exc.code in (401, 429) or 300 <= exc.code < 400:
            raise FirecrawlUnavailable(f"HTTP {exc.code}") from None
        try:
            payload = exc.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            payload = b""
        try:
            reply = json.loads(payload or b"{}")
        except ValueError:
            reply = None
        if exc.code >= 500:
            # Firecrawl answers a target site's own failure (connection reset, TLS, every
            # engine failed) with 500 and a JSON error code. That is the page failing, not
            # the service; only a bare 5xx, a non-JSON one or UNKNOWN_ERROR is the service.
            code = reply.get("code") if isinstance(reply, dict) else None
            if not isinstance(code, str) or not code or code == "UNKNOWN_ERROR":
                raise FirecrawlUnavailable(f"HTTP {exc.code}") from None
        reply = reply if isinstance(reply, dict) else {}
        reply["success"] = False
        reply.setdefault("error", f"HTTP {exc.code}")
        reply[HTTP_STATUS_KEY] = exc.code
        return reply
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
            ValueError) as exc:
        raise FirecrawlUnavailable(type(exc).__name__) from None
    if len(payload) > MAX_RESPONSE_BYTES:
        return {"success": False, "error": "reply larger than the page_watch limit"}
    try:
        reply = json.loads(payload)
    except ValueError:
        raise FirecrawlUnavailable("reply is not JSON") from None
    if not isinstance(reply, dict):
        raise FirecrawlUnavailable("reply is not a JSON object")
    return reply


def _short(text: Any, limit: int = 160) -> str:
    """A log-safe fragment of untrusted text."""
    return clean_text(str(text or ""))[:limit]


class _Strikes:
    """Consecutive Firecrawl replies that were a 5xx page failure.

    One site failing inside Firecrawl is that site's problem; MAX_CONSECUTIVE_5XX in a
    row is the service's, and the channel stops as unavailable.
    """

    def __init__(self, limit: int = MAX_CONSECUTIVE_5XX) -> None:
        self.limit = limit
        self.count = 0

    def note(self, reply: dict[str, Any], resets: bool = True) -> None:
        """Count a 5xx failed reply; any other reply ends the run of them, unless it is a
        robots.txt reply (`resets=False`): a small text file that loads says little
        about whether the service can still render pages."""
        status = reply.get(HTTP_STATUS_KEY)
        if isinstance(status, int) and status >= 500:
            self.count += 1
            if self.count >= self.limit:
                raise FirecrawlUnavailable(f"{self.count} consecutive HTTP 5xx page failures")
        elif resets:
            self.count = 0


def _robots_allows(settings: dict[str, Any], url: str, cache: dict[str, Any],
                   strikes: _Strikes | None = None) -> bool:
    """Whether robots.txt lets the radar read `url`, fetched through the same Firecrawl.

    Read as RFC 9309 says: a 4xx robots.txt (or none) allows everything; one that
    cannot be read for any other reason disallows everything for this run.
    """
    parsed = urllib.parse.urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        reply = firecrawl_post(settings["endpoint"], "/v2/scrape", {
            "url": origin + "/robots.txt", "formats": ["rawHtml"], "onlyMainContent": False,
            "timeout": int(settings["timeout"] * 1000), "proxy": "basic",
        }, settings["timeout"] + 10)
        if strikes is not None:
            strikes.note(reply, resets=False)
        data = reply.get("data") if reply.get("success") else None
        status = int(((data or {}).get("metadata") or {}).get("statusCode") or 0)
        parser = urllib.robotparser.RobotFileParser()
        if data is not None and 200 <= status < 300:
            text = str(data.get("rawHtml") or "")[:MAX_ROBOTS_CHARS]
            # A site that answers robots.txt with its HTML page has no rules to read.
            parser.parse([] if text.lstrip()[:1] == "<" else text.splitlines())
        elif data is not None and 400 <= status < 500:
            parser.parse([])
        else:
            parser.parse(["User-agent: *", "Disallow: /"])
        cache[origin] = parser
    parser = cache[origin]
    return all(parser.can_fetch(agent, url) for agent in ROBOTS_AGENTS)


def _host_allowed(host: str, allowed: set[str]) -> bool:
    host = host.lower()
    return any(host == name or host.endswith("." + name) for name in allowed)


def _plain(fragment: str) -> str:
    """Markdown to one line of plain text: images dropped, links reduced to their text."""
    text = _MD_IMAGE.sub(" ", fragment)
    text = _MD_LINK.sub(lambda match: match.group(1), text)
    text = "\n".join(_MD_MARKS.sub(" ", line) for line in text.splitlines())
    return clean_text(text)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0]
    return (head if len(head) > limit // 2 else text[:limit]) + " …"


def listing_date(text: str, reference: date) -> str:
    """The first date written in `text` (ISO, "Oct 14, 2026", "14 October 2026",
    "MAR 18 '25", 2026年10月14日), as YYYY-MM-DD; "" when none can be read.

    A date after the run day is no publication date; it is read as none.
    """
    for pattern, order in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            try:
                if order == "ymd":
                    year, month, day = map(int, groups)
                elif order == "mdy":
                    month, day, year = _MONTHS[groups[0].lower().rstrip(".")], int(groups[1]), int(groups[2])
                elif order == "dmy":
                    day, month, year = int(groups[0]), _MONTHS[groups[1].lower().rstrip(".")], int(groups[2])
                else:
                    month, day, year = _MONTHS[groups[0].lower().rstrip(".")], int(groups[1]), 2000 + int(groups[2])
                found = date(year, month, day)
            except (KeyError, ValueError):
                continue
            if found <= reference:
                return found.isoformat()
    return ""


def listing_links(markdown: str, page_url: str, hosts: Iterable[str], prefix: str,
                  reference: date) -> list[dict[str, str]]:
    """First-party post links on a listing page, in page order, one per URL.

    A link counts when it stays on the publisher's hosts, its path starts with
    `prefix` and it is not the listing itself. Its title is the link text, its date
    the first date written in the link text or the two lines after it, its summary
    those lines as plain text.
    """
    page = urllib.parse.urlsplit(page_url)
    allowed = {page.hostname.lower().removeprefix("www."), *(host.lower() for host in hosts)}
    lines = _MD_IMAGE.sub(" ", markdown).splitlines()
    found: dict[str, dict[str, str]] = {}
    for number, line in enumerate(lines):
        for match in _MD_LINK.finditer(line):
            target = urllib.parse.urljoin(page_url, match.group(2).strip())
            try:
                parsed = urllib.parse.urlsplit(target)
            except ValueError:
                continue
            path = parsed.path.rstrip("/") or "/"
            if (parsed.scheme != "https" or not _host_allowed(parsed.hostname or "", allowed)
                    or not parsed.path.startswith(prefix)
                    or path == (page.path.rstrip("/") or "/") or len(target) > MAX_URL_CHARS):
                continue
            url = urllib.parse.urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))
            title = _cut(_plain(match.group(1)), MAX_TITLE_CHARS)
            after = _plain(" ".join([line[match.end():], *lines[number + 1:number + 3]]))
            context = f"{_plain(match.group(1))} {after}"
            entry = found.get(url)
            usable = len(title) >= 4 and not _GENERIC_LINK.match(title)
            if entry is None:
                found[url] = {"url": url, "title": title if usable else "",
                              "date": listing_date(context, reference),
                              "summary": _cut(after, MAX_SUMMARY_CHARS)}
            else:
                if usable and not entry["title"]:
                    entry["title"] = title
                entry["date"] = entry["date"] or listing_date(context, reference)
    return [entry for entry in found.values() if entry["title"]]


def _page_lines(markdown: str) -> list[str]:
    """A page's lines as plain text, without the ones that only stamp an update time."""
    lines = []
    for raw in markdown.splitlines():
        text = _plain(raw)
        if len(text) >= 3 and not _STAMP_LINE.search(text):
            lines.append(text)
    return lines


def _line_digest(text: str) -> str:
    return hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:16]


def _within(day: str, reference: date, max_age: int) -> bool:
    try:
        age = (reference - date.fromisoformat(day)).days
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max_age


def _known_links(previous: dict[str, Any]) -> list[dict[str, Any]]:
    """The baseline's links as {url, first_seen, seeded?, offered?}, page order first."""
    known = []
    for raw in previous.get("links", []) or []:
        if isinstance(raw, dict) and isinstance(raw.get("url"), str):
            known.append({key: raw[key] for key in ("url", "first_seen", "seeded", "offered") if key in raw})
        elif isinstance(raw, str):  # an older baseline listed bare URLs: treat as seen at seeding
            known.append({"url": raw, "first_seen": str(previous.get("since") or ""), "seeded": True})
    return known


@dataclass
class PageWatchResult:
    items: list[Item] = field(default_factory=list)
    # Every entry the run got to, in order; each is then in exactly one of
    # checked (the page was read), failed (it was not) or robots_skipped.
    attempted: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    robots_skipped: list[str] = field(default_factory=list)
    unavailable: bool = False
    enabled: bool = False
    state: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """The explicit page_watch block of FetchReport.to_dict(); the evidence reads only this."""
        return {"enabled": self.enabled, "attempted": list(self.attempted), "checked": list(self.checked),
                "failed": list(self.failed), "robots_skipped": list(self.robots_skipped),
                "unavailable": self.unavailable, "items": len(self.items)}


def _watch_one(entry: dict[str, Any], reply: dict[str, Any], previous: dict[str, Any],
               settings: dict[str, Any], reference: date,
               max_age: int = 30) -> tuple[list[Item], dict[str, Any] | None, bool]:
    """Items, new state (None = keep the old one) and whether the page was read."""
    data = reply.get("data") if reply.get("success") else None
    if not isinstance(data, dict):
        print(f"[fetch] page_watch skip {entry['id']}: {_short(reply.get('error'))}")
        return [], None, False
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    try:
        status = int(meta.get("statusCode") or 200)
    except (TypeError, ValueError):
        status = 0
    tracking = data.get("changeTracking") if isinstance(data.get("changeTracking"), dict) else {}
    remote = tracking.get("changeStatus")
    if remote == "removed" or not 200 <= status < 300:
        print(f"[fetch] page_watch skip {entry['id']}: page answered {status or 'nothing'}")
        return [], None, False
    markdown = str(data.get("markdown") or "")[:MAX_MARKDOWN_CHARS]
    if remote == "same" and not markdown.strip():
        # Firecrawl kept the previous version and sent nothing to read: no item.
        return [], None, True
    today = reference.isoformat()
    seeded = "digest" in previous
    since = str(previous.get("since") or today) if seeded else today
    items: list[Item] = []

    def item(item_id: str, title: str, url: str, published: str, summary: str) -> Item:
        return Item(id=item_id, title=title, url=url, publisher=entry.get("publisher", entry["id"]),
                    source_id=entry["id"], evidence=entry.get("evidence", "O"), published=published,
                    summary=summary, topical=bool(entry.get("topical", False)))

    if entry.get("mode", "listing") == "listing":
        # Firecrawl's change history needs its hosted store (Bigtable and GCS at the pinned
        # 85a40f9d); self-hosted it reports every page as new, so the radar keeps its own
        # baseline: every link with the day it was first seen. Links still inside the age
        # window are offered every run, like feed entries (the repeat guard downstream
        # keeps a published one from coming back); max_items_per_source limits what is
        # offered per run, never what is recorded, and the least recently offered go first.
        digest = hashlib.sha256(_WS.sub(" ", markdown).strip().encode("utf-8")).hexdigest()
        links = listing_links(markdown, entry["url"], entry.get("hosts", []),
                              entry.get("link_prefix", "/"), reference)
        known = {link["url"]: link for link in _known_links(previous)}
        current: list[dict[str, Any]] = []
        for link in links:
            record = dict(known.get(link["url"]) or {"url": link["url"], "first_seen": today})
            if link["url"] not in known and not seeded:
                # First sight of a listing: most links on it are old posts. Only one that
                # states its own date can be news, and the age window decides that.
                record["seeded"] = True
            current.append(record)
        eligible = []
        for index, (link, record) in enumerate(zip(links, current)):
            by_date = bool(link["date"]) and _within(link["date"], reference, max_age)
            by_sight = not record.get("seeded") and _within(str(record.get("first_seen")), reference, max_age)
            if by_date or by_sight:
                eligible.append((str(record.get("offered") or ""), index))
        for _, index in sorted(eligible)[:settings["max_items"]]:
            link, record = links[index], current[index]
            record["offered"] = today
            items.append(item(f"{entry['id']}:{link_digest(link['url'])}", link["title"], link["url"],
                              # an undated link was published, as far as the radar knows,
                              # the day it first appeared
                              link["date"] or str(record.get("first_seen") or today), link["summary"]))
        on_page = {record["url"] for record in current}
        merged = current + [record for url, record in known.items() if url not in on_page]
        state = {"digest": digest, "since": since,
                 "changed": today if previous.get("digest") != digest else str(previous.get("changed") or today),
                 "links": merged[:MAX_STATE_LINKS]}
        return items, (None if state == previous else state), True
    lines = _page_lines(markdown)
    digests = list(dict.fromkeys(_line_digest(line) for line in lines))
    digest = hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()
    if previous.get("digest") == digest:
        return [], None, True
    known_lines = set(previous.get("lines", []))
    added = [line for line in lines if _line_digest(line) not in known_lines]
    state = {"digest": digest, "since": since, "changed": today, "lines": digests[:MAX_STATE_LINES]}
    published_meta = parse_date(meta.get("publishedTime") or meta.get("modifiedTime")
                                or meta.get("dcDate") or "")
    if published_meta and date.fromisoformat(published_meta) > reference:
        published_meta = ""
    if (seeded and added) or (not seeded and published_meta):
        title = entry.get("title") or _plain(str(meta.get("title") or meta.get("ogTitle") or ""))
        excerpt = " · ".join(added if seeded else lines)
        # A change the radar saw today is dated today: the page's own publishedTime is
        # when the page was first put up, often years ago, and would age the change out.
        items.append(item(f"{entry['id']}:{digest[:12]}", _cut(title or entry["id"], MAX_TITLE_CHARS),
                          entry["url"], today if seeded else published_meta,
                          _cut(excerpt, MAX_SUMMARY_CHARS)))
    return items, state, True


def fetch_page_watch(config: Config, reference: date | None = None,
                     state: dict[str, Any] | None = None) -> PageWatchResult:
    """Watch official pages that have no feed, through the loopback Firecrawl only.

    For each entry: robots.txt is checked, then `POST {endpoint}/v2/scrape` asks for
    markdown and changeTracking. A page Firecrawl reports `removed` gives no item;
    otherwise the radar compares it with its own baseline in `state`:

    - listing: every first-party post link is recorded with the day it was first seen.
      Each run offers, as one item each, the links still inside `filters.max_age_days`
      -- by the date the link states or, for a link that appeared after the first
      look, the day it was first seen -- at most `max_items_per_source` per run, least
      recently offered first, so an overflow is offered on the next run. The repeat
      guard (`recent_urls`) keeps a published one from being picked again;
    - page: a page with lines it did not have last time becomes one item pointing at
      the page, the new lines as its excerpt, dated the run day. Lines that only stamp
      an update time ("Last updated …", "©") are ignored.

    Each attempted entry ends up in exactly one of `checked`, `failed` (the page or
    Firecrawl's scrape of it failed: non-2xx, success:false, 408, a 5xx with a
    Firecrawl error code) or `robots_skipped`. The channel stops as `unavailable`, the
    baseline kept, when Firecrawl itself does not answer (refused, client timeout,
    502/503/504, a 5xx without an error code or UNKNOWN_ERROR, not JSON) or after
    MAX_CONSECUTIVE_5XX page failures in a row answered with a 5xx.
    """
    reference = reference or datetime.now(timezone.utc).date()
    previous_state = state if isinstance(state, dict) else {}
    known = previous_state.get("sources") if previous_state.get("schema") == PAGE_STATE_SCHEMA else None
    known = known if isinstance(known, dict) else {}
    result = PageWatchResult(state={"schema": PAGE_STATE_SCHEMA, "sources": dict(known)})
    settings = page_watch_settings(config)
    entries = _resolve_page_watch(config.sources)
    if not settings["enabled"] or not entries:
        return result
    result.enabled = True
    try:
        max_age = int(config.sources.get("filters", {}).get("max_age_days", 30))
    except (TypeError, ValueError):
        max_age = 30
    if loopback_endpoint(settings["endpoint"]) is None:
        print("[fetch] page_watch: refusing a Firecrawl endpoint that is not on a loopback "
              "address (127.0.0.1, localhost, ::1); channel skipped")
        result.unavailable = True
        return result
    if len(entries) > settings["max_entries"]:
        print(f"[fetch] page_watch: watching the first {settings['max_entries']} of {len(entries)} entries")
        entries = entries[:settings["max_entries"]]
    deadline = time.monotonic() + settings["budget"]
    robots: dict[str, Any] = {}
    strikes = _Strikes()
    for entry in entries:
        if time.monotonic() >= deadline:
            print(f"[fetch] page_watch: time budget of {settings['budget']:.0f}s spent; "
                  f"{entry['id']} and later entries wait for the next run")
            break
        try:
            if not _robots_allows(settings, entry["url"], robots, strikes):
                print(f"[fetch] page_watch skip {entry['id']}: robots.txt disallows {entry['url']}")
                result.attempted.append(entry["id"])
                result.robots_skipped.append(entry["id"])
                continue
            reply = firecrawl_post(settings["endpoint"], "/v2/scrape", {
                "url": entry["url"],
                "formats": ["markdown", {"type": "changeTracking", "modes": ["git-diff"],
                                         "tag": CHANGE_TAG}],
                # A listing's links can sit outside what Firecrawl calls main content;
                # only links under link_prefix are used, so the rest is harmless.
                "onlyMainContent": entry.get("mode", "listing") == "page",
                "timeout": int(settings["timeout"] * 1000),
                "maxAge": 0,
                # No stealth proxy: a site that blocks a plain request is not worked around.
                "proxy": "basic",
                "blockAds": True,
                "removeBase64Images": True,
            }, settings["timeout"] + 10)
            strikes.note(reply)
        except FirecrawlUnavailable as exc:
            print(f"[fetch] page_watch: local Firecrawl unavailable ({exc}); channel skipped")
            result.unavailable = True
            break
        result.attempted.append(entry["id"])
        items, new_state, answered = _watch_one(entry, reply, known.get(entry["id"], {}),
                                                settings, reference, max_age)
        (result.checked if answered else result.failed).append(entry["id"])
        if new_state is not None:
            result.state["sources"][entry["id"]] = new_state
        result.items.extend(items)
    if result.attempted:
        print(f"[fetch] page_watch: {len(result.items)} items from {len(result.checked)}/"
              f"{len(result.attempted)} pages ({len(result.failed)} failed, "
              f"{len(result.robots_skipped)} disallowed by robots.txt)")
    return result


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
    # Channels whose local service did not answer (page_watch without Firecrawl): not
    # a source failing, and nothing a source repair could fix.
    unavailable: list[str] = field(default_factory=list)
    # page_watch entries that were attempted but not read (page failed or robots.txt
    # disallowed). Kept apart from `failed`: `failed` feeds pairadar.health's
    # struggling list and the source-repair job, which can only repair a declared feed.
    page_failed: list[str] = field(default_factory=list)
    # Feeds robots.txt refused (not requested, not failing), and why: disallowed,
    # forbidden (401/403) or unreachable (5xx, 429, no answer).
    robots_skipped: list[str] = field(default_factory=list)
    robots_reasons: dict[str, str] = field(default_factory=dict)
    # pairadar.heat.Signals from the public attention APIs (None offline).
    signals: Any = None
    # The page_watch baseline after this run, for the caller to keep.
    page_state: dict[str, Any] = field(default_factory=dict)
    # PageWatchResult.summary(): explicit ids per outcome, for the freshness evidence.
    page_watch: dict[str, Any] = field(default_factory=lambda: PageWatchResult().summary())

    @property
    def answered(self) -> list[str]:
        return [source for source in self.attempted
                if source not in self.failed and source not in self.page_failed]

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
            "unavailable": sorted(self.unavailable),
            "robots_skipped": sorted(self.robots_skipped),
            "robots_reasons": dict(sorted(self.robots_reasons.items())),
            "signals": self.signals.summary() if self.signals is not None else None,
            "per_source": counts,
            "page_watch": dict(self.page_watch),
        }


def fetch_all(config: Config, offline: bool = False, reference: date | None = None,
              page_state: dict[str, Any] | None = None) -> FetchReport:
    """Collect live items. In offline mode nothing is fetched (used by CI and tests).

    `page_state` is the page_watch baseline from the last run; the updated one comes
    back as `report.page_state`.
    """
    if offline:
        print("[fetch] offline mode: skipping network sources")
        return FetchReport()
    report = FetchReport()
    robots = RobotsCache()
    if config.sources.get("arxiv", {}).get("enabled", False):
        report.attempted.append("arxiv")
        arxiv_items, answered = fetch_arxiv(config, reference, report)
        report.items.extend(arxiv_items)
        if not answered:
            report.failed.append("arxiv")
    feed_items, failed = fetch_feeds(config, robots=robots, skipped=report.robots_skipped,
                                     reasons=report.robots_reasons)
    report.attempted.extend(feed["id"] for feed in config.sources.get("feeds", [])
                            if feed["id"] not in report.robots_skipped)
    report.items.extend(feed_items)
    report.failed.extend(failed)
    watched = fetch_page_watch(config, reference, page_state)
    report.items.extend(watched.items)
    report.attempted.extend(watched.attempted)
    report.page_failed.extend([*watched.failed, *watched.robots_skipped])
    report.page_state = watched.state
    report.page_watch = watched.summary()
    if watched.unavailable:
        report.unavailable.append("page_watch")
    from . import heat  # heat builds on distill, which imports this module
    report.signals = heat.fetch_signals(config, reference or datetime.now(timezone.utc).date(), robots)
    print(f"[fetch] collected {len(report.items)} raw items from "
          f"{len(report.answered)}/{len(report.attempted)} sources")
    if report.failed:
        print(f"[fetch] no answer from: {', '.join(sorted(report.failed))}")
    if report.page_failed:
        print(f"[fetch] page_watch pages not read: {', '.join(sorted(report.page_failed))}")
    if report.fallback:
        print(f"[fetch] served by a fallback: {', '.join(sorted(report.fallback))}")
    if report.unavailable:
        print(f"[fetch] channel unavailable: {', '.join(sorted(report.unavailable))}")
    if report.robots_skipped:
        print("[fetch] robots.txt refuses: " + ", ".join(f"{source} ({report.robots_reasons.get(source, '?')})"
                                                     for source in sorted(report.robots_skipped)))
    if report.signals.failed:
        print(f"[fetch] attention signals not read: {', '.join(sorted(report.signals.failed))}")
    return report


def source_weight(config: Config, source_id: str) -> float:
    if source_id == "arxiv":
        return float(config.sources.get("arxiv", {}).get("weight", 1.0))
    for feed in [*config.sources.get("feeds", []), *config.sources.get("page_watch", [])]:
        if feed.get("id") == source_id:
            return float(feed.get("weight", 1.0))
    return 1.0


def metadata(config: Config) -> dict[str, Any]:
    feeds: list[dict[str, Any]] = list(config.sources.get("feeds", []))
    return {
        "feed_count": len(feeds),
        "arxiv_queries": len(config.sources.get("arxiv", {}).get("queries", [])),
    }
