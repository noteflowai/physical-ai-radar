"""Controller-owned roadmap priorities and bounded, reviewable work decisions."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
from pathlib import Path
import re

MAX_ROADMAP_BYTES = 24000
MAX_ROADMAP_AGE_DAYS = 35
WORK_TYPES = {"feature", "fix", "maintenance", "no-change"}


def load_roadmap(root: Path, revision: str, today: str) -> dict:
    path = root / "ROADMAP.md"
    if path.is_symlink() or not path.is_file():
        raise ValueError("A regular, reviewed ROADMAP.md is required before paid planning")
    if path.stat().st_size > MAX_ROADMAP_BYTES:
        raise ValueError("ROADMAP.md exceeds the bounded planning context")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    dated = re.search(r"(?:Updated:|更新[：:])\s*(\d{4}-\d{2}-\d{2})", text)
    if not dated:
        raise ValueError("ROADMAP.md needs an Updated:/更新 date")
    updated, current = date.fromisoformat(dated[1]), date.fromisoformat(today)
    if updated > current:
        raise ValueError("ROADMAP.md update date is in the future")
    section, milestones = "", {}
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().split(" ", 1)[0].lower()
        match = re.match(r"^\|\s*([A-Z][A-Z0-9]{1,7}-\d{2,3})\s*\|(.+)", line)
        if match and section == "now":
            if match[1] in milestones:
                raise ValueError("ROADMAP.md has duplicate Now milestone IDs")
            milestones[match[1]] = match[2].strip()
    if not milestones:
        raise ValueError("ROADMAP.md needs a Now section with stable milestone IDs")
    return {"path": "ROADMAP.md", "revision": revision,
            "sha256": hashlib.sha256(raw).hexdigest(), "updated_on": updated.isoformat(),
            "stale": (current - updated).days > MAX_ROADMAP_AGE_DAYS,
            "now": milestones, "text": text}


def validate_decision(candidate: dict, roadmap: dict, today: str) -> None:
    kind = candidate.get("work_type")
    if kind not in WORK_TYPES:
        raise ValueError("work_type must be feature, fix, maintenance or no-change")
    if candidate.get("milestone_id") not in roadmap["now"]:
        raise ValueError("Choose a milestone in ROADMAP.md's Now section; Later is not authorized")
    if roadmap["stale"] and kind == "feature":
        raise ValueError("Roadmap is stale: review priorities before proposing a new feature")
    for field in ("problem_evidence", "expected_outcome"):
        if not isinstance(candidate.get(field), str) or not 20 <= len(candidate[field]) <= 3000:
            raise ValueError(f"{field} must contain 20..3000 characters of concrete evidence")
    try:
        follow_up = date.fromisoformat(candidate.get("follow_up_on", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("follow_up_on must be an ISO calendar date") from error
    current = date.fromisoformat(today)
    if not current < follow_up <= current + timedelta(days=30):
        raise ValueError("Schedule a follow-up 1..30 days from the current Singapore date")
    if kind == "no-change":
        if not isinstance(candidate.get("reason"), str) or not 20 <= len(candidate["reason"]) <= 3000:
            raise ValueError("A no-change decision needs a concrete reason, not a delivery claim")
        if any(candidate.get(field) for field in ("edits", "decision_probe", "test_path")):
            raise ValueError("No-change cannot execute edits, probes or acceptance tests")


def bind_decision(candidate: dict, roadmap: dict) -> None:
    """The model cannot supply the authoritative roadmap revision or hash."""
    candidate["governance"] = {"version": 1, **{
        key: roadmap[key] for key in ("path", "revision", "sha256", "updated_on", "stale")}}


def next_version(old: str, work_type: str = "feature") -> str:
    major, minor, patch = map(int, old.split("."))
    if work_type == "feature":
        return f"{major}.{minor + 1}.0"
    if work_type in {"fix", "maintenance"}:
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("Only reviewed code work may advance a release version")
