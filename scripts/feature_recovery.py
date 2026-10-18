"""Domain shim for the installed private original-effect recovery policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def entrypoint():
    # The production owner sets this to its pinned private entrypoint. Public
    # product/library tests remain independent of the operator's private repo.
    entry = os.environ.get("NOTEFLOW_FEATURE_RECOVERY")
    if not entry:
        return None
    path = Path(entry)
    if (not path.is_absolute() or path.resolve() != path or not path.is_file()
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022
            or not os.access(path, os.X_OK)):
        raise ValueError("Configured private recovery entrypoint is unavailable")
    return path


def check_original_effects(service_state: Path, repository: str):
    path = entrypoint()
    if path is None:
        return None
    result = subprocess.run([str(path), "--service-state", str(service_state),
                             "--repository", repository], capture_output=True, text=True, timeout=180)
    if len(result.stdout.encode()) > 1_000_000:
        raise ValueError("Original recovery response exceeds its bound")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ValueError("Original effect recovery did not return a valid observation") from error
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("repository") != repository
            or value.get("scope") != "original-feature-effect-recovery"):
        raise ValueError("Original recovery observation has a different identity")
    if result.returncode == 20 and value.get("state") == "reconcile":
        raise ValueError("Reconcile the original native effect before creating or reauthoring work")
    if result.returncode != 0 or value.get("state") not in {"clear", "resume-original", "close-original"}:
        raise ValueError("Original effect recovery is unavailable")
    return value


def close_original(service_state: Path, repository: str, observation: dict):
    """Consume exactly the observed resolution through the owner's original lease."""
    import re
    proof = observation.get("external_finalization", {})
    identifier, sha = proof.get("task_id"), proof.get("resolution_sha256")
    descriptor = os.environ.get("FEATURE_LOCK_FD", "")
    if (proof.get("state") != "close-original"
            or identifier != observation.get("active_task_id")
            or not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier)
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not descriptor.isdecimal() or entrypoint() is None):
        raise ValueError("Original closure identity and inherited lease required")
    fd = int(descriptor)
    result = subprocess.run([str(entrypoint()), "consume-external-finalization",
                             "--service-state", str(service_state), "--repository", repository,
                             "--task-id", identifier, "--resolution-sha256", sha,
                             "--lock-fd", str(fd)], pass_fds=(fd,), capture_output=True,
                            text=True, timeout=180)
    if len(result.stdout.encode()) > 1_000_000:
        raise ValueError("External consumption response exceeds bound; reconcile")
    value = json.loads(result.stdout)
    if (result.returncode != 0 or not isinstance(value, dict)
            or value.get("schema_version") != 1 or value.get("repository") != repository
            or value.get("scope") != "original-feature-effect-recovery"
            or value.get("state") != "externally-finalized"
            or value.get("status") != "externally-finalized"
            or value.get("feature_id") != identifier or value.get("repo") != repository
            or value.get("resolution_sha256") != sha
            or any(value.get(k) is not False for k in (
                "historical_approval_proven", "runtime_acceptance_proven", "showcase_e2e_proven"))):
        raise ValueError("Original external consumption uncertain; reconcile")
    return value
