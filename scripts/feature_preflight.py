"""Read-only GitHub publication prerequisites, checked before a feature worker starts."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import time

try:
    from .agent_pipeline import write_json
    from .feature_policy import PROJECTS
    from .feature_versions import read_version
except ImportError:
    from agent_pipeline import write_json
    from feature_policy import PROJECTS
    from feature_versions import read_version


WORKFLOWS = {
    "physical-ai-radar": ["ci.yml"],
    "dsh-skills-anywhere": ["ci.yml", "huggingface.yml", "release.yml", "registry-recovery.yml"],
    "evalarc": ["ci.yml", "publish-pypi.yml"],
    "robot-reel": ["check.yml", "huggingface.yml", "release.yml", "validate.yml"],
    "ai-chat-for-amazon-bedrock": ["gate.yml"],
}
ENVIRONMENTS = {"dsh-skills-anywhere": "npm", "evalarc": "pypi", "robot-reel": "pypi"}


class GitHub:
    def __init__(self, name: str):
        self.repo = "noteflowai/" + name
        self.deadline = time.monotonic() + 45

    def __call__(self, suffix: str):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GitHub publication preflight exceeded 45 seconds")
        result = subprocess.run(
            ["gh", "api", f"repos/{self.repo}" + ("/" + suffix if suffix else "")],
            capture_output=True, text=True, timeout=min(12, remaining))
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:])
        return json.loads(result.stdout)


def environment_errors(environment: dict, policies: list[dict], tag: str) -> list[str]:
    errors = []
    for rule in environment.get("protection_rules", []):
        kind = rule.get("type")
        if kind == "required_reviewers":
            errors.append("Publishing requires an interactive environment reviewer")
        elif kind not in {"branch_policy", "wait_timer"}:
            errors.append("Unrecognized environment protection requires maintainer inspection")
    restriction = environment.get("deployment_branch_policy")
    if restriction:
        if restriction.get("protected_branches"):
            # GitHub's protected-branches mode permits protected branches, not
            # an arbitrary version tag. The controller dispatches the exact tag.
            errors.append("Environment allows protected branches rather than the release tag")
        elif restriction.get("custom_branch_policies"):
            # Release tags contain no slash. GitHub uses Ruby File.fnmatch; for
            # these names the basic wildcard operators agree with fnmatchcase.
            if not any(p.get("type") == "tag" and isinstance(p.get("name"), str)
                       and fnmatch.fnmatchcase(tag, p["name"]) for p in policies):
                errors.append(f"Environment does not permit release tag {tag}")
        else:
            errors.append("Unknown deployment branch policy")
    return errors


def check_project(name: str, api=None, *, release_tag: str | None = None) -> dict:
    if name not in PROJECTS:
        raise ValueError("Unknown specialist project")
    api = api or GitHub(name)
    result = {"repo": "noteflowai/" + name, "status": "ready",
              "checked_at": datetime.now(timezone.utc).isoformat(), "errors": []}
    errors = result["errors"]
    try:
        repository = api("")
        if repository.get("archived") or repository.get("disabled"):
            errors.append("Repository is archived or disabled")
        if repository.get("permissions", {}).get("push") is not True:
            errors.append("The scheduling identity lacks repository push permission")
        head = api("git/ref/heads/main")["object"]["sha"]
        result["source_commit"] = head
        listed = api("actions/workflows?per_page=100")["workflows"]
        active = {w["path"] for w in listed if w["state"] == "active"}
        required = {".github/workflows/" + p for p in WORKFLOWS[name]}
        errors.extend("Required workflow is missing or disabled: " + p for p in sorted(required - active))
        if name in ENVIRONMENTS:
            def read(path):
                item = api(f"contents/{path}?ref={head}")
                if item.get("encoding") != "base64":
                    raise ValueError("Release metadata is not a readable GitHub file")
                return base64.b64decode(item["content"], validate=False).decode("utf-8")
            if release_tag is None:
                old = read_version(name, read)
                major, minor, _ = map(int, old.split("."))
                tag = f"v{major}.{minor + 1}.0"
            elif re.fullmatch(r"v\d+\.\d+\.\d+", release_tag):
                tag = release_tag
            else:
                raise ValueError("A resumed package release needs its exact stable version tag")
            environment_name = ENVIRONMENTS[name]
            environment = api("environments/" + environment_name)
            restrictions = environment.get("deployment_branch_policy") or {}
            policies = []
            if restrictions.get("custom_branch_policies"):
                policies = api("environments/" + environment_name +
                               "/deployment-branch-policies?per_page=100")["branch_policies"]
            result.update(environment=environment_name, release_tag=tag,
                          deployment_branch_policy=restrictions, branch_policies=policies)
            result["wait_timer_minutes"] = sum(
                r.get("wait_timer", 0) for r in environment.get("protection_rules", [])
                if r.get("type") == "wait_timer")
            errors.extend(environment_errors(environment, policies, tag))
        if name == "robot-reel":
            variable = api("actions/variables/PYPI_PUBLISH_ENABLED")
            if variable.get("value") != "true":
                errors.append("PYPI_PUBLISH_ENABLED must be true for the required PyPI distribution")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        errors.append("Unable to verify publication prerequisites: " + str(error))
    if errors:
        result["status"] = "blocked"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", choices=list(PROJECTS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = [check_project(name) for name in (args.repo or list(PROJECTS))]
    if args.output:
        write_json(args.output, records)
    print(json.dumps(records, indent=2))
    return int(any(r["status"] != "ready" for r in records))


if __name__ == "__main__":
    raise SystemExit(main())
