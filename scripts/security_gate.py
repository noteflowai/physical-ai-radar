"""Thin adapter to the separately pinned publication-security service."""
import hashlib
import json
from pathlib import Path
import subprocess


def require_safe_publication(text: str):
    data = text.encode("utf-8")
    try:
        result = subprocess.run(
            [str(Path.home() / ".local/bin/noteflow-publication-check"), "stdin"],
            input=data, capture_output=True, timeout=210)
        report = json.loads(result.stdout)
        if (result.returncode != 0 or report.get("policy") != "aws-security-first-v1"
                or report.get("state") != "passed"
                or report.get("source_sha256") != hashlib.sha256(data).hexdigest()
                or report.get("publication_approved") is not False
                or report.get("cloud_write_authorized") is not False
                or report.get("findings") != []):
            raise ValueError("invalid or blocked security receipt")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        raise RuntimeError("publication-security-blocked-or-unavailable") from None
    return report
