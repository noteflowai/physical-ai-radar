"""Distillation layer: classify, extract quantitative claims, score and select.

The ranking is deliberately explainable — every component is a small additive term so a
reader can reconstruct why an item was picked. No opaque model sits in this path.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Iterable

from .config import Config, Item
from .fetch import source_weight

# A figure starts where no word, digit, point or comma precedes it, so "v2.5" and
# "1,000" are not read from the middle, and it keeps its thousands separators:
# "\b\d+" alone published "1,000 Hz" as "000 Hz".
_FIGURE = r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
NUMBER_PATTERNS = (
    _FIGURE + r"\s?%",
    # "×" is not a word character, so a closing \b after it never matched.
    _FIGURE + r"\s?(?:x|×)(?![A-Za-z0-9])",
    _FIGURE + r"\s?(?:ms|Hz|hz|fps|FPS)\b",
    _FIGURE + r"\s?[BMK]?\s?(?:parameters|params)\b",
    _FIGURE + r"\s?(?:hours|episodes|demonstrations|trajectories|tasks)\b",
    _FIGURE + r"\s?(?:GB|TB|W|kg|DoF|dof)\b",
)
_NUMBER_RE = re.compile("|".join(NUMBER_PATTERNS))
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

EVIDENCE_BONUS = {"O": 0.6, "R": 0.35, "M": 0.1}
SIGNAL_BONUS = {
    "closed_loop": 0.5,
    "real_robot": 0.4,
    "numbers": 0.3,
    "open_release": 0.25,
    "latency": 0.2,
}


def extract_numbers(text: str, limit: int = 4) -> list[str]:
    """Pull quantitative claims out of free text, de-duplicated and order-stable."""
    found: list[str] = []
    for match in _NUMBER_RE.finditer(text):
        token = re.sub(r"\s+", " ", match.group(0)).strip()
        if token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


@lru_cache(maxsize=None)
def _keyword_re(keyword: str) -> re.Pattern[str]:
    """A keyword matches as a whole term, optionally pluralised.

    Plain substring matching filed a GeForce NOW launch under RL because "ppo" sits
    inside "support", read "author" as Jetson "thor" and "Android" as the DROID
    dataset. A term now has to start and end on a word boundary; a trailing "s" or
    "es" is allowed so "humanoids" and "benchmarks" still count.
    """
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?:e?s)?(?![a-z0-9])")


def has_term(lowered: str, keyword: str) -> bool:
    return _keyword_re(keyword).search(lowered) is not None


def count_terms(text: str, keywords: Iterable[str]) -> int:
    """How many of the keywords occur in the text, each counted once."""
    lowered = text.lower()
    return sum(1 for keyword in keywords if has_term(lowered, keyword))


def detect_signals(text: str, taxonomy: dict[str, Any]) -> list[str]:
    """Flag each signal whose keywords or pattern occur; a signal may declare both."""
    lowered = text.lower()
    signals: list[str] = []
    for signal in taxonomy.get("signals", []):
        keywords = signal.get("keywords", [])
        pattern = signal.get("pattern")
        if any(has_term(lowered, keyword) for keyword in keywords) or (
            pattern and re.search(pattern, text)
        ):
            signals.append(signal["id"])
    return signals


def lane_hits(text: str, lane: dict[str, Any]) -> int:
    """Count how many of a lane's keywords appear in the text."""
    return count_terms(text, lane["keywords"])


def anchor_hits(text: str, taxonomy: dict[str, Any]) -> int:
    """How many Physical AI anchor terms the text contains.

    Lane keywords describe *which* part of the field an item is about, and many of
    them -- benchmark, memory, fine-tuning, safety -- are just as common in posts about
    chatbots. The anchors answer the earlier question: is this about robots, embodied
    agents or the physical world at all?
    """
    return count_terms(text, taxonomy.get("anchors", []))


