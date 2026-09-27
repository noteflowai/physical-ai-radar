"""Bounded research context and dated, reviewed topic notes for feature selection."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TOPICS = Path(__file__).resolve().parents[1] / "data/feature-topics.json"


def research_context(inputs: dict) -> list[dict]:
    """Keep evening research and repository signals visible alongside model rankings."""
    groups: list[list[dict]] = [[] for _ in range(5)]
    prefixes = ("radar-evening-", "radar-", "gh-", "hf-")
    for item in inputs.get("sources", []):
        if not isinstance(item, dict):
            continue
        identifier, url = item.get("id", ""), item.get("url", "")
        if not isinstance(identifier, str) or identifier.startswith("repo-"):
            continue  # Owned-repository statistics are not external trend evidence.
        if not isinstance(url, str) or len(url) > 2000:
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                continue
        except ValueError:
            continue
        group = next((i for i, prefix in enumerate(prefixes)
                      if identifier.startswith(prefix)), 4)
        groups[group].append(item)
    # A duplicate in the morning list must not displace the evening record just
    # because that URL appears later in the evening list.
    seen = set()
    for i, group in enumerate(groups):
        unique = []
        for item in group:
            if item["url"] not in seen:
                unique.append(item)
                seen.add(item["url"])
        groups[i] = unique
    result, used = [], 0
    for index in range(max(map(len, groups), default=0)):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            entry = {}
            for key in ("id", "url", "title", "summary", "date", "issue_date",
                        "collected_at", "signal", "pipeline", "stars", "pushed_at"):
                value = item.get(key)
                if isinstance(value, str):
                    entry[key] = value[:2000 if key == "url" else 500]
                elif value is None or isinstance(value, (int, float)):
                    if key in item:
                        entry[key] = value
            size = len(json.dumps(entry, ensure_ascii=False).encode())
            if used + size > 18000:
                continue
            result.append(entry)
            used += size
            if len(result) == 20:
                return result
    return result


def topic_context(repo: str, *, today: date | None = None, path: Path = TOPICS) -> dict:
    """Optional notes expire; a broken note must not prevent ordinary feature work."""
    today = today or datetime.now(ZoneInfo("Asia/Singapore")).date()
    result = {"items": [], "diagnostics": []}
    try:
        if path.stat().st_size > 64000:
            raise ValueError("topic file exceeds 64 KB")
        topics = json.loads(path.read_text())["topics"]
        if not isinstance(topics, list) or len(topics) > 20:
            raise ValueError("expected at most 20 topic notes")
    except (OSError, ValueError, KeyError, TypeError):
        result["diagnostics"].append("Topic notes unavailable or invalid; use current research.")
        return result
    used = 0
    for item in topics:
        try:
            if not isinstance(item, dict):
                raise ValueError("invalid topic")
            checked, expires = date.fromisoformat(item["checked_on"]), date.fromisoformat(item["expires_on"])
            if not checked <= today <= expires:
                result["diagnostics"].append("Skipped topic outside its reviewed date window.")
                continue
            opportunity = item["opportunities"].get(repo)
            if opportunity is None:
                continue
            if not isinstance(opportunity, str) or not opportunity.strip():
                raise ValueError("missing opportunity")
            if any(not isinstance(item.get(key), str) or not item[key].strip()
                   for key in ("id", "title", "summary")):
                raise ValueError("invalid topic description")
            if (not isinstance(item.get("constraints"), list)
                    or not all(isinstance(value, str) for value in item["constraints"])):
                raise ValueError("invalid topic constraints")
            sources = item["sources"]
            if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
                raise ValueError("missing source links")
            if any(not isinstance(url, str) or urlsplit(url).scheme != "https"
                   or not urlsplit(url).netloc for url in sources):
                raise ValueError("invalid source link")
            entry = {key: item[key] for key in
                     ("id", "title", "checked_on", "expires_on", "summary", "sources", "constraints")}
            entry["opportunity"] = opportunity
            size = len(json.dumps(entry, ensure_ascii=False).encode())
            if size > 8000 or used + size > 12000 or len(result["items"]) >= 3:
                result["diagnostics"].append("Skipped topic outside the context budget.")
                continue
            result["items"].append(entry)
            used += size
        except (KeyError, ValueError, TypeError, AttributeError):
            result["diagnostics"].append("Skipped malformed topic note; use current research.")
    return result
