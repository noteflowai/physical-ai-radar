"""Domain shim for the installed private original-effect recovery policy."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def check_original_effects(service_state: Path, repository: str):
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
    result = subprocess.run([str(path), "--service-state", str(service_state),
                             "--repository", repository], capture_output=True, text=True, timeout=30)
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
    if result.returncode != 0 or value.get("state") not in {"clear", "resume-original"}:
        raise ValueError("Original effect recovery is unavailable")
    return value
