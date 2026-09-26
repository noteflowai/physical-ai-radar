#!/usr/bin/env python3
"""Noninteractive model calls and commit-bound CI/publishing gates.

This is a local, outbound-only maintainer tool. It is never a GitHub runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

class PublicationAbandoned(RuntimeError):
    """A closed or externally changed proposal must not be silently adopted."""


REQUIRED_PR_CHECKS = {
    "noteflowai/dsh-skills-anywhere": [
        "Hugging Face showcase", "GitHub Action", "Node 22 on ubuntu-latest",
        "Node 24 on ubuntu-latest", "Node 22 on macos-latest", "Node 24 on macos-latest",
        "Node 22 on windows-latest",
    ],
    "noteflowai/evalarc": [
        "test (3.11)", "test (3.12)", "test (3.13)", "site", "action", "strands-recipe",
        *[f"docker-audit ({task}, {language})" for task in
          ("durable-kv", "support-routing", "robot-evidence-review")
          for language in ("python", "javascript")],
    ],
    "noteflowai/robot-reel": [
        "verify / check", "verify / rerun", "verify / newton",
        "verify / package", "verify / container", "verify / browser",
    ],
    "noteflowai/physical-ai-radar": [
        "test (3.10)", "test (3.11)", "test (3.12)", "scripts", "pages",
    ],
}


def write_json(path: Path, value: dict | list) -> None:
    """A killed job must leave either the old receipt or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def command(args: list[str], cwd: Path | None = None, timeout: int = 120) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
           "PIP_NO_INPUT": "1", "CI": "1"}
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr[-6000:]}")
    return result.stdout


def clean_terminal(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).strip()


