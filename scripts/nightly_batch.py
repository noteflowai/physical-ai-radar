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
import time
from typing import Any

try:
    from .agent_pipeline import write_json
    from .job_schedule import night_day
    from .feature_outcomes import DEFERRED_EXIT, TERMINAL_BATCH
except ImportError:
    from agent_pipeline import write_json
    from job_schedule import night_day
    from feature_outcomes import DEFERRED_EXIT, TERMINAL_BATCH


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute(argv: list[str], root: Path, env: dict, log: Path, seconds: int) -> int:
    """Bound a whole stage process tree; outer bootstrap termination also cleans it up."""
    pass_fds = ()
    if env.get("FEATURE_LOCK_FD"):
        fd = int(env["FEATURE_LOCK_FD"])
        os.fstat(fd)
        pass_fds = (fd,)
    with log.open("ab", buffering=0) as output:
        # A termination request (SIGTERM, SIGINT) is recorded by the handler and acted on here, in the ordinary flow of this
        # function, never inside the handler:
        #  - the handler only appends the signal to `arrived` and, when the stage already exists, asks its process group to
        #    end (`killpg` takes no lock). It does not wait: a signal that arrives while this function is inside
        #    `child.wait()` holds `Popen`'s non-reentrant wait lock, and a second `wait()` from the handler would never
        #    get it;
        #  - a request that is already known is consumed BEFORE the stage is started, so no stage starts that is then
        #    stopped (and none whose effects cannot be taken back);
        #  - a request that arrives while `Popen` runs finds no `child` yet: the stage may exist but this function does not
        #    hold it, so the handler must not leave. The check right after `Popen` then stops the stage, and if `Popen`
        #    itself fails, the request still wins: the batch must end (SystemExit), not carry on to the next stage as for an
        #    ordinary start failure. A start failure with no request is raised as it was;
        #  - the only `return` of this function is after the handlers are back, and a request recorded by then wins over the
        #    stage's code and over the cap's 124 (see the end of this function): cleaning up a stage that ran over its cap, and
        #    putting the handlers back, are both windows in which a request can arrive, and neither may turn it into an
        #    ordinary stage failure that lets the batch go on to the next stage.
        # The handlers are installed with both signals blocked, so that a SIGINT or SIGTERM cannot take the default action
        # (KeyboardInterrupt) between the two installations and leave one handler behind. The mask is restored before
        # `Popen`: the stage inherits no mask and, since caught signals are reset by exec, no handler.
        child = None
        arrived: list[int] = []

        def stop() -> None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            # Descendants may survive even if their parent has already exited.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            child.wait()

        def interrupted(signum, _frame):
            arrived.append(signum)
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)       # ask the stage to end now; the flow below reaps it
                except (ProcessLookupError, PermissionError):
                    pass

        previous: dict[int, Any] = {}
        held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous[sig] = signal.signal(sig, interrupted)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, held)
            if arrived:
                raise SystemExit(128 + arrived[0])
            try:
                child = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=pass_fds)
            except BaseException:
                if arrived:
                    raise SystemExit(128 + arrived[0]) from None
                raise
            deadline = time.monotonic() + seconds
            code = 124                           # what the stage cap gives; the stage's own code replaces it below
            timed_out = False
            while not arrived:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    code = child.wait(timeout=min(remaining, 0.25))
                except subprocess.TimeoutExpired:
                    continue
                break
            if arrived or timed_out:
                stop()                           # the stage is cleaned up whichever came first, request or cap
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        # The one exit of this function that returns: a request recorded at any point up to here, including while the stage
        # was being cleaned up and while the handlers were being put back, ends the batch ahead of whatever code the stage
        # or the cap gave. The handlers are back by now, so a request from here on takes the caller's own action.
        if arrived:
            raise SystemExit(128 + arrived[0])
        return code


def run_batch(root: Path, state_root: Path, day: str, executor=execute) -> dict:
    state = state_root / day
    state.mkdir(parents=True, exist_ok=True)
    receipt = state / "run.json"
    saved = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {
        "day": day, "timezone": "Asia/Singapore", "stages": {},
    }
    if saved["day"] != day:
        raise ValueError("Night receipt has a different batch date")
    if saved.get("status") in TERMINAL_BATCH and saved.get("pipeline_version") == 3:
        print(json.dumps({"status": "already-terminal", "outcome": saved["status"], "day": day}), flush=True)
        return saved
    saved.pop("finished_at", None)
    saved.update(status="running", started_at=timestamp(), pipeline_version=3)
    write_json(receipt, saved)
    env = {**os.environ, "RADAR_RUN_DAY": day, "RADAR_LOCK_HELD": "1",
           "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "PIP_NO_INPUT": "1"}
    fresh = state / "fresh-radar"
    steps = [
        ("fresh", [sys.executable, "-m", "pairadar", "--date", day,
                   "--no-readme", "--out", str(fresh)], 300),
        # Reserve the feature budget before editorial tasks. The sum of all stage
        # caps is 236 minutes, within the bootstrap's four-hour wall-clock limit.
        ("repos", ["bash", "scripts/improve_repos.sh", "--date", day], 10800),
        ("updates", [sys.executable, "scripts/feature_updates.py"], 600),
        ("sources", ["bash", "scripts/repair_sources.sh"], 600),
        ("publish", ["bash", "scripts/publish_daily.sh", "--date", day], 600),
        ("notes", ["bash", "scripts/draft_daily_notes.sh", "--date", day], 1200),
        ("report", [sys.executable, "scripts/feature_report.py", "--date", day], 60),
    ]
    for name, argv, seconds in steps:
        old = saved["stages"].get(name, {})
        if old.get("status") in {"complete", "deferred"}:
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
        if name == "repos":
            saved["stages"].pop("updates", None)
        if name != "report":
            saved["stages"].pop("report", None)
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
            status = ("deferred" if name == "repos" and code == DEFERRED_EXIT
                      else "complete" if code == 0 else "retry-needed")
            record.update(exit_code=code, status=status)
        except Exception as error:
            record.update(status="retry-needed", error=str(error))
        record["finished_at"] = timestamp()
        write_json(receipt, saved)
        print(f"[night {day}] {name}: {record['status']}", flush=True)
    statuses = {saved["stages"].get(name, {}).get("status") for name, _, _ in steps}
    outcome = ("retry-needed" if statuses - {"complete", "deferred"} else
               "complete-with-deferrals" if "deferred" in statuses else "complete")
    saved.update(status=outcome, finished_at=timestamp())
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
    return (0 if result["status"] == "complete" else
            DEFERRED_EXIT if result["status"] == "complete-with-deferrals" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