def classify(text: str, config: Config) -> tuple[str, int]:
    """Return (lane_id, hit_count) for the best matching lane.

    A text that matches no keyword at all still returns a lane, so callers that
    need topical evidence have to look at the hit count -- `select` does.
    """
    best_lane = "foundation"
    best_score = -1.0
    best_hits = 0
    for lane in config.lanes:
        hits = lane_hits(text, lane)
        score = hits * float(lane.get("weight", 1.0))
        if score > best_score:
            best_lane, best_score, best_hits = lane["id"], score, hits
    return best_lane, best_hits


def recency_bonus(published: str, reference: date) -> float:
    try:
        age = (reference - datetime.fromisoformat(published).date()).days
    except ValueError:
        return 0.0
    if age <= 0:
        return 1.0
    if age == 1:
        return 0.8
    if age <= 3:
        return 0.5
    if age <= 7:
        return 0.25
    return 0.0


def one_liner(summary: str, max_chars: int = 240) -> str:
    """First one or two sentences of the source abstract, hard-capped."""
    if not summary:
        return ""
    sentences = _SENTENCE_RE.split(summary)
    text = sentences[0]
    if len(text) < 110 and len(sentences) > 1:
        text = f"{text} {sentences[1]}"
    text = text.strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return text


def prefilter(items: Iterable[Item], config: Config, reference: date) -> list[Item]:
    """Drop stale entries and sponsored/promotional posts before ranking.

    Feeds occasionally re-publish old posts and trade press mixes in vendor-sponsored
    articles; neither belongs on a radar that claims to show what changed today. An
    entry whose date could not be read goes too: its age is unknown, so no window
    on the page could honestly include it.
    """
    rules = config.sources.get("filters", {})
    max_age = int(rules.get("max_age_days", 30))
    phrases = [phrase.lower() for phrase in rules.get("exclude_phrases", [])]
    kept: list[Item] = []
    for item in items:
        blob = f"{item.title} {item.summary}".lower()
        if any(phrase in blob for phrase in phrases):
            continue
        try:
            age = (reference - date.fromisoformat(item.published)).days
        except ValueError:
            continue
        if age > max_age or age < -1:
            continue
        kept.append(item)
    return kept


def enrich(items: Iterable[Item], config: Config, reference: date) -> list[Item]:
    """Classify, score and annotate items in place; returns a ranked copy."""
    enriched: list[Item] = []
    for item in items:
        blob = f"{item.title}. {item.summary}"
        if item.lane_locked:
            # Keep the curated lane, but score it on its own keywords.
            lane, hits = item.lane, lane_hits(blob, config.lane(item.lane))
        else:
            lane, hits = classify(blob, config)
        item.lane = lane
        item.lane_hits = hits
        item.anchor_hits = anchor_hits(blob, config.taxonomy)
        item.signals = detect_signals(blob, config.taxonomy)
        item.numbers = item.numbers or extract_numbers(blob)
        lane_weight = float(config.lane(lane).get("weight", 1.0))
        item.score = (
            min(hits, 6) * 0.35 * lane_weight
            + EVIDENCE_BONUS.get(item.evidence, 0.1)
            + sum(SIGNAL_BONUS.get(signal, 0.0) for signal in item.signals)
            + recency_bonus(item.published, reference)
            + (source_weight(config, item.source_id) - 1.0)
        )
        enriched.append(item)
    enriched.sort(key=lambda entry: (-entry.score, entry.published, entry.title))
    return enriched


_ARXIV_URL = re.compile(
    r"^https://(?:export\.|www\.)?arxiv\.org/(?:abs|pdf|html)/"
    r"(?P<id>\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?$"
)


def url_key(url: str) -> str:
    """Normalise a link so the same article compares equal across runs and feeds.

    The arXiv API names a paper `http://arxiv.org/abs/2609.01234v1` while the
    category feeds say `https://arxiv.org/abs/2609.01234`; left alone, the repeat
    guard saw two different papers and published the same one twice in a week. So
    the scheme is folded to https and an arXiv link is reduced to its abstract page
    without the version. The function is idempotent, because stored keys are fed
    back through it.
    """
    key = url.strip().split("#")[0].split("?")[0].rstrip("/").lower()
    if key.startswith("http://"):
        key = "https://" + key[len("http://"):]
    match = _ARXIV_URL.match(key)
    if match:
        return f"https://arxiv.org/abs/{match.group('id')}"
    return key