def call_agent(args: list[str], output: Path, cwd: Path | None = None) -> None:
    """Pin the engine that supports the installed JSON agent permission policies."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    result = subprocess.run(
        ["kiro-cli", "chat", "--agent-engine", "v1", "--no-interactive", *args],
        cwd=cwd, env=env, text=True, capture_output=True,
        timeout=int(os.environ.get("REPO_AGENT_TIMEOUT", "900")),
        stdin=subprocess.DEVNULL,
    )
    text = clean_terminal(result.stdout)
    diagnostics = clean_terminal(result.stderr)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n" + diagnostics)
    output.with_suffix(output.suffix + ".stdout").write_text(text)
    if result.returncode or re.search(
        r"failed to set model|using [\"']?default|needs upgrading|Method not found",
        diagnostics, re.I,
    ):
        raise RuntimeError(f"Agent/model selection failed: {diagnostics[-3000:]}")


def parse_object(text: str) -> dict:
    text = clean_terminal(text).removeprefix("> ").strip()
    # The v1 terminal renderer removes code fences but keeps their language label.
    if text.startswith("json\n"):
        text = text[5:].strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("The agent must return one JSON object")
    return result


def gh(repo: str, *args: str, timeout: int = 120) -> str:
    return command(["gh", *args, "--repo", repo], timeout=timeout)


def checks_state(checks: list[dict]) -> str:
    """An empty, unknown, cancelled, or wholly skipped set never passes."""
    if not checks:
        return "pending"
    states = []
    for check in checks:
        if check.get("__typename") == "StatusContext":
            states.append({"SUCCESS": "pass", "PENDING": "pending"}.get(
                check.get("state"), "fail"))
        elif check.get("status") != "COMPLETED":
            states.append("pending")
        else:
            states.append({
                "SUCCESS": "pass", "SKIPPED": "skip", "NEUTRAL": "skip",
            }.get(check.get("conclusion"), "fail"))
    if "fail" in states:
        return "fail"
    if "pending" in states or "pass" not in states:
        return "pending"
    return "pass"


def wait_pr(repo: str, pr: int, head: str, seconds: int = 1800) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        record = json.loads(gh(
            repo, "pr", "view", str(pr), "--json",
            "headRefOid,baseRefName,isCrossRepository,state,statusCheckRollup,mergeCommit,url",
        ))
        if record["headRefOid"] != head:
            raise PublicationAbandoned("PR head changed; the checked commit cannot be substituted")
        if record["baseRefName"] != "main":
            raise RuntimeError("Scheduled publishing only merges into main")
        if record.get("isCrossRepository"):
            raise PublicationAbandoned("Scheduled publishing cannot adopt a fork's proposal")
        if record["state"] not in {"OPEN", "MERGED"}:
            raise PublicationAbandoned(f"PR is {record['state']}")
        state = checks_state(record["statusCheckRollup"])
        successful = {check.get("name") or check.get("context")
                      for check in record["statusCheckRollup"]
                      if check.get("conclusion") == "SUCCESS"
                      or (check.get("__typename") == "StatusContext"
                          and check.get("state") == "SUCCESS")}
        required = set(REQUIRED_PR_CHECKS.get(repo, []))
        if state == "pass" and required.issubset(successful):
            return record
        if state == "fail":
            raise RuntimeError("CI failed or was cancelled on " + head)
        time.sleep(20)
    raise TimeoutError("CI has not passed on the expected commit")


def wait_publication(repo: str, commit: str, workflows: list[str],
                     seconds: int = 2400) -> list[dict]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        runs = json.loads(gh(
            repo, "run", "list", "--branch", "main", "--commit", commit,
            "--limit", "60", "--json",
            "databaseId,headSha,status,conclusion,workflowName,url",
        ))
        latest = {}
        for run in sorted(runs, key=lambda x: x["databaseId"], reverse=True):
            if run["headSha"] == commit:
                latest.setdefault(run["workflowName"], run)
        relevant = [latest.get(name) for name in workflows]
        if any(r and r["status"] == "completed" and r["conclusion"] != "success"
               for r in relevant):
            raise RuntimeError("A required main/deployment workflow failed")
        if all(r and r["status"] == "completed" and r["conclusion"] == "success"
               for r in relevant):
            return relevant
        time.sleep(20)
    raise TimeoutError("Publication did not complete on the merged commit")


def public_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "NoteFlowAI-Maintainer/1.0", "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read(8_000_001)
        if len(body) > 8_000_000 or response.status != 200:
            raise ValueError("Unexpected public response")
        content_type = response.headers.get("Content-Type", "")
        if "html" in content_type and b"<title" not in body.lower():
            raise ValueError("The public page has no title")
        return body


def read_public(url: str) -> dict:
    body = public_bytes(url)
    return {"url": url, "status": 200, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest()}


def public_commit(repo: str, commit: str, url: str) -> dict:
    """Bind public content to the checked commit using existing build provenance."""
    root = url.rstrip("/") + "/"
    if ".hf.space" in url or repo == "noteflowai/evalarc":
        name = "space-manifest.json" if repo.endswith("/robot-reel") else "manifest.json"
        manifest = json.loads(public_bytes(root + name))
        source = manifest.get("source", {})
        actual = manifest.get("source_commit", source.get("commit"))
        dirty = manifest.get("source_dirty", source.get("dirty"))
        if actual != commit or dirty is not False:
            raise ValueError("Public build provenance differs from the checked commit")
        body = public_bytes(root + "index.html")
        if ".hf.space" in url:
            # Only the platform's exact non-executable creator metadata may differ.
            body = re.sub(
                rb'(?<=<head>)<script>window\.huggingface=\{variables:'
                rb'\{"SPACE_CREATOR_USER_ID":"[0-9a-f]{24}"\}\};</script>',
                b"", body, count=1,
            )
        expected = manifest["files"]["index.html"]
        if isinstance(expected, dict):
            expected = expected["sha256"]
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("Public homepage differs from its committed build manifest")
        return {"commit": commit, "manifest": root + name, "homepage_sha256": expected}
    if repo == "noteflowai/robot-reel":
        expected = public_bytes(f"https://raw.githubusercontent.com/{repo}/{commit}/docs/index.html")
        actual = public_bytes(root + "index.html")
        if actual != expected:
            raise ValueError("Public Robot Reel homepage differs from the checked commit")
        return {"commit": commit, "path": "docs/index.html",
                "homepage_sha256": hashlib.sha256(actual).hexdigest()}
    if repo == "noteflowai/physical-ai-radar":
        build = json.loads(command(["gh", "api", f"repos/{repo}/pages/builds/latest"]))
        if build.get("commit") != commit or build.get("status") != "built":
            raise ValueError("The Pages build does not identify the checked Radar commit")
        expected = public_bytes(f"https://raw.githubusercontent.com/{repo}/{commit}/radar/latest.json")
        actual = public_bytes(root + "radar/latest.json")
        if actual != expected:
            raise ValueError("Public Radar data differs from the checked commit")
        return {"commit": commit, "pages_build": "built",
                "data_sha256": hashlib.sha256(actual).hexdigest()}
    raise ValueError("No publication provenance contract is configured for this repository")


def verify_deployment(repo: str, commit: str, workflows: list[str], urls: list[str]) -> tuple:
    for attempt in range(2):
        try:
            runs = wait_publication(repo, commit, workflows)
            break
        except RuntimeError:
            if attempt:
                raise
            failed = json.loads(gh(repo, "run", "list", "--commit", commit, "--limit", "30",
                                   "--json", "databaseId,workflowName,conclusion"))
            newest = {}
            for run in failed:
                newest.setdefault(run["workflowName"], run)
            for name in workflows:
                run = newest.get(name)
                if run and run["conclusion"] in {"failure", "timed_out", "cancelled"}:
                    gh(repo, "run", "rerun", str(run["databaseId"]), "--failed")
            time.sleep(20)
    pages = []
    for url in urls:
        deadline = time.monotonic() + 180
        while True:
            try:
                page = read_public(url)
                page["commit_proof"] = public_commit(repo, commit, url)
                pages.append(page)
                break
            except (ValueError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(10)
    return runs, pages


def finish_commit(repo: str, commit: str, workflows: list[str],
                  urls: list[str], output: Path) -> dict:
    """Verify a deterministic daily publisher's main commit, including retries."""
    if not workflows or not urls:
        raise ValueError("Publication requires deployment gates and public URLs")
    pending = {"repo": repo, "commit": commit, "workflow_names": workflows,
               "urls": urls, "status": "pending"}
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, pending)
    try:
        runs, pages = verify_deployment(repo, commit, workflows, urls)
    except Exception as error:
        write_json(output, {**pending, "status": "retry-needed", "error": str(error)})
        raise
    receipt = {**pending, "status": "published", "workflows": runs,
               "public_readback": pages}
    write_json(output, receipt)
    return receipt


