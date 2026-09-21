"""Shared configuration and data loading for Physical AI Radar.

The whole pipeline is intentionally dependency-free (standard library only) so the
daily GitHub Actions run stays fast, reproducible and easy to audit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RADAR_DIR = ROOT / "radar"
DAILY_DIR = RADAR_DIR / "daily"
ASSETS_DIR = ROOT / "assets"

LANGS: tuple[str, ...] = ("zh", "en", "ja")
README_FILES = {"zh": "README.md", "en": "README.en.md", "ja": "README.ja.md"}
MARKER_START = "<!-- RADAR:START -->"
MARKER_END = "<!-- RADAR:END -->"


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON document, raising a clear error if it is malformed."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - configuration error
        raise SystemExit(f"missing data file: {path}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - configuration error
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


@dataclass(frozen=True)
class Config:
    """Loaded configuration bundle."""

    sources: dict[str, Any]
    taxonomy: dict[str, Any]
    glossary: dict[str, Any]
    baseline: dict[str, Any]

    @property
    def lanes(self) -> list[dict[str, Any]]:
        return list(self.taxonomy["lanes"])

    def lane(self, lane_id: str) -> dict[str, Any]:
        for lane in self.lanes:
            if lane["id"] == lane_id:
                return lane
        raise KeyError(lane_id)

    def lane_name(self, lane_id: str, lang: str) -> str:
        return self.lane(lane_id)["name"][lang]

    def chart_label(self, lane_id: str, lang: str = "en") -> str:
        """Label for plotting. English full names overrun the gutter, so the taxonomy
        carries a curated short form; Chinese and Japanese names are already compact
        enough that the localized name is the honest label, trimmed only if it is not."""
        label = self.lane(lane_id).get("chart_label")
        if isinstance(label, dict):
            if label.get(lang):
                return label[lang]
        elif label and lang == "en":
            return label
        return self.lane(lane_id)["name"][lang]
        lane = self.lane(lane_id)
        return lane.get("chart_label") or lane["name"]["en"]

    def ui(self, lang: str) -> dict[str, Any]:
        return self.glossary["ui"][lang]


NOTES_DIR = DATA_DIR / "notes"
NOTES_SCHEMA = "pairadar-notes-1"


def load_notes(day: str, directory: Path = NOTES_DIR) -> dict[str, Any]:
    """Load one day of drafted per-item analysis, or nothing.

    The file is optional and authored outside the deterministic pipeline (see
    docs/METHODOLOGY.md section 5), so a missing or malformed file must never stop
    a run -- the radar falls back to its per-lane templates.
    """
    path = directory / f"{day}.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[notes] ignoring {path}: {exc}")
        return {}
    if document.get("schema") != NOTES_SCHEMA or not isinstance(document.get("notes"), dict):
        print(f"[notes] ignoring {path}: not a {NOTES_SCHEMA} document")
        return {}
    return document


def load_config() -> Config:
    return Config(
        sources=load_json(DATA_DIR / "sources.json"),
        taxonomy=load_json(DATA_DIR / "taxonomy.json"),
        glossary=load_json(DATA_DIR / "glossary.json"),
        baseline=load_json(DATA_DIR / "baseline.json"),
    )


@dataclass
class Item:
    """One tracked artefact (paper, official post or tracked news item)."""

    id: str
    title: str
    url: str
    publisher: str
    source_id: str
    evidence: str
    published: str
    summary: str = ""
    lane: str = "foundation"
    lane_locked: bool = False
    lane_hits: int = 0
    score: float = 0.0
    numbers: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "publisher": self.publisher,
            "source_id": self.source_id,
            "evidence": self.evidence,
            "published": self.published,
            "lane": self.lane,
            "score": round(self.score, 3),
            "numbers": self.numbers,
            "signals": self.signals,
            # The rendered page quotes an excerpt of this, so a snapshot without it
            # cannot reproduce the page it describes.
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Item":
        """Rebuild an item from a published snapshot, for re-rendering without a fetch."""
        return cls(
            id=payload["id"], title=payload["title"], url=payload["url"],
            publisher=payload.get("publisher", ""), source_id=payload.get("source_id", ""),
            evidence=payload.get("evidence", "M"), published=payload.get("published", ""),
            summary=payload.get("summary", ""), lane=payload.get("lane", "foundation"),
            lane_locked=True,  # the lane was decided when the snapshot was published
            score=float(payload.get("score", 0.0)),
            numbers=list(payload.get("numbers", [])),
            signals=list(payload.get("signals", [])),
        )


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
