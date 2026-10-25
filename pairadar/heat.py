"""Heat: how much attention an item is getting, used for ranking and nothing else.

Four components, each capped and scaled to 0..1, then averaged with the weights in
`sources.json` `heat`:

- `cross`: independent sources telling the same story. Items are joined into one
  event when their links normalise to the same URL (`distill.url_key`) or, for
  anything but a paper, when their titles tell the same story (`distill.same_story`).
  The count is distinct `source_id`s; two is the first that scores.
- `hf`: Hugging Face Daily Papers upvotes for an arXiv paper.
- `hn`: Hacker News points of a story linking the item, or titled as the same story
  (Algolia search API, recent stories only).
- `stars`: stars gained per day by a tracked GitHub repository the item is about
  (its release feed, or a link into the repository), from the count kept in
  `radar/heat-state.json` on the previous run.

The weighted heat is added to the score, at most `heat.weight`. It never touches
`evidence`: a story on every robotics site is still `[M]`, and a paper with many
upvotes is still a self-reported `[R]`. An item with two or more independent sources
or a heat of at least `heat.trending` carries the `trending` signal, which the
highlights quota reads.

Every request is a documented public API: robots.txt is honoured, requests to one
host are spaced, and each endpoint that does not answer is recorded and skipped.
"""
from __future__ import annotations

import json
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config, Item, ROOT
from .distill import same_story, story_terms, url_key
from . import fetch

STATE_PATH = ROOT / "radar" / "heat-state.json"
STATE_SCHEMA = "pairadar-heat-1"
MAX_QUERIES = 6
MAX_REPOS = 8
_ARXIV_ABS = re.compile(r"^https://arxiv\.org/abs/(\d{4}\.\d{4,5})$")
_GITHUB_REPO = re.compile(r"^https://github\.com/([a-z0-9_.-]+/[a-z0-9_.-]+)(?:/|$)")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "weight": 0.8,
    "trending": 0.3,
    "components": {"cross": 0.4, "hf": 0.25, "hn": 0.2, "stars": 0.15},
    "caps": {"sources": 4, "hf_upvotes": 100, "hn_points": 300, "stars_per_day": 200},
    "hf_daily_papers": "https://huggingface.co/api/daily_papers",
    "hn_search": "https://hn.algolia.com/api/v1/search",
    "hn_queries": [],
    "hn_days": 3,
    "github_repos": [],
}


def settings(config: Config) -> dict[str, Any]:
    raw = config.sources.get("heat", {}) or {}
    merged = {**DEFAULTS, **raw}
    merged["components"] = {**DEFAULTS["components"], **raw.get("components", {})}
    merged["caps"] = {**DEFAULTS["caps"], **raw.get("caps", {})}
    merged["hn_queries"] = list(merged["hn_queries"])[:MAX_QUERIES]
    merged["github_repos"] = [repo.lower() for repo in merged["github_repos"]][:MAX_REPOS]
    return merged


@dataclass
class Signals:
    upvotes: dict[str, int] = field(default_factory=dict)       # arXiv id -> upvotes
    stories: list[dict[str, Any]] = field(default_factory=list)  # HN: key, terms, points
    stars: dict[str, int] = field(default_factory=dict)          # owner/repo -> stars
    attempted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    robots_skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"attempted": sorted(self.attempted), "failed": sorted(self.failed),
                "robots_skipped": sorted(self.robots_skipped), "hf_papers": len(self.upvotes),
                "hn_stories": len(self.stories), "repos": len(self.stars)}


def _json(url: str, name: str, signals: Signals, robots: fetch.RobotsCache | None,
          getter: Callable[[str], bytes | None]) -> Any:
    """One request; None (recorded) when robots.txt disallows it or it does not answer."""
    signals.attempted.append(name)
    verdict = robots.verdict(url) if robots is not None else fetch.ALLOW
    if verdict == fetch.DISALLOW:
        signals.robots_skipped.append(name)
        return None
    if verdict == fetch.UNREACHABLE:
        signals.failed.append(name)
        return None
    fetch.polite(url)
    payload = getter(url)
    try:
        if payload:
            return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        pass
    signals.failed.append(name)
    return None


def fetch_signals(config: Config, reference: date, robots: fetch.RobotsCache | None = None,
                  getter: Callable[[str], bytes | None] | None = None) -> Signals:
    """Read the three public APIs. Never raises; what did not answer is in `failed`."""
    cfg = settings(config)
    getter = getter or (lambda url: fetch.http_get(url, retries=1))
    signals = Signals()
    if not cfg["enabled"]:
        return signals
    papers = _json(cfg["hf_daily_papers"], "hf_daily_papers", signals, robots, getter)
    for entry in papers if isinstance(papers, list) else []:
        paper = entry.get("paper", {}) if isinstance(entry, dict) else {}
        if isinstance(paper.get("id"), str) and isinstance(paper.get("upvotes"), int):
            signals.upvotes[paper["id"]] = max(signals.upvotes.get(paper["id"], 0), paper["upvotes"])
    since = int(datetime.combine(reference - timedelta(days=int(cfg["hn_days"])), datetime.min.time(),
                                 tzinfo=timezone.utc).timestamp())
    seen: set[str] = set()
    for query in cfg["hn_queries"]:
        url = cfg["hn_search"] + "?" + urllib.parse.urlencode({
            # typoTolerance=false: with it, "robot" also matched "root".
            "query": query, "tags": "story", "typoTolerance": "false",
            "numericFilters": f"created_at_i>{since}", "hitsPerPage": 100})
        reply = _json(url, f"hn:{query}", signals, robots, getter)
        for hit in (reply or {}).get("hits", []) if isinstance(reply, dict) else []:
            if not isinstance(hit, dict) or hit.get("objectID") in seen:
                continue
            seen.add(hit.get("objectID"))
            points = hit.get("points") if isinstance(hit.get("points"), int) else 0
            link = hit.get("url") if isinstance(hit.get("url"), str) else ""
            signals.stories.append({"key": url_key(link) if link else "",
                                    "terms": story_terms(str(hit.get("title") or "")),
                                    "points": points})
    for repo in cfg["github_repos"]:
        reply = _json(f"https://api.github.com/repos/{repo}", f"github:{repo}", signals, robots, getter)
        if isinstance(reply, dict) and isinstance(reply.get("stargazers_count"), int):
            signals.stars[repo] = reply["stargazers_count"]
    return signals