def finish(repo: str, pr: int, head: str, workflows: list[str],
           urls: list[str], output: Path) -> dict:
    """A successful receipt means PR checks, merge, main deployment, and readback."""
    if not workflows or not urls:
        raise ValueError("A publication requires named deployment gates and public readback URLs")
    pending = {"repo": repo, "pr": pr, "head": head, "workflow_names": workflows,
               "urls": urls, "status": "pending"}
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, pending)
    try:
        record = wait_pr(repo, pr, head)
        if record["state"] != "MERGED":
            gh(repo, "pr", "merge", str(pr), "--merge", "--match-head-commit", head)
            deadline = time.monotonic() + 120
            while True:
                record = json.loads(gh(repo, "pr", "view", str(pr), "--json", "mergeCommit,url"))
                if record.get("mergeCommit"):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("GitHub has not exposed the completed merge commit")
                time.sleep(5)
        commit = record["mergeCommit"]["oid"]
        runs, pages = verify_deployment(repo, commit, workflows, urls)
    except Exception as error:
        status = "abandoned" if isinstance(error, PublicationAbandoned) else "retry-needed"
        write_json(output, {**pending, "status": status, "error": str(error)})
        raise
    receipt = {"repo": repo, "pr": pr, "head": head, "merge_commit": commit,
               "url": record["url"], "workflows": runs, "public_readback": pages,
               "status": "published"}
    write_json(output, receipt)
    return receipt


def reconcile_superseded(item: dict) -> dict | None:
    """A newer verified main deployment can retire an interrupted older receipt."""
    repo = item["repo"]
    commit = item.get("commit")
    if "pr" in item:
        record = json.loads(gh(repo, "pr", "view", str(item["pr"]),
                               "--json", "state,mergeCommit"))
        if record["state"] != "MERGED" or not record.get("mergeCommit"):
            return None
        commit = record["mergeCommit"]["oid"]
    current = json.loads(command(["gh", "api", f"repos/{repo}/git/ref/heads/main"]))["object"]["sha"]
    if not commit or current == commit:
        return None
    comparison = json.loads(command(["gh", "api", f"repos/{repo}/compare/{commit}...{current}"]))
    if comparison.get("status") != "ahead":
        return None
    runs, pages = verify_deployment(repo, current, item["workflow_names"], item["urls"])
    return {**item, "status": "superseded", "superseding_commit": current,
            "workflows": runs, "public_readback": pages}


def main() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="action", required=True)
    call = subs.add_parser("call")
    call.add_argument("--output", required=True, type=Path)
    call.add_argument("args", nargs=argparse.REMAINDER)
    final = subs.add_parser("finish")
    final.add_argument("--repo", required=True)
    final.add_argument("--pr", required=True, type=int)
    final.add_argument("--head", required=True)
    final.add_argument("--workflow", action="append", default=[])
    final.add_argument("--url", action="append", default=[])
    final.add_argument("--output", required=True, type=Path)
    published = subs.add_parser("verify-commit")
    published.add_argument("--repo", required=True)
    published.add_argument("--commit", required=True)
    published.add_argument("--workflow", action="append", default=[])
    published.add_argument("--url", action="append", default=[])
    published.add_argument("--output", required=True, type=Path)
    resume = subs.add_parser("resume")
    resume.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "call":
        rest = args.args[1:] if args.args[:1] == ["--"] else args.args
        call_agent(rest, args.output)
        print(args.output.read_text()[-4000:])
    elif args.action == "finish":
        print(json.dumps(finish(args.repo, args.pr, args.head, args.workflow,
                                args.url, args.output), indent=2))
    elif args.action == "verify-commit":
        print(json.dumps(finish_commit(args.repo, args.commit, args.workflow,
                                       args.url, args.output), indent=2))
    else:
        failures = []
        for file in sorted(args.directory.glob("*.json")):
            item = json.loads(file.read_text())
            if item.get("status") not in {"pending", "retry-needed"}:
                continue
            try:
                superseded = reconcile_superseded(item)
                if superseded:
                    write_json(file, superseded)
                    continue
                if "pr" in item:
                    finish(item["repo"], item["pr"], item["head"], item["workflow_names"],
                           item["urls"], file)
                else:
                    finish_commit(item["repo"], item["commit"], item["workflow_names"],
                                  item["urls"], file)
            except Exception as error:
                if not isinstance(error, PublicationAbandoned):
                    failures.append({"file": str(file), "error": str(error)})
        if failures:
            raise RuntimeError(json.dumps(failures))


if __name__ == "__main__":
    main()
