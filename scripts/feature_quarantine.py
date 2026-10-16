"""Thin terminal-worktree adapter; archive policy belongs to the private runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def configured():
    return bool(os.environ.get("NOTEFLOW_FEATURE_QUARANTINE"))


def terminal_quarantine(root: Path | None, service_state: Path, task: dict):
    entry = os.environ.get("NOTEFLOW_FEATURE_QUARANTINE")
    if not entry:
        return None  # Standalone product compatibility; production sets the pinned entry.
    path = Path(entry)
    if (root is None or not path.is_absolute() or path.resolve() != path
            or not path.is_file() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise ValueError("Terminal quarantine entrypoint is unavailable")
    try:
        fd = int(os.environ["FEATURE_LOCK_FD"])
        os.fstat(fd)
    except (KeyError, ValueError, OSError) as error:
        raise ValueError("Terminal quarantine requires the original inherited lease") from error
    response = subprocess.run(
        [str(path), "--root", str(root), "--service-state", str(service_state),
         "--task-id", task["id"], "--repository", task["repo"], "--lock-fd", str(fd)],
        capture_output=True, text=True, timeout=180, pass_fds=(fd,))
    if len(response.stdout.encode()) > 1_000_000:
        raise ValueError("Terminal quarantine response exceeds its bound")
    value = json.loads(response.stdout)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("scope") != "terminal-feature-quarantine"
            or value.get("repository") != task["repo"] or value.get("task_id") != task["id"]
            or response.returncode or value.get("state") not in {"clean", "quarantined"}):
        raise ValueError("Terminal quarantine held; retain the original task and worktree")
    return value


def before_new_task(root: Path, service_state: Path, repository: str):
    """Recover only a terminal orphan matching the clone's exact feature branch."""
    if not configured() or not root.exists():
        return
    result = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            cwd=root, capture_output=True, check=True)
    if not result.stdout:
        return
    branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=True).stdout.strip()
    prefix = "automation/feature-"
    if not branch.startswith(prefix):
        raise ValueError("Dirty clone has no terminal feature identity; reconcile before checkout")
    # Avoid directory traversal, and do not guess task IDs from correction suffixes.
    task_id = branch[len(prefix):]
    if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in task_id):
        raise ValueError("Dirty clone has an invalid feature identity")
    state = service_state / "tasks" / repository.split("/")[1] / task_id
    if not (state / "terminal.json").is_file():
        raise ValueError("Dirty clone belongs to nonterminal work; reconcile before checkout")
    terminal_quarantine(root, service_state, {"id": task_id, "repo": repository})
