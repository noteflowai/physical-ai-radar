"""Verified release announcements on owned channels, with a durable outbox."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import format_datetime
import fcntl
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

try:
    from .agent_pipeline import command, finish, gh, reconcile_superseded, wait_pr, write_json
    from .feature_release import REPOS, fetch
except ImportError:
    from agent_pipeline import command, finish, gh, reconcile_superseded, wait_pr, write_json
    from feature_release import REPOS, fetch

ORIGIN = "https://noteflowai.github.io/physical-ai-radar"
DATA = "data/feature-releases.json"


def identity(repo: str, number: str) -> str:
    name = repo.removeprefix("noteflowai/")
    if name not in REPOS or repo != "noteflowai/" + name or not re.fullmatch(r"\d+\.\d+\.\d+", number):
        raise ValueError("Unexpected announcement identity")
    return name + "/" + number


def validate(event: dict) -> None:
    if event["id"] != identity(event["repo"], event["version"]):
        raise ValueError("Announcement identity changed")
    if not re.fullmatch(r"[0-9a-f]{40}", event["commit"]):
        raise ValueError("Announcement needs an exact verified source commit")
    tag = event["version"] if event["repo"].endswith("/ai-chat-for-amazon-bedrock") else "v" + event["version"]
    if event["release_url"] != f"https://github.com/{event['repo']}/releases/tag/{tag}":
        raise ValueError("Announcement needs the verified release URL")
    if event["source_url"] != f"https://github.com/{event['repo']}/pull/{event['pr']}":
        raise ValueError("Announcement needs its source pull request")
    for field in ["title", "summary", "usage", "limitations", "upgrade"]:
        if (not isinstance(event.get(field), str) or not 1 <= len(event[field]) <= 8000
                or any(ord(c) < 32 and c not in "\n\t\r" for c in event[field])):
            raise ValueError("Invalid public announcement text")
    if datetime.fromisoformat(event["released_at"]).tzinfo is None:
        raise ValueError("Announcement timestamp needs a timezone")


def queue(source: dict, state: Path) -> Path:
    release = source.get("distribution", {})
    if source.get("status") != "published" or release.get("status") != "verified":
        raise ValueError("Cannot announce a release before public package verification")
    if release.get("commit") != source["merge_commit"]:
        raise ValueError("Release and announcement refer to different source")
    plan, delivery = source["feature"], source.get("delivery", {})
    event = {
        "id": identity(source["repo"], release["version"]), "repo": source["repo"],
        "version": release["version"], "commit": source["merge_commit"],
        "feature_id": source["feature_id"], "pr": source["pr"],
        "title": plan["title"].removeprefix("feat:").strip(),
        "summary": delivery.get("summary") or plan["behavior"],
        "usage": delivery.get("usage") or plan.get("usage") or "Open the source pull request for usage documentation.",
        "limitations": delivery.get("limitations") or plan.get("limitations") or "See the source pull request for the checked feature scope.",
        "upgrade": delivery.get("upgrade") or plan.get("upgrade") or "Use this version's release assets; retain the previous version for rollback.",
        "release_url": release["url"], "source_url": source["url"],
        "released_at": source.get("release_verified_at") or datetime.now(timezone.utc).isoformat(),
    }
    validate(event)
    name = hashlib.sha256(event["id"].encode()).hexdigest()
    target = state / "outbox" / (name + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        old = json.loads(target.read_text())
        if old["event"]["commit"] != event["commit"]:
            raise ValueError("A version already has an announcement for another commit")
        return target
    write_json(target, {"status": "pending", "event": event, "attempts": 0})
    return target


def enqueue(source: dict, state: Path) -> None:
    """Promotion contract errors cannot wedge a completed product release."""
    key = hashlib.sha256(source["feature_id"].encode()).hexdigest()
    error_file = state / "announcement-errors" / (key + ".json")
    try:
        queue(source, state)
    except (ValueError, KeyError, TypeError) as error:
        write_json(error_file, {"status": "needs-repair", "source": source, "error": str(error)})
    else:
        error_file.unlink(missing_ok=True)


def read_events(root: Path) -> list[dict]:
    path = root / DATA
    records = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(records, list):
        raise ValueError("Release ledger must be a list")
    seen = set()
    for record in records:
        validate(record)
        if record["id"] in seen:
            raise ValueError("Duplicate version in public release ledger")
        seen.add(record["id"])
    return sorted(records, key=lambda e: (e["released_at"], e["id"]), reverse=True)


STYLE = """body{font:17px/1.65 system-ui,sans-serif;margin:0;background:#080d18;color:#e8edf6}
main,nav{max-width:960px;margin:auto;padding:24px}nav{display:flex;gap:24px;flex-wrap:wrap}
a{color:#9ecbff}h1{font-size:clamp(2rem,5vw,3.2rem);line-height:1.15}h2{line-height:1.3}
article{padding:24px 0;border-top:1px solid #314058}p{max-width:80ch}.meta{color:#a8b7cd}
pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:20px;background:#111d31;border-radius:8px}
code{overflow-wrap:anywhere}.links{display:flex;gap:22px;flex-wrap:wrap}footer{margin:40px 0}"""


def page(title: str, description: str, url: str, body: str) -> str:
    esc = html.escape
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · NoteFlowAI</title>"
        f'<meta name="description" content="{esc(description, quote=True)}">'
        f'<link rel="canonical" href="{esc(url, quote=True)}">'
        '<meta property="og:type" content="article">'
        f'<meta property="og:title" content="{esc(title, quote=True)}">'
        f'<meta property="og:description" content="{esc(description, quote=True)}">'
        f'<meta property="og:url" content="{esc(url, quote=True)}">'
        f'<meta property="og:image" content="{ORIGIN}/assets/og.en.png">'
        f'<link rel="alternate" type="application/rss+xml" title="Verified project releases" href="{ORIGIN}/updates/feed.xml">'
        f"<style>{STYLE}</style></head><body><nav aria-label=\"Main navigation\">"
        f'<a href="{ORIGIN}/en/">Physical AI Radar</a><a href="{ORIGIN}/updates/">Project updates</a>'
        f'<a href="{ORIGIN}/updates/feed.xml">Subscribe via RSS</a></nav><main>{body}'
        '<footer>Release notes from NoteFlowAI’s open-source projects.</footer></main></body></html>\n')


def render(root: Path) -> list[str]:
    records = read_events(root)
    output = root / "updates"
    output.mkdir(exist_ok=True)
    written = []
    cards = []
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    for tag, value in [("title", "NoteFlowAI project releases"), ("link", ORIGIN + "/updates/"),
                       ("description", "New features with verified releases and usage documentation.")]:
        ET.SubElement(channel, tag).text = value
    esc = html.escape
    for event in records:
        url = ORIGIN + "/updates/" + event["id"] + "/"
        meta = f"{event['repo']} · {event['version']} · {event['released_at'][:10]}"
        body = (
            f'<p class="meta">{esc(meta)}</p><h1>{esc(event["title"])}</h1>'
            f'<p>{esc(event["summary"])}</p><h2>Use it</h2><pre>{esc(event["usage"])}</pre>'
            f'<h2>Scope</h2><p>{esc(event["limitations"])}</p><h2>Upgrade and rollback</h2>'
            f'<p>{esc(event["upgrade"])}</p><p class="links">'
            f'<a href="{esc(event["release_url"], quote=True)}">Release and downloads</a>'
            f'<a href="{esc(event["source_url"], quote=True)}">Implementation and checks</a></p>'
            f'<p class="meta">Source commit: <code>{event["commit"]}</code></p>')
        target = output / event["id"] / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page(event["title"], event["summary"][:250], url, body))
        written.append(target.relative_to(root).as_posix())
        cards.append(f'<article><p class="meta">{esc(meta)}</p><h2><a href="{url}">'
                     f'{esc(event["title"])}</a></h2><p>{esc(event["summary"])}</p></article>')
        item = ET.SubElement(channel, "item")
        for tag, text in [("title", event["title"]), ("link", url), ("guid", url),
                          ("description", event["summary"]),
                          ("pubDate", format_datetime(datetime.fromisoformat(event["released_at"])))]:
            ET.SubElement(item, tag).text = text
    intro = ('<h1>Project updates</h1><p>Small, complete improvements to Physical AI research, '
             'skills, evaluation, replay and knowledge workflows. Each entry links to a released '
             'version and its implementation.</p>')
    (output / "index.html").write_text(page(
        "Project updates", "Verified releases from NoteFlowAI's open-source projects.",
        ORIGIN + "/updates/", intro + "".join(cards or ["<p>New releases will appear here.</p>"])))
    ET.indent(rss)
    (output / "feed.xml").write_bytes(ET.tostring(rss, encoding="utf-8", xml_declaration=True))
    (output / "releases.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    return written + ["updates/index.html", "updates/feed.xml", "updates/releases.json"]


def publish(state: Path, workspace: Path) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    with (state / "announcements.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return publish_locked(state, workspace)


def publish_locked(state: Path, workspace: Path) -> dict:
    for file in (state / "announcement-errors").glob("*.json"):
        enqueue(json.loads(file.read_text())["source"], state)
    pending = [(p, json.loads(p.read_text())) for p in sorted((state / "outbox").glob("*.json"))]
    transaction = state / "announcement-transaction.json"
    saved = json.loads(transaction.read_text()) if transaction.exists() else None
    announced = {d["event"]["id"] for _, d in pending if d["status"] == "announced"}
    if saved and set(saved["events"]).issubset(announced):
        transaction.unlink()
        saved = None
    repairs = [d["event"]["id"] for _, d in pending if d["status"] == "needs-repair"]
    pending = [(p, d) for p, d in pending if d["status"] == "pending"]
    if not pending:
        return {"status": "needs-repair" if repairs or any(
            (state / "announcement-errors").glob("*.json")) else "idle", "repairs": repairs}
    repo = "noteflowai/physical-ai-radar"
    if not workspace.exists():
        command(["git", "clone", "--quiet", "https://github.com/" + repo + ".git", str(workspace)], timeout=180)
        (workspace / ".git/feature-updates-owned").write_text("managed announcement clone\n")
    if workspace.is_symlink() or not (workspace / ".git/feature-updates-owned").is_file():
        raise ValueError("Refusing an unmarked announcement checkout")
    proof = {"status": "already-present"}
    if not saved:
        command(["git", "fetch", "origin", "main"], cwd=workspace)
        untracked = command(["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace)
        if any(not (p.startswith("updates/") or p == DATA) for p in untracked.splitlines()):
            raise ValueError("Unexpected files in the managed announcement checkout")
        command(["git", "reset", "--hard", "origin/main"], cwd=workspace)
        command(["git", "clean", "-fd", "--", "updates/"], cwd=workspace)
        records = {e["id"]: e for e in read_events(workspace)}
        for _, record in pending:
            event = record["event"]
            validate(event)
            if event["id"] in records and records[event["id"]] != event:
                raise ValueError("A published announcement differs; preserve the existing record")
            records[event["id"]] = event
        write_json(workspace / DATA, sorted(records.values(), key=lambda e:e["id"]))
        changed = render(workspace)
        generation_file = state / "announcement-generation.json"
        generation = json.loads(generation_file.read_text()) if generation_file.exists() else 0
        branch = "automation/updates-" + hashlib.sha256(
            json.dumps([generation, [d["event"] for _, d in pending]], sort_keys=True).encode()).hexdigest()[:16]
        command(["git", "checkout", "-B", branch], cwd=workspace)
        command(["git", "add", "--", DATA, *changed], cwd=workspace)
        dirty = command(["git", "diff", "--cached", "--name-only"], cwd=workspace).strip()
        if dirty:
            command(["git", "commit", "-m", "Publish verified project release updates"], cwd=workspace)
            head = command(["git", "rev-parse", "HEAD"], cwd=workspace).strip()
            # Checkpoint before any remote mutation. A crash can only resume this commit.
            saved = {"branch": branch, "head": head,
                     "events": [d["event"]["id"] for _, d in pending]}
            write_json(transaction, saved)
    if saved:
        if not saved.get("pr"):
            branch, head = saved["branch"], saved["head"]
            if not branch.startswith("automation/updates-") or not re.fullmatch(r"[0-9a-f]{40}", head):
                raise ValueError("Invalid announcement transaction reference")
            existing = json.loads(gh(repo, "pr", "list", "--head", branch, "--state", "all",
                                     "--json", "number,headRefOid,state"))
            if existing:
                if len(existing) != 1 or existing[0]["headRefOid"] != head:
                    raise ValueError("Existing announcement PR has different source")
                saved["pr"] = existing[0]["number"]
            else:
                command(["git", "push", "origin", head + ":refs/heads/" + branch], cwd=workspace)
                body = state / "announcement-pr.md"
                body.write_text("Publish release notes and RSS entries for publicly verified versions.\n\n"
                                + "\n".join("- " + d["event"]["release_url"] for _, d in pending
                                            if d["event"]["id"] in saved["events"])
                                + "\n\nEvery record links its source commit and PR. "
                                "Deterministic rendering escapes HTML/XML content.\n")
                gh(repo, "pr", "create", "--head", branch, "--base", "main",
                   "--title", "Publish verified project release updates", "--body-file", str(body))
                saved["pr"] = json.loads(gh(repo, "pr", "view", branch, "--json", "number"))["number"]
            write_json(transaction, saved)
        try:
            proof = finish(repo, saved["pr"], saved["head"], ["CI", "pages-build-deployment"],
                           [ORIGIN + "/"], state / "announcement-publication.json")
        except RuntimeError as error:
            record = json.loads(gh(repo, "pr", "view", str(saved["pr"]),
                                    "--json", "state,headRefOid"))
            conflict = record["headRefOid"] != saved["head"] or record["state"] == "CLOSED"
            failed_checks = "CI failed or was cancelled" in str(error)
            if not conflict and failed_checks and not saved.get("check_retry"):
                runs = json.loads(gh(repo, "run", "list", "--commit", saved["head"], "--limit", "30",
                                     "--json", "databaseId,status,conclusion"))
                failed = [r for r in runs if r["status"] == "completed"
                          and r["conclusion"] in {"failure", "cancelled", "timed_out"}]
                if failed:
                    saved["check_retry"] = datetime.now(timezone.utc).isoformat()
                    write_json(transaction, saved)
                    for run in failed:
                        gh(repo, "run", "rerun", str(run["databaseId"]), "--failed")
                    raise
            if not conflict and failed_checks and saved.get("check_retry"):
                if (datetime.now(timezone.utc) - datetime.fromisoformat(saved["check_retry"])).total_seconds() < 600:
                    raise
            if conflict or failed_checks:
                # Quarantine only this content batch. New verified releases can proceed.
                if record["state"] == "OPEN" and record["headRefOid"] == saved["head"]:
                    gh(repo, "pr", "close", str(saved["pr"]))
                for file, data in pending:
                    if data["event"]["id"] in saved["events"]:
                        data.update(status="needs-repair", last_error=str(error), pr=saved["pr"])
                        write_json(file, data)
                write_json(state / "announcement-failures" / (saved["head"] + ".json"),
                           {**saved, "error": str(error)})
                generation_file = state / "announcement-generation.json"
                generation = json.loads(generation_file.read_text()) if generation_file.exists() else 0
                write_json(generation_file, generation + 1)
                transaction.unlink()
                return {"status": "needs-repair", "events": saved["events"]}
            raise
        except (ValueError, OSError):
            # A later daily issue may already be deployed when catch-up resumes.
            # Recheck the original PR gates, then verify an actual descendant build.
            record = wait_pr(repo, saved["pr"], saved["head"], seconds=60)
            if record["state"] != "MERGED":
                raise
            proof = reconcile_superseded({
                "repo": repo, "pr": saved["pr"], "head": saved["head"],
                "workflow_names": ["CI", "pages-build-deployment"], "urls": [ORIGIN + "/"]})
            if not proof:
                raise
            write_json(state / "announcement-publication.json", proof)
    remote = json.loads(fetch(ORIGIN + "/updates/releases.json", 8_000_000))
    by_id = {item["id"]: item for item in remote}
    covered = saved["events"] if saved else [d["event"]["id"] for _, d in pending]
    for file, record in pending:
        event = record["event"]
        if event["id"] not in covered:
            continue
        if by_id.get(event["id"]) != event:
            raise RuntimeError("Public announcement data differs from the approved record")
        url = ORIGIN + "/updates/" + event["id"] + "/"
        body = fetch(url, 1_000_000)
        if event["commit"].encode() not in body:
            raise RuntimeError("Public update page does not identify the released commit")
        feed = ET.fromstring(fetch(ORIGIN + "/updates/feed.xml", 8_000_000))
        if url not in [item.findtext("link") for item in feed.findall("./channel/item")]:
            raise RuntimeError("RSS does not carry the released feature")
        record.update(status="announced", channels={"github_release": event["release_url"],
                      "project_update": url, "rss": ORIGIN + "/updates/feed.xml"},
                      published_at=datetime.now(timezone.utc).isoformat(), publication=proof)
        write_json(file, record)
    transaction.unlink(missing_ok=True)
    return {"status": "needs-repair" if repairs or any(
        (state / "announcement-errors").glob("*.json")) else "announced",
        "events": covered, "repairs": repairs}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", type=Path)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/ai-feature-agent")
    parser.add_argument("--workspace", type=Path, default=Path.home() / ".local/share/ai-feature-updates")
    parser.add_argument("--retry-repairs", action="store_true",
                        help="requeue quarantined announcements after repairing their recorded cause")
    args = parser.parse_args()
    if args.render:
        print(json.dumps({"files": render(args.render)}))
    else:
        if args.retry_repairs:
            args.state.mkdir(parents=True, exist_ok=True)
            with (args.state / "announcements.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for file in (args.state / "outbox").glob("*.json"):
                    record = json.loads(file.read_text())
                    if record["status"] == "needs-repair":
                        record["status"] = "pending"
                        write_json(file, record)
        try:
            result = publish(args.state, args.workspace)
            print(json.dumps(result))
            return 1 if result["status"] == "needs-repair" else 0
        except Exception as error:
            for file in (args.state / "outbox").glob("*.json"):
                record = json.loads(file.read_text())
                if record["status"] == "pending":
                    record.update(attempts=record.get("attempts", 0) + 1,
                                  last_error=str(error), last_attempt=datetime.now(timezone.utc).isoformat())
                    write_json(file, record)
            raise


if __name__ == "__main__":
    sys.exit(main())
