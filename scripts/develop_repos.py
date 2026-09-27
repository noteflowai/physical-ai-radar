#!/usr/bin/env python3
"""Develop one complete feature per project; resume the same feature across days."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

try:
    from .agent_pipeline import command, finish, gh, reconcile_superseded, write_json
    from .feature_policy import PROJECTS, SYSTEM, acceptance_command, allowed, matches
    from .feature_runtime import GPU_IMAGE, archive, execute, image_for
    from .feature_wordpress import SLUG, package_files, publish as publish_wordpress, version as wordpress_version
    from .improve_repos import model_json, snapshot, verify_public_browser
    from .job_schedule import run_day
    from .nightly_batch import execute as execute_stage
except ImportError:
    from agent_pipeline import command, finish, gh, reconcile_superseded, write_json
    from feature_policy import PROJECTS, SYSTEM, acceptance_command, allowed, matches
    from feature_runtime import GPU_IMAGE, archive, execute, image_for
    from feature_wordpress import SLUG, package_files, publish as publish_wordpress, version as wordpress_version
    from improve_repos import model_json, snapshot, verify_public_browser
    from job_schedule import run_day
    from nightly_batch import execute as execute_stage


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def calendar_day() -> str:
    """Release allowance follows wall-clock Singapore dates, not catch-up dates."""
    return datetime.now(ZoneInfo("Asia/Singapore")).date().isoformat()


def record_allowance(result: dict, service_state: Path, name: str) -> None:
    if result.get("status") == "published":
        released = result.get("released_on", result["completed_on"])
        write_json(service_state / "allowances" / released / (name + ".json"), result)


def ask(prompt: str, state: Path, label: str, *, review: bool = False) -> dict:
    return model_json(prompt, state, label,
                      "claude-opus-5" if review else "claude-sonnet-5", system=SYSTEM)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def text_file(root: Path, path: str, limit: int = 24000) -> str:
    file = root / path
    if not file.resolve().is_relative_to(root.resolve()) or file.is_symlink():
        raise ValueError("Repository context cannot follow paths outside its snapshot")
    if not file.is_file():
        return "<file does not exist>"
    raw = file.read_bytes()
    if b"\0" in raw:
        return "<binary file>"
    if len(raw) > limit:
        lines = raw.decode("utf-8").splitlines()
        index = [f"{i}: {line[:180]}" for i, line in enumerate(lines, 1)
                 if re.search(r"\b(?:def|class|function)\s+\w+|^export ", line)]
        return (f"<{len(lines)} lines; request need_files objects with path/start_line/end_line "
                f"or path/search to read relevant sections>\n" + "\n".join(index[:180]))
    return raw.decode("utf-8")


def requested_context(root: Path, request, config: dict, max_bytes: int = 24000) -> tuple[str, str]:
    if isinstance(request, str):
        if not allowed(request, config):
            raise ValueError("Context request is outside the product scope")
        return request, text_file(root, request)
    path = request.get("path", "")
    if not allowed(path, config):
        raise ValueError("Context request is outside the product scope")
    file = root / path
    if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
        raise ValueError("Context cannot follow symlinks")
    if not file.is_file():
        return path, "<file does not exist>"
    lines = file.read_text().splitlines()
    if "search" in request:
        query = request["search"]
        if not isinstance(query, str) or not 2 <= len(query) <= 150:
            raise ValueError("Invalid source search")
        indexes = [i for i, line in enumerate(lines) if query in line]
        if not indexes:
            return path + " [search " + query + "]", "<no matching lines>"
        start = max(1, indexes[0] - 30)
        end = min(len(lines), indexes[0] + 150)
    else:
        start, end = request.get("start_line"), request.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end:
            raise ValueError("Invalid source line range")
        end = min(end, len(lines))
        end = min(end, start + 239)
    text = "\n".join(lines[start - 1:end])
    while len(text.encode()) > max_bytes and end > start:
        end -= 1
        text = "\n".join(lines[start - 1:end])
    if len(text.encode()) > max_bytes:
        text = "<single source line exceeds the excerpt budget>"
    return f"{path} [lines {start}-{end}]", text


def checkout(workspace: Path, name: str) -> Path:
    root = workspace / name
    if not root.exists():
        command(["git", "clone", "--quiet",
                 f"https://github.com/noteflowai/{name}.git", str(root)], timeout=600)
        (root / ".git/feature-agent-owned").write_text("dedicated feature clone\n")
    if root.is_symlink() or not (root / ".git/feature-agent-owned").is_file():
        raise ValueError("Refusing to modify a checkout not owned by the feature service")
    command(["git", "fetch", "origin", "main"], cwd=root)
    return root


def reset(root: Path, ref: str) -> None:
    command(["git", "reset", "--hard", ref], cwd=root)
    command(["git", "clean", "-fd"], cwd=root)


def context(root: Path, name: str, inputs: dict, config: dict) -> dict:
    files = command(["git", "ls-files"], cwd=root).splitlines()
    tree = []
    for group, limit in [("code", 200), ("tests", 100), ("docs", 60)]:
        tree.extend(p for p in [p for p in files if allowed(p, config)
                                and matches(p, config[group])][:limit] if p not in tree)
    readmes = {p: text_file(root, p, 20000) for p in
               ["README.md", "readme.txt", "pyproject.toml", "package.json", "composer.json"]
               if (root / p).exists()}
    issues = json.loads(gh("noteflowai/" + name, "issue", "list", "--state", "open",
                           "--limit", "6", "--json", "number,title,body,url"))
    for issue in issues:
        issue["body"] = issue["body"][:1200]
    research = [{k: (str(item[k])[:400] if k == "summary" else item[k])
                 for k in ["id", "title", "url", "summary", "date"] if k in item}
                for item in inputs.get("sources", [])[:20]]
    return {"repo": name, "mission": config["mission"], "avoid": config["avoid"],
            "tree": tree, "readmes": readmes, "issues": issues,
            "recent_commits": command(["git", "log", "-8", "--format=%h %s"], cwd=root),
            "research": research,
            "allowed_code": config["code"], "allowed_docs": config["docs"],
            "test_runners": config["test_runners"],
            "validation_contract": {
                "new_acceptance": "The controller explicitly executes test_path using test_runner before and after the feature, even when package scripts enumerate older suites.",
                "regressions": config["checks"],
                "wordpress": "The existing bin/prerelease.sh discovers every top-level tests/*.php suite; composer.json is not the release gate.",
            },
            "gpu": "L40S, serial offline experiments; Python 3.12, PyTorch 2.9.1 CUDA 12.8. GPU checks must use existing dependencies and small synthetic/local fixtures.",
            "wordpress_release": "A new WordPress feature must increment the minor version consistently in the plugin header, constant and stable tag, with changelog and upgrade notice. Preserve PHP 7.4 and WordPress compatibility."}


def bounded_files(root: Path, paths: list[str], budget: int) -> dict:
    result = {}
    for path in paths:
        value = text_file(root, path)
        if len(value.encode()) > budget:
            value = text_file(root, path, 1000)[:1200]
        result[path] = value
        budget -= len(value.encode())
    return result


def validate_plan(plan: dict, config: dict) -> None:
    for field in ["title", "problem", "behavior", "why_this_repo"]:
        if not isinstance(plan.get(field), str) or not 5 <= len(plan[field]) <= 4000:
            raise ValueError(f"Feature needs a concrete {field}")
    if not 1 <= len(plan.get("acceptance", [])) <= 8:
        raise ValueError("One feature needs one to eight acceptance conditions")
    paths = plan.get("read_paths", [])
    if not isinstance(paths, list) or not 1 <= len(paths) <= 14:
        raise ValueError("Read a bounded implementation and test surface")
    if not all(allowed(p, config) for p in paths):
        raise ValueError("Plan requested a file outside the product scope")
    acceptance_command(plan, config)
    gpu = plan.get("gpu_test_path")
    if gpu is not None and (not allowed(gpu, config) or not matches(gpu, config["tests"])
                            or not gpu.endswith(".py")):
        raise ValueError("GPU experiment must be a reviewed Python test in this repository")
    if gpu and not plan.get("gpu_reason"):
        raise ValueError("A GPU experiment needs a reason tied to this feature")


def apply(root: Path, proposal: dict, config: dict, plan: dict) -> list[str]:
    """Exact edits and new files, validated in memory before changing any file."""
    edits = proposal.get("edits", [])
    if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError("Provide one complete feature in at most 32 exact edits")
    before, after = {}, {}
    for edit in edits:
        path, old, new = edit["path"], edit["old"], edit["new"]
        if not allowed(path, config) or not isinstance(new, str) or "\0" in new:
            raise ValueError("Invalid product edit")
        file = root / path
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Feature edits cannot follow symlinks")
        if matches(path, config["tests"]) and file.exists():
            raise ValueError("Retain the existing regression suite; put new acceptance tests in a new file")
        if path not in before:
            before[path] = file.read_text() if file.exists() else None
        current = after.get(path, before[path])
        if old is None:
            if current is not None or not new:
                raise ValueError("New-file edits must target an absent file")
            after[path] = new
        elif not isinstance(old, str) or not old or current is None or current.count(old) != 1:
            raise ValueError(f"Edit must match exactly one existing span: {path}")
        else:
            after[path] = current.replace(old, new, 1)
    if len(after) > 16 or sum(len(v.encode()) for v in after.values()) > 240000:
        raise ValueError("Feature is too large for one reviewable daily increment")
    if plan["test_path"] not in after:
        raise ValueError("The acceptance test must be part of the feature")
    if not any(matches(p, config["code"]) for p in after):
        raise ValueError("The daily feature needs an actual product implementation")
    if not any(matches(p, config["docs"]) for p in after):
        raise ValueError("The feature needs usage documentation")
    if plan.get("gpu_test_path") and plan["gpu_test_path"] not in after:
        raise ValueError("Include the GPU experiment in the reviewed feature")
    for path, text in after.items():
        if not text.strip():
            raise ValueError("Daily features cannot empty existing files")
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    return sorted(after)


def require_review(review: dict) -> None:
    if review.get("approved") is not True or review.get("findings") != []:
        raise ValueError("Independent review requires correction: " + json.dumps(review))


@contextmanager
def gpu_slot(state: Path, wait: int = 120):
    """One automation experiment at a time, with a bounded wait for external work."""
    with (state / "gpu.lock").open("a") as lock:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                used = command(["nvidia-smi", "--query-gpu=memory.used",
                                "--format=csv,noheader,nounits", "--id=0"]).strip()
                if int(used) > 4096:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    raise BlockingIOError("GPU is in use by another workload")
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("GPU busy; retain this feature for the next scheduled attempt")
                time.sleep(5)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def inspect_checks(records: list[dict]) -> None:
    for record in records:
        expected = [{"argv": argv, "exit_code": 0, "executed": True}
                    for argv in record["commands"]]
        if record["exit_code"] != 0 or record.get("executed_commands") != expected:
            raise RuntimeError(Path(record["log"]).read_text()[-10000:])


def validate_feature(root: Path, task: dict, config: dict, state: Path,
                     service_state: Path) -> list[dict]:
    plan = task["plan"]
    if task.get("repo") == "noteflowai/ai-chat-for-amazon-bedrock":
        names = ["readme.txt", SLUG + ".php"]
        old = wordpress_version({p: command(
            ["git", "show", task["base"] + ":" + p], cwd=root).encode() for p in names})
        actual = wordpress_version({p: (root / p).read_bytes() for p in names})
        major, minor, _ = map(int, old.split("."))
        expected = f"{major}.{minor + 1}.0"
        if actual != expected:
            raise ValueError(f"The complete WordPress feature must advance {old} to {expected} "
                             "in the plugin header, constant, stable tag, changelog and upgrade notice")
    image = image_for(root, task["base"], config, state)

    def checked(snapshot_path, selected_image, commands, label, **options):
        receipt = state / label / "receipt.json"
        prior = read_json(receipt, {})
        reusable = prior.get("exit_code") == 0
        if label == "acceptance-before":
            reusable = prior.get("exit_code", 0) not in {0, 124, 125, 126, 127}
        if (reusable and prior.get("candidate_commit") == task["head"]
                and prior.get("base_commit") == task["base"]
                and prior.get("commands") == commands and prior.get("image") == selected_image
                and Path(prior.get("log", "")).is_file()
                and hashlib.sha256(Path(prior["log"]).read_bytes()).hexdigest() == prior.get("log_sha256")):
            return prior
        with (gpu_slot(service_state) if options.get("gpu") else nullcontext()):
            record = execute(snapshot_path, selected_image, commands, state, label, **options)
        record.update(candidate_commit=task["head"], base_commit=task["base"])
        # Save each completed substage before starting another costly operation.
        write_json(receipt, record)
        return record

    with tempfile.TemporaryDirectory(prefix="feature-validation-") as tmp:
        scratch = Path(tmp)
        scratch.chmod(0o755)
        red, green = scratch / "red", scratch / "green"
        archive(root, task["base"], red)
        archive(root, task["head"], green)
        # Acceptance tests must fail against the published product before the feature.
        for path in task["changed"]:
            if matches(path, config["tests"]):
                (red / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(green / path, red / path)
        test = acceptance_command(plan, config)
        setup = config.get("acceptance_setup", [])
        red_commands = [*setup, test]
        failing = checked(red, image, red_commands, "acceptance-before")
        executed = failing.get("executed_commands", [])
        if (failing["exit_code"] in {0, 124, 125, 126, 127}
                or len(executed) != len(red_commands) or executed[-1].get("argv") != test
                or executed[-1].get("executed") is not True
                or executed[-1].get("exit_code", 0) in {0, 124, 125, 126, 127}
                or executed[:-1] != [{"argv": argv, "exit_code": 0, "executed": True} for argv in setup]):
            raise ValueError("Acceptance must fail on the old behavior, without a container/timeout failure")
        passing = checked(green, image, [*setup, test, *config["checks"]], "checks",
                          generated=config.get("generated", []), timeout=1800)
        inspect_checks([passing])
        records = [failing, passing]
        for path in config.get("generated", []):
            generated = state / "checks/artifacts/generated" / path
            if generated.exists():
                boundary = (state / "checks/artifacts").resolve()
                if generated.is_symlink() or not generated.resolve().is_relative_to(boundary):
                    raise ValueError("Generated output cannot follow a symlink")
                if generated.stat().st_size > 2_000_000:
                    raise ValueError("Generated output exceeds its publication budget")
                (root / path).write_bytes(generated.read_bytes())
        if plan.get("gpu_test_path"):
            gpu = checked(green, GPU_IMAGE, [["python", plan["gpu_test_path"]]],
                          "gpu", gpu=True, timeout=900)
            inspect_checks([gpu])
            records.append(gpu)
        return records


def push(root: Path, task: dict, state: Path) -> dict:
    repo, branch, head = task["repo"], task["branch"], task["head"]
    if not branch.startswith("automation/feature-"):
        raise ValueError("Refusing an unrelated branch")
    command(["git", "push", "-u", "origin", branch], cwd=root, timeout=180)
    prs = json.loads(gh(repo, "pr", "list", "--head", branch, "--state", "all",
                        "--json", "number,headRefOid,state"))
    if prs and (prs[0]["headRefOid"] != head or prs[0]["state"] == "CLOSED"):
        raise ValueError("A remote proposal changed externally; retain it for reconciliation")
    if prs:
        task["pr"] = prs[0]["number"]
    else:
        body = state / "pr.md"
        plan = task["plan"]
        body.write_text(
            plan["problem"] + "\n\n" + plan["behavior"] +
            "\n\nAcceptance:\n" + "\n".join("- " + c for c in plan["acceptance"]) +
            "\n\nOne specialist feature. Behavioral acceptance fails on the base and passes "
            "on the implementation; project checks and independent model review passed. "
            "GPU results are included only when actually executed. "
            "Automatically developed and reviewed; no human review is claimed.\n")
        gh(repo, "pr", "create", "--base", "main", "--head", branch,
           "--title", task["plan"]["title"][:120], "--body-file", str(body))
        task["pr"] = json.loads(gh(repo, "pr", "view", branch, "--json", "number"))["number"]
    task["phase"] = "ci"
    return task


def publish(root: Path, task: dict, config: dict, state: Path) -> dict:
    pr = json.loads(gh(task["repo"], "pr", "view", str(task["pr"]),
                       "--json", "state,headRefOid,mergeCommit,url"))
    if pr["headRefOid"] != task["head"] or task.get("reviewed_head") != task["head"]:
        raise ValueError("Publication requires the exact independently reviewed head")
    if pr["state"] == "OPEN":
        current = json.loads(command(["gh", "api", f"repos/{task['repo']}/commits/main"]))["sha"]
        if current != task["base"]:
            raise RuntimeError("Main advanced; reimplement the saved feature on the current base and revalidate")
    result = None
    if pr["state"] == "MERGED" and config["urls"]:
        newer = reconcile_superseded({
            "repo": task["repo"], "pr": task["pr"], "head": task["head"],
            "workflow_names": config["workflows"], "urls": config["urls"],
        })
        if newer:
            result = {**newer, "status": "published", "url": pr["url"],
                      "merge_commit": pr["mergeCommit"]["oid"]}
    if result is None:
        result = finish(task["repo"], task["pr"], task["head"], config["workflows"],
                        config["urls"], state / "publication.json")
    if config["urls"] and task["repo"] != "noteflowai/physical-ai-radar":
        # Existing controller runs public browser interactions; it installs only
        # Playwright dependencies from the reviewed/merged product.
        result["browser_readback"] = verify_public_browser(root, config, state)
    result["feature"] = task["plan"]
    result["feature_id"] = task["id"]
    result["checks"] = task["checks"]
    if task["repo"].endswith("/ai-chat-for-amazon-bedrock"):
        runs = result["workflows"]
        run = next(r for r in runs if r["workflowName"] == "Quality gate")
        artifacts = state / "plugin-package"
        artifacts.mkdir(exist_ok=True)
        proof_file = artifacts / "source.json"
        proof = read_json(proof_file, {})
        packages = list(artifacts.glob("*.zip"))
        cached = (len(packages) == 1 and proof.get("commit") == result["merge_commit"]
                  and proof.get("sha256") == hashlib.sha256(packages[0].read_bytes()).hexdigest())
        if not cached:
            for package in packages:
                package.unlink()
            try:
                gh(task["repo"], "run", "download", str(run["databaseId"]),
                   "--name", "plugin-package", "--dir", str(artifacts), timeout=180)
            except Exception:
                refresh = state / f"artifact-refresh-{run['databaseId']}.json"
                if not refresh.exists():
                    gh(task["repo"], "run", "rerun", str(run["databaseId"]))
                    write_json(refresh, {"commit": result["merge_commit"], "requested_at": stamp()})
                raise RuntimeError("The exact main CI package needs regeneration; resume after its workflow completes")
        packages = list(artifacts.glob("*.zip"))
        if len(packages) != 1:
            raise RuntimeError("Expected exactly one CI-verified WordPress installable package")
        package_files(packages[0])
        write_json(proof_file, {"commit": result["merge_commit"], "run": run["databaseId"],
                               "sha256": hashlib.sha256(packages[0].read_bytes()).hexdigest()})
        result["package"] = {"path": str(packages[0]),
                             "sha256": hashlib.sha256(packages[0].read_bytes()).hexdigest()}
        result["wordpress_org"] = publish_wordpress(packages[0], state)
        result["status"] = "published"
    write_json(state / "publication.json", result)
    return result


def complete(result: dict, state: Path, active: Path, service_state: Path,
             name: str, day: str) -> dict:
    """Durably consume today's allowance before removing the active transaction."""
    result["completed_on"] = day
    result["released_on"] = calendar_day()
    write_json(state / "complete.json", result)
    record_allowance(result, service_state, name)
    write_json(service_state / day / name / "result.json", result)
    active.unlink(missing_ok=True)
    return result


