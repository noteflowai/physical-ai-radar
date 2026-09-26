#!/usr/bin/env python3
"""Run the night maintenance stages under radar-run's shared lock."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

try:
    from .agent_pipeline import write_json
    from .job_schedule import night_day
except ImportError:
    from agent_pipeline import write_json
    from job_schedule import night_day


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute(argv: list[str], root: Path, env: dict, log: Path, seconds: int) -> int:
    """Bound a whole stage process tree; outer bootstrap termination also cleans it up."""
    with log.open("ab", buffering=0) as output:
        child = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)

        def stop() -> None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            # Descendants may survive even if their parent has already exited.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()

        def interrupted(signum, _frame):
            stop()
            raise SystemExit(128 + signum)

        previous = {sig: signal.signal(sig, interrupted)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            try:
                return child.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                stop()
                return 124
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def run_batch(root: Path, state_root: Path, day: str, executor=execute) -> dict:
    state = state_root / day
    state.mkdir(parents=True, exist_ok=True)
    receipt = state / "run.json"
    saved = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {
        "day": day, "timezone": "Asia/Singapore", "stages": {},
    }
    if saved["day"] != day:
        raise ValueError("Night receipt has a different batch date")
    if saved.get("status") == "complete":
        print(json.dumps({"status": "already-complete", "day": day}), flush=True)
        return saved
    saved.update(status="running", started_at=timestamp())
    write_json(receipt, saved)
    env = {**os.environ, "RADAR_RUN_DAY": day, "RADAR_LOCK_HELD": "1",
           "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PIP_NO_INPUT": "1"}
    fresh = state / "fresh-radar"
    steps = [
        ("sources", ["bash", "scripts/repair_sources.sh"], 2400),
        ("publish", ["bash", "scripts/publish_daily.sh", "--date", day], 2400),
        ("fresh", [sys.executable, "-m", "pairadar", "--date", day,
                   "--no-readme", "--out", str(fresh)], 600),
        ("notes", ["bash", "scripts/draft_daily_notes.sh", "--date", day], 5400),
        ("repos", ["bash", "scripts/improve_repos.sh", "--date", day], 5400),
    ]
    for name, argv, seconds in steps:
        old = saved["stages"].get(name, {})
        if old.get("status") == "complete":
            continue
        if name == "notes" and saved["stages"].get("publish", {}).get("status") != "complete":
            saved["stages"][name] = {"status": "waiting", "reason": "daily publication not verified"}
            write_json(receipt, saved)
            continue
        if name == "repos" and saved["stages"].get("fresh", {}).get("status") == "complete":
            argv += ["--radar-snapshot", str(fresh / "radar/latest.json")]
        record = {"status": "running", "started_at": timestamp(),
                  "attempts": old.get("attempts", 0) + 1}
        saved["stages"][name] = record
        write_json(receipt, saved)
        print(f"[night {day}] {name}: attempt {record['attempts']}", flush=True)
        try:
            code = executor(argv, root, env, state / f"{name}.log", seconds)
            if code == 0 and name == "notes" and not (root / f"data/notes/{day}.json").is_file():
                raise RuntimeError("Notes command returned without publishing the day's notes")
            if code == 0 and name == "fresh":
                data = json.loads((fresh / "radar/latest.json").read_text(encoding="utf-8"))
                if data.get("date") != day:
                    raise RuntimeError("Fresh snapshot has a different batch date")
            record.update(exit_code=code, status="complete" if code == 0 else "retry-needed")
        except Exception as error:
            record.update(status="retry-needed", error=str(error))
        record["finished_at"] = timestamp()
        write_json(receipt, saved)
        print(f"[night {day}] {name}: {record['status']}", flush=True)
    saved.update(status="complete" if all(
        saved["stages"].get(name, {}).get("status") == "complete"
        for name, _, _ in steps) else "retry-needed", finished_at=timestamp())
    write_json(receipt, saved)
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="explicit issue-date override for a controlled manual run")
    parser.add_argument("--previous-day", action="store_true",
                        help="02:30 catch-up: resume the preceding Singapore evening")
    parser.add_argument("--state", type=Path, default=Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "pairadar/nightly")
    args = parser.parse_args()
    if os.environ.get("RADAR_LOCK_HELD") != "1":
        parser.error("Use scripts/nightly.sh or radar-run to hold the shared clone lock")
    os.umask(0o077)
    result = run_batch(Path(__file__).resolve().parents[1], args.state.resolve(),
                       night_day(args.date, previous_day=args.previous_day))
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
