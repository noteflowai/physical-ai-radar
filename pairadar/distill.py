"""Distillation layer: classify, extract quantitative claims, score and select.

The ranking is deliberately explainable — every component is a small additive term so a
reader can reconstruct why an item was picked. No opaque model sits in this path.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable

from .config import Config, Item
from .fetch import source_weight

NUMBER_PATTERNS = (
    r"\b\d+(?:\.\d+)?\s?%",
    r"\b\d+(?:\.\d+)?\s?(?:x|×)\b",
    r"\b\d+(?:\.\d+)?\s?(?:ms|Hz|hz|fps|FPS)\b",
    r"\b\d+(?:\.\d+)?\s?[BMK]?\s?(?:parameters|params)\b",
    r"\b\d+(?:,\d{3})*(?:\.\d+)?\s?(?:hours|episodes|demonstrations|trajectories|tasks)\b",
    r"\b\d+(?:\.\d+)?\s?(?:GB|TB|W|kg|DoF|dof)\b",
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


def detect_signals(text: str, taxonomy: dict[str, Any]) -> list[str]:
    lowered = text.lower()
    signals: list[str] = []
    for signal in taxonomy.get("signals", []):
        sid = signal["id"]
        if "keywords" in signal:
            if any(keyword in lowered for keyword in signal["keywords"]):
                signals.append(sid)
        elif "pattern" in signal:
            if re.search(signal["pattern"], text):
                signals.append(sid)
    return signals


def lane_hits(text: str, lane: dict[str, Any]) -> int:
    """Count how many of a lane's keywords appear in the text."""
    lowered = text.lower()
    return sum(1 for keyword in lane["keywords"] if keyword in lowered)


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
    articles; neither belongs on a radar that claims to show what changed today.
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
            age = (reference - datetime.fromisoformat(item.published).date()).days
        except ValueError:
            age = 0
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


def url_key(url: str) -> str:
    """Normalise a link so the same article compares equal across runs and feeds."""
    return url.split("?")[0].rstrip("/").lower()


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


def select(
    items: list[Item],
    limit: int = 8,
    per_lane: int = 2,
    min_score: float = 0.9,
    seen: Iterable[str] = (),
) -> list[Item]:
    """Pick the daily shortlist, capping each lane so one topic cannot dominate.

    An item needs at least one lane keyword to qualify. Evidence and recency alone
    can clear `min_score`, and without this gate an off-topic hit from the broad
    arXiv queries would be published under whichever lane the classifier fell back
    to -- labelled as if it belonged there.

    `seen` holds normalised URLs already published on earlier days. The arXiv
    window looks back three days and feeds keep entries for thirty, so without it
    a strong item reappears on consecutive days and a daily radar stops showing
    what changed.
    """
    published = {url_key(url) for url in seen}
    picked: list[Item] = []
    lane_counts: dict[str, int] = {}
    for item in items:
        if url_key(item.url) in published:
            continue
        if item.lane_hits < 1 and not item.lane_locked:
            continue
        if item.score < min_score:
            continue
        if lane_counts.get(item.lane, 0) >= per_lane:
            continue
        picked.append(item)
        lane_counts[item.lane] = lane_counts.get(item.lane, 0) + 1
        if len(picked) >= limit:
            break
    return picked


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