def checkpoint(root: Path, task: dict, state: Path, active: Path) -> None:
    """Keep unpublished commit objects recoverable outside the disposable clone."""
    target = state / (task["head"] + ".bundle")
    temporary = target.with_suffix(".bundle.tmp")
    command(["git", "bundle", "create", str(temporary), "HEAD", "^" + task["base"]], cwd=root)
    temporary.replace(target)
    task["bundle"] = target.name
    write_json(active, task)


def restore(root: Path, task: dict, state: Path) -> None:
    head, branch = task["head"], task["branch"]
    if not branch.startswith("automation/feature-") or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("Invalid saved feature reference")
    exists = subprocess.run(["git", "cat-file", "-e", head + "^{commit}"], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        bundle = state / task.get("bundle", "candidate.bundle")
        if bundle.is_file():
            command(["git", "fetch", str(bundle), "HEAD"], cwd=root)
        else:
            command(["git", "fetch", "origin", branch], cwd=root)
            fetched = command(["git", "rev-parse", "FETCH_HEAD"], cwd=root).strip()
            if fetched != head:
                raise ValueError("Remote feature differs from the saved independently reviewed commit")
    command(["git", "checkout", "-B", branch, head], cwd=root)
    if command(["git", "rev-parse", "HEAD"], cwd=root).strip() != head:
        raise ValueError("Unable to restore the exact feature commit")


def advance(root: Path, task: dict, config: dict, state: Path, active: Path,
            service_state: Path, name: str, day: str) -> dict:
    """Each costly phase has a durable starting checkpoint before it can time out."""
    attempt_dir = state / f"attempt-{task['attempts']}"
    attempt_dir.mkdir(exist_ok=True)
    phase = task["phase"]
    task.setdefault("phase_attempts", {})[phase] = task.get("phase_attempts", {}).get(phase, 0) + 1
    write_json(active, task)
    if task["phase"] == "validate":
        for revision in range(2):
            task["checks"] = validate_feature(root, task, config, attempt_dir, service_state)
            dirty = command(["git", "diff", "--name-only"], cwd=root).splitlines()
            if not dirty:
                break
            generated = config.get("generated", [])
            if not set(dirty).issubset(generated):
                raise ValueError("Validation changed unapproved source files")
            command(["git", "add", "--", *generated], cwd=root)
            command(["git", "commit", "--amend", "--no-edit"], cwd=root)
            task["head"] = command(["git", "rev-parse", "HEAD"], cwd=root).strip()
            checkpoint(root, task, state, active)
        else:
            raise ValueError("Generated output did not stabilize on the validated commit")
        if any(r["candidate_commit"] != task["head"] for r in task["checks"]):
            raise ValueError("Validation evidence is not bound to the final candidate commit")
        task["phase"] = "acceptance-review"
        write_json(active, task)
    if task["phase"] == "acceptance-review":
        diff = command(["git", "diff", task["base"], task["head"], "--"], cwd=root)
        review = ask(
            'Final independent acceptance review. Verify the final diff implements the one '
            'feature, tests exercise the promised behavior, and measured claims match actual logs. '
            'The red test must fail for missing behavior, not a harness/runner error or a broken test. '
            'Return {"approved":true|false,"findings":[]}.\n' + json.dumps(
                {"plan": task["plan"], "diff": diff, "checks": task["checks"],
                 "logs": [Path(r["log"]).read_text()[-6000:] for r in task["checks"]]}),
            attempt_dir, "acceptance-review", review=True)
        require_review(review)
        task["reviewed_head"] = task["head"]
        task["phase"] = "push"
        write_json(active, task)
    if task["phase"] == "push":
        task.update(push(root, task, state))
        write_json(active, task)
    if task["phase"] != "ci":
        raise ValueError("Unknown feature advancement phase")
    result = publish(root, task, config, state)
    return complete(result, state, active, service_state, name, day)


def retire(task: dict, state: Path, active: Path, service_state: Path,
           name: str, day: str, reason: str) -> dict:
    if task.get("pr"):
        record = json.loads(gh(task["repo"], "pr", "view", str(task["pr"]),
                               "--json", "state,headRefOid"))
        if record["state"] == "OPEN" and record["headRefOid"] == task.get("head"):
            gh(task["repo"], "pr", "close", str(task["pr"]), "--delete-branch")
    result = {"repo": task["repo"], "feature_id": task["id"], "status": "deferred",
              "reason": reason, "completed_on": day,
              "publication_state": read_json(state / "publication.json", {})}
    write_json(state / "retired-task.json", task)
    write_json(state / "terminal.json", result)
    write_json(service_state / day / name / "result.json", result)
    active.unlink(missing_ok=True)
    return result


def develop(name: str, inputs: dict, workspace: Path, service_state: Path, day: str,
            *, plan_only: bool = False) -> dict:
    config, repo = {**PROJECTS[name]}, "noteflowai/" + name
    task_dir = service_state / "tasks" / name
    task_dir.mkdir(parents=True, exist_ok=True)
    active = task_dir / "active.json"
    task = read_json(active)
    if task:
        previous_state = task_dir / task["id"]
        terminal = read_json(previous_state / "complete.json") or read_json(previous_state / "terminal.json")
        if terminal:
            # Recover a crash between terminal recording, day allowance and active cleanup.
            record_allowance(terminal, service_state, name)
            write_json(service_state / terminal["completed_on"] / name / "result.json", terminal)
            active.unlink()
            if terminal["completed_on"] == day:
                return terminal
            task = None
    allowance = read_json(service_state / "allowances" / calendar_day() / (name + ".json"))
    if allowance:
        return {**allowance, "daily_limit_reused": True}
    root = checkout(workspace, name)
    main = command(["git", "rev-parse", "origin/main"], cwd=root).strip()
    if name == "physical-ai-radar":
        generation_ref = (task["base"] if task and task["phase"] not in {"plan", "implement"} else main)
        issue = json.loads(command(["git", "show", generation_ref + ":radar/latest.json"], cwd=root))["date"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", issue):
            raise ValueError("Invalid published Radar issue date")
        assets = command(["git", "ls-tree", "-r", "--name-only", generation_ref,
                          "--", "assets"], cwd=root).splitlines()
        config["generated"] = ["index.html", "en/index.html", "ja/index.html",
                               *[f"radar/daily/{issue}.{lang}.md" for lang in ["en", "zh", "ja"]],
                               *[p for p in assets if p.endswith(".svg")]]
    if task is None:
        identifier = day + "-" + name + "-" + datetime.now(timezone.utc).strftime("%H%M%S%f")
        task = {"id": identifier, "repo": repo, "created_at": stamp(), "created_on": day, "base": main,
                "branch": "automation/feature-" + identifier, "phase": "plan",
                "attempts": 0}
        write_json(active, task)
    state = task_dir / task["id"]
    state.mkdir(exist_ok=True)
    if plan_only and task["phase"] in {"push", "ci"}:
        return {"repo": repo, "status": "planned", "feature_id": task["id"], "plan": task["plan"]}
    if task["phase"] in {"plan", "implement"} and (
        task["attempts"] >= 9 or task.get("planning_attempts", 0) >= 9
    ):
        return retire(task, state, active, service_state, name, day,
                      "Nine implementation attempts failed; retained diagnostics, no feature released. "
                      "A smaller independently reviewed plan may be selected next day.")
    if task.get("phase_attempts", {}).get(task["phase"], 0) >= 9:
        return retire(task, state, active, service_state, name, day,
                      "Nine resumptions of the same phase did not complete. "
                      "Retained the source/publication state and diagnostics; no successful release claimed.")
    if task["phase"] in {"validate", "acceptance-review", "push", "ci"}:
        if plan_only:
            return {"repo": repo, "status": "planned", "feature_id": task["id"], "plan": task["plan"]}
        try:
            restore(root, task, state)
            return advance(root, task, config, state, active, service_state, name, day)
        except Exception as error:
            task["feedback"] = str(error)
            if task["phase"] in {"validate", "acceptance-review"}:
                task["phase"] = "implement"
                write_json(active, task)
                raise
            if not task.get("pr"):
                write_json(active, task)
                raise
            record = json.loads(gh(repo, "pr", "view", str(task["pr"]),
                                   "--json", "state,headRefOid"))
            changed = record["headRefOid"] != task["head"] or record["state"] == "CLOSED"
            needs_correction = "CI failed or was cancelled" in str(error) or "Main advanced" in str(error)
            if changed or (record["state"] == "OPEN" and needs_correction):
                if not changed:
                    gh(repo, "pr", "close", str(task["pr"]), "--delete-branch")
                # Same feature, fresh validation and independent review on the correction.
                task["phase"] = "implement"
                task["base"] = main
                task["branch"] += "-r" + str(task["attempts"])
            write_json(active, task)
            raise
    command(["git", "checkout", "-B", task["branch"], main], cwd=root)
    reset(root, main)
    task["base"] = main
    ctx = context(root, name, inputs, config)
    if "plan" not in task:
        prompt = (
            'Choose ONE complete, small feature with real product behavior. Inspect the existing '
            'README, file tree and issues. FIRST read implementation before asserting a gap: '
            'return {"need_files":["path",{"path":"path","search":"symbol"},'
            '{"path":"path","start_line":1,"end_line":200}]} to inspect source. '
            'If prior feedback shows the behavior already exists, abandon that proposal and '
            'choose a different capability; do not restate the existing behavior as a new feature. '
            'After inspection return {"title":"feat: ...","problem":"...",'
            '"behavior":"...","why_this_repo":"...","acceptance":["observable behavior"],'
            '"read_paths":["implementation and relevant existing tests"],'
            '"inspect_ranges":[{"path":"optional large source file","start_line":1,"end_line":200}],'
            '"test_path":"tests/a new focused behavioral test file",'
            '"test_runner":"unittest|pytest|vitest|node|php",'
            '"gpu_test_path":null,"gpu_reason":null}. GPU is optional when useful. '
            'Choose a test_runner from context.test_runners. GPU experiment files must not '
            'match the CPU suite discovery pattern; place a standalone probe under tests/experiments/. '
            'Acceptance tests belong directly in tests/: test_*.py, *.test.cjs or *.test.ts, '
            'or *.php for WordPress. Include product, existing-test and documentation paths; '
            'WordPress also needs ai-chat-for-amazon-bedrock.php and readme.txt for the version increment. '
            'Keep each description below 4000 characters and acceptance to at most eight conditions. '
            'Do not choose pure documentation, a broad refactor or multiple independent features.\n'
        )
        for plan_attempt in range(3):
            try:
                task["planning_attempts"] = task.get("planning_attempts", 0) + 1
                write_json(active, task)
                for inspection in range(6):
                    instruction = prompt
                    if inspection == 5:
                        instruction += ('The source inspection budget is now exhausted. Return the '
                                        'one feature plan grounded in the source already inspected; '
                                        'do not request further files in this round.\n')
                    candidate_plan = ask(instruction + json.dumps(ctx, ensure_ascii=False) +
                                         "\nPrior findings:\n" + task.get("feedback", "")[-6000:],
                                         state, f"plan-{plan_attempt}-read-{inspection}")
                    if not candidate_plan.get("need_files"):
                        break
                    requests = candidate_plan["need_files"]
                    if not isinstance(requests, list) or len(requests) > 6:
                        raise ValueError("Read at most six focused source excerpts per inspection")
                    inspected = ctx.setdefault("inspected_files", {})
                    for request in requests:
                        key, value = requested_context(root, request, config, max_bytes=10000)
                        inspected[key] = value
                    while sum(len(s.encode()) for s in inspected.values()) > 45000:
                        inspected.pop(next(iter(inspected)))
                runner = candidate_plan.get("test_runner")
                suffix = {"unittest": ".py", "pytest": ".py", "node": ".test.cjs",
                          "vitest": ".test.ts", "php": ".php"}.get(runner)
                if suffix:
                    key = hashlib.sha256(task["id"].encode()).hexdigest()[:12]
                    candidate_plan["test_path"] = "tests/test_feature_" + key + suffix
                if name == "ai-chat-for-amazon-bedrock" and "read_paths" in candidate_plan:
                    for required in [SLUG + ".php", "readme.txt"]:
                        if required not in candidate_plan["read_paths"]:
                            candidate_plan["read_paths"].append(required)
                validate_plan(candidate_plan, config)
                if (root / candidate_plan["test_path"]).exists():
                    raise ValueError("Choose a NEW behavioral test file; existing regression tests are immutable")
                inspected = bounded_files(root, candidate_plan["read_paths"], 28000)
                # Retain source excerpts used to establish the missing capability.
                for key, value in reversed(list(ctx.get("inspected_files", {}).items())):
                    if key not in inspected and sum(len(s.encode()) for s in inspected.values()) + len(value.encode()) <= 45000:
                        inspected[key] = value
                ctx["inspected_files"] = inspected
                ranges = candidate_plan.get("inspect_ranges", [])
                if not isinstance(ranges, list) or len(ranges) > 5:
                    raise ValueError("Read at most five focused source ranges for the plan")
                for request in ranges:
                    remaining = max(1000, 45000 - sum(
                        len(s.encode()) for s in ctx["inspected_files"].values()))
                    key, value = requested_context(root, request, config, max_bytes=min(24000, remaining))
                    ctx["inspected_files"][key] = value
                if sum(len(s.encode()) for s in ctx["inspected_files"].values()) > 60000:
                    ctx.pop("inspected_files")
                    raise ValueError("Request fewer source ranges; inspected context exceeds its budget")
                review = ask(
                    'Review this feature plan for ONE complete capability, project fit, compatibility, '
                    'testability and realistic scope. Return {"approved":true|false,"findings":[]}.\n' +
                    json.dumps({"plan": candidate_plan, "context": ctx}, ensure_ascii=False),
                    state, f"plan-review-{plan_attempt}", review=True)
                require_review(review)
                task["plan"] = candidate_plan
                break
            except Exception as error:
                task["feedback"] = str(error)
                write_json(active, task)
        else:
            raise RuntimeError("Feature planning needs correction: " + task["feedback"])
        task["phase"] = "implement"
        write_json(active, task)
    plan = task["plan"]
    if plan_only:
        return {"repo": repo, "status": "planned", "feature_id": task["id"], "plan": plan}
    files = bounded_files(root, plan["read_paths"], 50000)
    for request in plan.get("inspect_ranges", []):
        key, value = requested_context(root, request, config)
        files[key] = value
    for attempt in range(3):
        task["attempts"] += 1
        attempt_dir = state / f"attempt-{task['attempts']}"
        attempt_dir.mkdir(exist_ok=True)
        reset(root, main)
        task["phase"] = "implement"
        write_json(active, task)
        try:
            prompt = (
                'Implement the saved feature completely, with functional code, focused behavioral '
                'tests in new files and usage docs. Preserve all existing tests unchanged. '
                'The behavioral test must fail before the feature and pass '
                'after it. Do not merely test implementation details. Return '
                '{"edits":[{"path":"relative path","old":"exact unique existing span or null '
                'for a new file","new":"replacement or full new file"}]}. '
                'If essential context is missing, return {"need_files":["relative paths",'
                '{"path":"relative path","start_line":1,"end_line":200},'
                '{"path":"relative path","search":"literal symbol"}]} instead. '
                'Preserve PHP 7.4 support for WordPress. Do not change the chosen capability.\n')
            for read_round in range(4):
                payload = {"plan": plan, "mission": config["mission"], "files": files,
                           "feedback": task.get("feedback", "")[-10000:]}
                proposal = ask(prompt + json.dumps(payload, ensure_ascii=False),
                               attempt_dir, f"author-{read_round}")
                if not proposal.get("need_files"):
                    break
                requested = proposal["need_files"]
                if not isinstance(requested, list) or len(requested) > 10:
                    raise ValueError("Invalid context request")
                for request in requested:
                    key, value = requested_context(root, request, config)
                    files[key] = value
                if sum(len(t.encode()) for t in files.values()) > 75000:
                    raise ValueError("Feature context exceeds its review budget; narrow the implementation")
            changed = apply(root, proposal, config, plan)
            command(["git", "add", "--", *changed], cwd=root)
            diff = command(["git", "diff", "--cached", "--no-ext-diff"], cwd=root)
            if len(diff.encode()) > 80000:
                raise ValueError("Feature diff exceeds independent review budget")
            review = ask(
                'Review the complete feature diff and acceptance. Reject incomplete wiring, '
                'multiple unrelated features, weakened checks, compatibility/security regressions, '
                'unsupported claims and unsafe GPU work. Return {"approved":true|false,"findings":[]}.\n' +
                json.dumps({"plan": plan, "mission": config["mission"], "diff": diff}),
                attempt_dir, "code-review", review=True)
            require_review(review)
            command(["git", "commit", "-m", plan["title"][:120]], cwd=root)
            task["head"] = command(["git", "rev-parse", "HEAD"], cwd=root).strip()
            task["changed"] = changed
            task["phase"] = "validate"
            checkpoint(root, task, state, active)
            return advance(root, task, config, state, active, service_state, name, day)
        except Exception as error:
            task["feedback"] = str(error)
            write_json(attempt_dir / "failure.json", {"error": str(error), "at": stamp()})
            write_json(active, task)
            if task["phase"] in {"push", "ci"}:
                raise
            task["phase"] = "implement"
            write_json(active, task)
    raise RuntimeError("Same feature retained for automatic correction: " + task["feedback"][-3000:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/ai-feature-agent")
    parser.add_argument("--workspace", type=Path, default=Path.home() / ".local/share/ai-feature-agent")
    parser.add_argument("--repo", action="append", choices=list(PROJECTS))
    parser.add_argument("--date")
    parser.add_argument("--radar-snapshot", type=Path)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--plan-only", action="store_true",
                        help="save independently reviewed plans without executing or publishing code")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o022)
    args.state.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    day = run_day(args.date)
    selected = args.repo or list(PROJECTS)
    state = args.state / day
    state.mkdir(exist_ok=True)
    if args.worker and (os.environ.get("FEATURE_LOCK_HELD") != "1" or len(selected) != 1):
        parser.error("Feature workers must be started by the locked daily controller")
    lock_context = nullcontext(None) if args.worker else (args.state / "daily.lock").open("a")
    with lock_context as lock:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inputs = read_json(state / "sources.json") if args.worker else snapshot(args.radar_snapshot)
        if not args.worker:
            write_json(state / "sources.json", inputs)
        if args.collect_only:
            print(json.dumps({"projects": selected, "sources": len(inputs["sources"])}))
            return 0
        results = []
        for name in selected:
            receipt = state / name / "result.json"
            old = read_json(receipt, {})
            if old.get("status") in {"published", "deferred"}:
                results.append(old)
                continue
            if args.worker:
                try:
                    result = develop(name, inputs, args.workspace, args.state, day,
                                     plan_only=args.plan_only)
                except Exception as error:
                    result = {"repo": "noteflowai/" + name, "status": "retry-needed",
                              "error": str(error), "at": stamp()}
            else:
                receipt.parent.mkdir(parents=True, exist_ok=True)
                argv = [sys.executable, str(Path(__file__).resolve()),
                        "--state", str(args.state.resolve()), "--workspace", str(args.workspace.resolve()),
                        "--date", day, "--repo", name, "--worker"]
                if args.plan_only:
                    argv.append("--plan-only")
                code = execute_stage(argv, Path(__file__).resolve().parents[1],
                                     {**os.environ, "FEATURE_LOCK_HELD": "1"},
                                     receipt.parent / "worker.log", 2100)
                result = read_json(receipt, {"repo": "noteflowai/" + name,
                                            "status": "retry-needed", "at": stamp()})
                if code != 0 and result.get("status") not in {"published", "deferred"}:
                    result.update(status="retry-needed", worker_exit_code=code)
            write_json(receipt, result)
            results.append(result)
            print(json.dumps(result), flush=True)
        if not args.worker:
            write_json(state / "results.json", results)
        successful = {"published", "planned"} if args.plan_only else {"published"}
        return 1 if any(r["status"] not in successful for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
