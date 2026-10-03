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


def require_safe_github_text(args):
    """Check public title/body text before controller GitHub writes."""
    if tuple(args[:2]) not in {("pr", "create"), ("pr", "edit"), ("pr", "comment"),
                              ("issue", "create"), ("issue", "edit"), ("issue", "comment")}:
        return
    fields = []
    for index, argument in enumerate(args):
        flag, separator, inline = argument.partition("=")
        if flag not in {"--title", "--body", "-b", "--body-file"}:
            continue
        try:
            value = inline if separator else args[index + 1]
            if flag == "--body-file":
                path = Path(value)
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                    raise ValueError("invalid body scope")
                value = path.read_text(encoding="utf-8")
            fields.append(value)
        except (OSError, ValueError, IndexError):
            raise RuntimeError("publication-security-content-unavailable") from None
    if not fields:
        raise RuntimeError("publication-security-explicit-text-required")
    require_safe_publication("\n".join(fields))
