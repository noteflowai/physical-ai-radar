"""Radar domain steps for the installed private batch controller.

No scheduling, retry, receipt mutation or process supervision belongs here.
These are the original version-3 commands and product readback conditions.
"""
from __future__ import annotations

import json
import os
import sys


def steps(root, state, day):
    return [
        ("fresh", [sys.executable, "-m", "pairadar", "--date", day,
                   "--no-readme", "--out", str(state / "fresh-radar")], 300),
        ("repos", ["bash", "scripts/improve_repos.sh", "--date", day], 10800),
        ("updates", [sys.executable, "scripts/feature_updates.py"], 600),
        ("sources", ["bash", "scripts/repair_sources.sh"], 600),
        ("publish", ["bash", "scripts/publish_daily.sh", "--date", day], 600),
        ("notes", ["bash", "scripts/draft_daily_notes.sh", "--date", day], 1200),
        ("report", [sys.executable, "scripts/feature_report.py", "--date", day], 60),
    ]


def environment(day):
    return {**os.environ, "RADAR_RUN_DAY": day, "RADAR_LOCK_HELD": "1",
            "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PIP_NO_INPUT": "1"}


def prepare(name, argv, saved, state):
    if name == "notes" and saved["stages"].get("publish", {}).get("status") != "complete":
        return argv, "daily publication not verified"
    if name == "repos" and saved["stages"].get("fresh", {}).get("status") == "complete":
        argv += ["--radar-snapshot", str(state / "fresh-radar/radar/latest.json")]
    return argv, None


def invalidates(name):
    return (["updates"] if name == "repos" else []) + (["report"] if name != "report" else [])


def deferred(name, code):
    return name == "repos" and code == 20


def validate(name, code, root, state, day):
    if code == 0 and name == "notes" and not (root / f"data/notes/{day}.json").is_file():
        raise RuntimeError("Notes command returned without publishing the day's notes")
    if code == 0 and name == "fresh":
        data = json.loads((state / "fresh-radar/radar/latest.json").read_text())
        if data.get("date") != day:
            raise RuntimeError("Fresh snapshot has a different batch date")