def deduplicate(items: Iterable[Item]) -> list[Item]:
    """Drop repeated URLs and near-identical titles, keeping the highest scored."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Item] = []
    for item in items:
        key = url_key(item.url)
        title_key = re.sub(r"[^a-z0-9]+", "", item.title.lower())[:80]
        if key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(key)
        if title_key:
            seen_titles.add(title_key)
        unique.append(item)
    return unique


def qualifies(item: Item, min_score: float) -> bool:
    """The gates every pick must pass, whatever the caps say.

    An item needs at least one lane keyword. Evidence and recency alone can clear
    `min_score`, and without this gate an off-topic hit from the broad arXiv queries
    would be published under whichever lane the classifier fell back to -- labelled
    as if it belonged there.

    It also needs to be about Physical AI at all: either its source is topical by
    construction (a robotics feed, a cs.RO listing) or its text names at least one
    anchor term. General vendor blogs post about cloud gaming, chatbots and data
    centres far more often than about robots, and their vocabulary overlaps the lanes.
    Curated entries are exempt from both, since a person chose their lane.
    """
    if item.lane_locked:
        return item.score >= min_score
    if item.lane_hits < 1:
        return False
    if not item.topical and item.anchor_hits < 1:
        return False
    return item.score >= min_score


def select(
    items: list[Item],
    limit: int = 8,
    per_lane: int = 2,
    min_score: float = 0.9,
    seen: Iterable[str] = (),
    per_source: int | None = None,
    per_evidence: int | None = None,
) -> list[Item]:
    """Pick the daily shortlist, capping each lane so one topic cannot dominate.

    `per_source` and `per_evidence` keep one feed or one evidence class from taking
    the page. On a day when the arXiv category feeds are the only research source,
    every paper carries same-day recency and the shortlist used to come back all
    `[R]`; the official and media items that did qualify were simply outranked.
    These two caps are soft: they reserve room, they do not leave it empty. Once the
    capped pass is done, any free slots are filled from what the caps held back, in
    rank order, still under the lane cap and every gate above.

    `seen` holds normalised URLs already published on earlier days. The arXiv
    window looks back three days and feeds keep entries for thirty, so without it
    a strong item reappears on consecutive days and a daily radar stops showing
    what changed.
    """
    published = {url_key(url) for url in seen}
    eligible = [item for item in items
                if url_key(item.url) not in published and qualifies(item, min_score)]
    picked: list[int] = []
    lane_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}

    def take(index: int, item: Item) -> None:
        picked.append(index)
        lane_counts[item.lane] = lane_counts.get(item.lane, 0) + 1
        source_counts[item.source_id] = source_counts.get(item.source_id, 0) + 1
        evidence_counts[item.evidence] = evidence_counts.get(item.evidence, 0) + 1

    for capped in (True, False):
        for index, item in enumerate(eligible):
            if len(picked) >= limit:
                break
            if index in picked or lane_counts.get(item.lane, 0) >= per_lane:
                continue
            if capped and per_source is not None and source_counts.get(item.source_id, 0) >= per_source:
                continue
            if capped and per_evidence is not None and evidence_counts.get(item.evidence, 0) >= per_evidence:
                continue
            take(index, item)
    return [eligible[index] for index in sorted(picked)]


def lane_distribution(items: Iterable[Item], config: Config) -> list[tuple[str, int]]:
    counts = {lane["id"]: 0 for lane in config.lanes}
    for item in items:
        counts[item.lane] = counts.get(item.lane, 0) + 1
    return [(lane_id, count) for lane_id, count in counts.items()]


def evidence_mix(items: Iterable[Item]) -> dict[str, int]:
    mix = {"O": 0, "R": 0, "M": 0}
    for item in items:
        mix[item.evidence] = mix.get(item.evidence, 0) + 1
    return mix
