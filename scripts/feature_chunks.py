"""Persist bounded author output as inert drafts until a complete feature is ready."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

try:
    from .feature_policy import allowed, matches, readable
except ImportError:
    from feature_policy import allowed, matches, readable


def recover_prefix(text: str, error: json.JSONDecodeError) -> dict:
    """Recover complete edit objects only from an unfinished first JSON object."""
    start = re.search(r"(?m)^[ \t]*(?:>[ \t]*)?(\{)", text)
    if not start:
        raise error
    raw = text[start.start(1):]
    prefix = re.match(r'\{\s*"edits"\s*:\s*\[', raw)
    if not prefix:
        raise error
    # A complete object followed by bad/ambiguous output must never be salvaged.
    decoder = json.JSONDecoder()
    try:
        decoder.raw_decode(raw)
    except json.JSONDecodeError:
        pass
    else:
        raise error
    edits, pos = [], prefix.end()
    while True:
        pos += len(raw[pos:]) - len(raw[pos:].lstrip())
        try:
            value, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            break
        if not isinstance(value, dict) or not {"path", "old", "new"} <= value.keys():
            raise error
        edits.append(value)
        pos = end + len(raw[end:]) - len(raw[end:].lstrip())
        if pos >= len(raw) or raw[pos] != ",":
            break
        pos += 1
    if not edits:
        raise error
    return {"edits": edits, "continue": True, "transport_recovered": True}


def identity(base: str, plan: dict) -> str:
    return hashlib.sha256(json.dumps([base, plan], sort_keys=True).encode()).hexdigest()


def draft_for(task: dict, base: str, plan: dict) -> dict:
    key = identity(base, plan)
    if task.get("draft", {}).get("identity") != key:
        task["draft"] = {"identity": key, "edits": [], "open_files": []}
    if not task["draft"]["edits"]:
        task.pop("source_notes", None)
    return task["draft"]


def virtual_files(root: Path, edits: list[dict], config: dict) -> dict[str, str]:
    """Use the same exact-anchor rules as apply(), without writing product files."""
    after = {}
    for edit in edits:
        path, old, new = edit["path"], edit["old"], edit["new"]
        if not isinstance(path, str) or not allowed(path, config):
            raise ValueError("Invalid draft product path")
        if not isinstance(new, str) or "\0" in new:
            raise ValueError("Invalid draft product text")
        file = root / path
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Drafts cannot follow symlinks")
        if matches(path, config["tests"]) and file.exists():
            raise ValueError("Keep existing regression tests unchanged")
        current = after[path] if path in after else file.read_text() if file.exists() else None
        if old is None:
            if current is not None or not new:
                raise ValueError("Draft new files must target absent files")
            after[path] = new
        elif not isinstance(old, str) or not old or current is None or current.count(old) != 1:
            raise ValueError(f"Draft edit must match exactly one span: {path}")
        else:
            after[path] = current.replace(old, new, 1)
    if len(edits) > 32 or len(after) > 16 or sum(len(v.encode()) for v in after.values()) > 240000:
        raise ValueError("Draft exceeds the existing daily feature size limits")
    return after


def add_chunk(root: Path, draft: dict, response: dict, config: dict) -> tuple[dict, dict | None]:
    """Atomically accept a chunk; final output still passes normal apply/review gates."""
    result = copy.deepcopy(draft)
    edits = response.get("edits")
    if not isinstance(edits, list) or len(edits) > 32:
        raise ValueError("Author chunk needs an edits array of at most 32 entries")
    continuing = response.get("continue", False)
    if not isinstance(continuing, bool):
        raise ValueError("Author continue must be a boolean")
    if continuing and not edits:
        raise ValueError("Continuation must make progress")
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("Invalid author edit")
        path, complete = edit.get("path"), edit.get("file_complete", True)
        if not isinstance(complete, bool):
            raise ValueError("file_complete must be a boolean")
        if edit.get("append") is True:
            if (path not in result["open_files"] or not isinstance(edit.get("new"), str)
                    or not edit["new"] or "old" in edit):
                raise ValueError("Append only to an explicitly open draft new file")
            target = next(e for e in result["edits"] if e["path"] == path and e["old"] is None)
            target["new"] += edit["new"]
            if complete:
                result["open_files"].remove(path)
        else:
            if not {"path", "old", "new"} <= edit.keys() or path in result["open_files"]:
                raise ValueError("Use exact edits, or append to finish an open new file")
            if not complete and edit["old"] is not None:
                raise ValueError("Only new files may be split across chunks")
            result["edits"].append({k: edit[k] for k in ["path", "old", "new"]})
            if not complete:
                result["open_files"].append(path)
    virtual_files(root, result["edits"], config)
    if continuing:
        return result, None
    if result["open_files"] or not result["edits"]:
        raise ValueError("Finish every draft file before submitting the complete feature")
    if not isinstance(response.get("delivery"), dict):
        raise ValueError("The final author chunk requires delivery metadata")
    return result, {"edits": result["edits"], "delivery": response["delivery"]}


def manifest(root: Path, draft: dict, config: dict) -> list[dict]:
    return [{"path": path, "bytes": len(text.encode()),
             "file_complete": path not in draft["open_files"], "tail": text[-1000:]}
            for path, text in virtual_files(root, draft["edits"], config).items()]


def inspect_draft(root: Path, draft: dict, request, config: dict, reader) -> tuple[str, str]:
    path = request if isinstance(request, str) else request.get("path", "")
    if not isinstance(path, str) or not readable(path, config):
        return reader(root, request, config, max_bytes=10000)
    texts = virtual_files(root, draft["edits"], config)
    if path not in texts:
        return reader(root, request, config, max_bytes=10000)
    with tempfile.TemporaryDirectory(prefix="feature-draft-read-") as tmp:
        file = Path(tmp) / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(texts[path])
        return reader(Path(tmp), request, config, max_bytes=10000)