def _scaled(value: float, cap: float) -> float:
    if value <= 0 or cap <= 0:
        return 0.0
    return math.log1p(min(value, cap)) / math.log1p(cap)


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": STATE_SCHEMA, "stars": {}}
    if state.get("schema") != STATE_SCHEMA or not isinstance(state.get("stars"), dict):
        return {"schema": STATE_SCHEMA, "stars": {}}
    return state


def next_state(state: dict[str, Any], stars: dict[str, int], day: str) -> dict[str, Any]:
    """Keep the count before today's for each repository, so a second run on one day
    still measures against yesterday. A repository not read today keeps its entry."""
    out = {"schema": STATE_SCHEMA, "stars": {}}
    for repo, entry in state.get("stars", {}).items():
        out["stars"][repo] = dict(entry)
    for repo, count in stars.items():
        entry = out["stars"].get(repo)
        if entry and entry.get("date") == day:
            entry["count"] = count
        else:
            out["stars"][repo] = {"count": count, "date": day,
                                  **({"prev_count": entry["count"], "prev_date": entry["date"]} if entry else {})}
    return out


def velocity(entry: dict[str, Any] | None) -> float:
    if not entry or "prev_count" not in entry:
        return 0.0
    try:
        days = (date.fromisoformat(entry["date"]) - date.fromisoformat(entry["prev_date"])).days
    except (KeyError, ValueError):
        return 0.0
    return max(0.0, (entry["count"] - entry["prev_count"]) / days) if days > 0 else 0.0


def _repo_of(item: Item, feeds: dict[str, dict[str, Any]], tracked: Iterable[str]) -> str | None:
    repo = str(feeds.get(item.source_id, {}).get("repo", "")).lower()
    if repo:
        return repo
    match = _GITHUB_REPO.match(url_key(item.url))
    return match.group(1) if match and match.group(1) in tracked else None


def clusters(items: list[Item]) -> list[list[int]]:
    """Indexes of items telling one event: same normalised URL, or (not papers) the same story."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keys = [url_key(item.url) for item in items]
    terms = [story_terms(item.title) if item.evidence != "R" else None for item in items]
    first: dict[str, int] = {}
    for i, key in enumerate(keys):
        if key in first:
            parent[find(i)] = find(first[key])
        else:
            first[key] = i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if terms[i] is not None and terms[j] is not None and same_story(terms[i], terms[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def apply(items: list[Item], config: Config, signals: Signals | None = None,
          state: dict[str, Any] | None = None) -> list[Item]:
    """Set `heat`, add it to the score, mark `trending`; returns the list re-ranked.

    `state` is the repository star counts after this run (`next_state`), used for the
    stars-per-day component.
    """
    cfg = settings(config)
    if not cfg["enabled"]:
        return items
    signals = signals or Signals()
    weights, caps = cfg["components"], cfg["caps"]
    total = sum(weights.values()) or 1.0
    feeds = {feed["id"]: feed for feed in config.sources.get("feeds", [])}
    stars = (state or {}).get("stars", {})
    for group in clusters(items):
        members = [items[i] for i in group]
        sources = len({item.source_id for item in members})
        cross = (min(sources, caps["sources"]) - 1) / max(1, caps["sources"] - 1)
        hf = hn = gh = 0.0
        for item in members:
            paper = _ARXIV_ABS.match(url_key(item.url))
            if paper:
                hf = max(hf, _scaled(signals.upvotes.get(paper.group(1), 0), caps["hf_upvotes"]))
            key, item_terms = url_key(item.url), story_terms(item.title)
            for story in signals.stories:
                if (story["key"] and story["key"] == key) or (
                        item.evidence != "R" and same_story(item_terms, story["terms"])):
                    hn = max(hn, _scaled(story["points"], caps["hn_points"]))
            repo = _repo_of(item, feeds, cfg["github_repos"])
            if repo:
                gh = max(gh, _scaled(velocity(stars.get(repo)), caps["stars_per_day"]))
        heat = (weights["cross"] * cross + weights["hf"] * hf + weights["hn"] * hn
                + weights["stars"] * gh) / total
        for item in members:
            item.heat = round(heat, 4)
            item.score += cfg["weight"] * heat
            if (sources >= 2 or heat >= cfg["trending"]) and "trending" not in item.signals:
                item.signals.append("trending")
    items.sort(key=lambda entry: (-entry.score, entry.published, entry.title))
    return items


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
