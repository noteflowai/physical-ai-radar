"""Issue immutable releases and verify the packages users can actually install."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

try:
    from .agent_pipeline import command, gh, write_json
    from .feature_versions import version, WP, NPM
    from .security_gate import require_safe_publication
except ImportError:
    from agent_pipeline import command, gh, write_json
    from feature_versions import version, WP, NPM
    from security_gate import require_safe_publication

REPOS = {"physical-ai-radar", NPM, "evalarc", "robot-reel", WP}


class ReleasePending(RuntimeError):
    """Keep the same release transaction while remote work finishes."""


def api(path: str, *args: str):
    return json.loads(command(["gh", "api", path, *args]))


def optional_api(path: str):
    result = subprocess.run(["gh", "api", path], text=True, capture_output=True, timeout=60)
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "(HTTP 404)" in result.stderr:
        return None
    raise RuntimeError(result.stderr[-2000:])


def fetch(url: str, limit: int = 134217728) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Release readback requires HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "NoteFlowAI-Release/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Release artifact exceeds readback budget")
    return body


def public_json(url: str):
    try:
        return json.loads(fetch(url, 4_000_000))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ReleasePending("The public registry has not exposed this version") from None
        raise


def tag_commit(repo: str, tag: str) -> str | None:
    item = optional_api(f"repos/{repo}/git/ref/tags/{tag}")
    if not item:
        return None
    obj = item["object"]
    for _ in range(5):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api(f"repos/{repo}/git/tags/{obj['sha']}")["object"]
    raise ValueError("Release tag must resolve to a commit")


def ensure_tag(repo: str, tag: str, commit: str) -> None:
    current = tag_commit(repo, tag)
    if current is None:
        api(f"repos/{repo}/git/refs", "-f", "ref=refs/tags/" + tag, "-f", "sha=" + commit)
        current = tag_commit(repo, tag)
    if current != commit:
        raise ValueError("An immutable release tag already points to different source")


def workflow(repo: str, filename: str, commit: str) -> dict | None:
    runs = json.loads(gh(repo, "run", "list", "--workflow", filename, "--commit", commit,
                         "--limit", "30", "--json",
                         "databaseId,headSha,status,conclusion,event,url"))
    return max((r for r in runs if r["headSha"] == commit),
               key=lambda r: r["databaseId"], default=None)


def dispatch(repo: str, filename: str, tag: str, commit: str, state: Path, fields: dict) -> None:
    marker = state / (filename + ".dispatch.json")
    prior = json.loads(marker.read_text()) if marker.exists() else {}
    if prior and prior["commit"] != commit:
        raise ValueError("Dispatch receipt belongs to different source")
    run = workflow(repo, filename, commit)
    if run and run["status"] != "completed":
        raise ReleasePending("The exact release workflow is still running")
    if run and run["conclusion"] == "success":
        return
    now = datetime.now(timezone.utc)
    if prior and (run["databaseId"] if run else None) == prior.get("run"):
        if (now - datetime.fromisoformat(prior["requested_at"])).total_seconds() < 600:
            raise ReleasePending("Waiting for the dispatched release workflow to appear")
    if prior.get("attempts", 0) >= 3:
        raise RuntimeError("Release workflow failed repeatedly; retained the exact release for repair")
    args = ["workflow", "run", filename, "--ref", tag]
    for key, value in fields.items():
        args += ["-f", key + "=" + value]
    write_json(marker, {"commit": commit, "run": run["databaseId"] if run else None,
                        "attempts": prior.get("attempts", 0) + 1,
                        "requested_at": now.isoformat()})
    gh(repo, *args)
    raise ReleasePending("Started the exact tagged release workflow; resume after completion")


def release_notes(source: dict, number: str) -> str:
    plan = source["feature"]
    delivery = source.get("delivery", {})
    title = delivery.get("summary") or plan["behavior"]
    usage = delivery.get("usage") or plan.get("usage") or (
        "See the usage documentation linked from the source pull request.")
    limits = delivery.get("limitations") or plan.get("limitations") or (
        "Scope is limited to the behavior and acceptance checks in the source pull request.")
    upgrade = delivery.get("upgrade") or plan.get("upgrade") or (
        "Use this version's download and retain the previous release for rollback.")
    marker = f"<!-- feature-release:{source['feature_id']} -->"
    gpu = any(r.get("gpu") and r.get("exit_code") == 0 for r in source.get("checks", []))
    return (
        f"{marker}\n{title}\n\n### Use it\n\n{usage}\n\n### Scope and upgrade\n\n"
        f"{limits}\n\n{upgrade}\n\n### Verification\n\n"
        f"- Source: {source['url']}\n- Version: `{number}`\n"
        f"- Verified source commit: `{source['merge_commit']}`\n"
        "- Behavioral acceptance and the repository's release checks passed.\n"
        + ("- A reviewed GPU experiment was executed; its logs are retained.\n" if gpu else "")
        + "\nAuthored and reviewed with agent assistance; no human review is claimed.\n")


def retry_tag_workflow(repo: str, run: dict | None, state: Path) -> None:
    """Retry failed jobs only; successful uploads and source gates are never replayed."""
    if not run or run["status"] != "completed":
        raise ReleasePending("The tagged release workflow is still preparing the release")
    if run["conclusion"] == "success":
        raise ReleasePending("Waiting for public registry readback after successful publication")
    marker = state / "tag-workflow-retry.json"
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    remote = api(f"repos/{repo}/actions/runs/{run['databaseId']}")
    attempt = remote["run_attempt"]
    now = datetime.now(timezone.utc)
    if remote["status"] != "completed":
        raise ReleasePending("Waiting for the requested workflow retry")
    if (previous.get("attempt") == attempt
            and (now - datetime.fromisoformat(previous["requested_at"])).total_seconds() < 600):
        raise ReleasePending("Waiting for the requested workflow retry to appear")
    if previous.get("count", 0) >= 3:
        raise RuntimeError("Tagged release failed repeatedly; preserve the version for repair")
    write_json(marker, {"run": run["databaseId"], "attempt": attempt,
                        "count": previous.get("count", 0) + 1, "requested_at": now.isoformat()})
    gh(repo, "run", "rerun", str(run["databaseId"]), "--failed")
    raise ReleasePending("Retrying failed release jobs on the same tag and artifacts")


def asset_files(repo: str, release: dict, state: Path, suffixes: tuple[str, ...]) -> list[Path]:
    target = state / "release-assets"
    target.mkdir(exist_ok=True)
    files = []
    for asset in release.get("assets", []):
        name = asset["name"]
        if not name.endswith(suffixes):
            continue
        if asset.get("state", "uploaded") != "uploaded":
            raise ReleasePending("The release still contains an incomplete upload")
        if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid release asset filename")
        if asset.get("size", 0) > 134217728:
            raise ValueError("Release asset exceeds byte budget")
        digest = asset.get("digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ValueError("Release asset lacks GitHub's upload digest")
        file = target / name
        valid = file.exists() and "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest() == digest
        if not valid:
            if file.exists():
                file.unlink()
            gh(repo, "release", "download", release["tag_name"], "--pattern", name,
               "--dir", str(target), timeout=180)
        if "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError("Downloaded release asset does not match the checked upload")
        files.append(file)
    if not files:
        raise ReleasePending("The checked release assets are not available yet")
    return files


def verify_pypi(name: str, number: str, files: list[Path]) -> dict:
    normal = name.replace("-", "_")
    expected = {f"{normal}-{number}-py3-none-any.whl", f"{normal}-{number}.tar.gz"}
    if {p.name for p in files} != expected:
        raise ValueError("Python release needs exactly the expected wheel and source archive")
    metadata = public_json(f"https://pypi.org/pypi/{name}/{number}/json")
    if metadata["info"]["name"].replace("_", "-") != name or metadata["info"]["version"] != number:
        raise ValueError("PyPI returned a different package identity")
    published = {item["filename"]: item for item in metadata["urls"]}
    if set(published) != expected:
        raise ValueError("PyPI has a different set of distributions")
    receipts = []
    for file in files:
        item, body = published[file.name], file.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if item["digests"]["sha256"] != digest:
            raise ValueError("PyPI digest differs from the tested GitHub release artifact")
        url = item["url"]
        if urllib.parse.urlsplit(url).netloc != "files.pythonhosted.org":
            raise ValueError("Unexpected PyPI artifact host")
        if fetch(url, len(body)) != body:
            raise ValueError("Public PyPI bytes differ from the tested artifact")
        receipts.append({"name": file.name, "sha256": digest, "url": url})
    return {"status": "verified", "url": f"https://pypi.org/project/{name}/{number}/",
            "files": receipts}


def verify_npm(number: str, files: list[Path]) -> dict:
    if len(files) != 1 or files[0].name != f"{NPM}-{number}.tgz":
        raise ValueError("npm release needs its one versioned tarball")
    body = files[0].read_bytes()
    metadata = public_json(f"https://registry.npmjs.org/{NPM}/{number}")
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
    if (metadata.get("name") != NPM or metadata.get("version") != number
            or metadata.get("dist", {}).get("integrity") != integrity):
        raise ValueError("npm identity or integrity differs from the release")
    url = f"https://registry.npmjs.org/{NPM}/-/{NPM}-{number}.tgz"
    if fetch(url, len(body)) != body:
        raise ValueError("Public npm bytes differ from the checked release")
    return {"status": "verified", "url": f"https://www.npmjs.com/package/{NPM}/v/{number}",
            "sha256": hashlib.sha256(body).hexdigest(), "integrity": integrity}


def verify_mcp(number: str) -> dict:
    server = urllib.parse.quote(f"io.github.noteflowai/{NPM}", safe="")
    url = f"https://registry.modelcontextprotocol.io/v0.1/servers/{server}/versions/{number}"
    item = public_json(url)
    record = item.get("server", item)
    if (record.get("name") != f"io.github.noteflowai/{NPM}" or record.get("version") != number
            or not any(p.get("identifier") == NPM and p.get("version") == number
                       and p.get("registryType") == "npm" for p in record.get("packages", []))):
        raise ValueError("MCP registry identity differs from the checked npm release")
    return {"status": "verified", "url": url}


def managed_release(repo: str, tag: str, source: dict, files: list[Path],
                    state: Path, number: str) -> dict:
    item = optional_api(f"repos/{repo}/releases/tags/{tag}")
    notes = state / "release-notes.md"
    notes.write_text(release_notes(source, number))
    require_safe_publication(source["feature"]["title"] + "\n" + notes.read_text())
    if item is None:
        # `gh release view` also finds an interrupted draft hidden from public APIs.
        probe = subprocess.run(["gh", "release", "view", tag, "--repo", repo,
                                "--json", "databaseId"], capture_output=True, text=True, timeout=60)
        if probe.returncode == 0:
            item = api(f"repos/{repo}/releases/{json.loads(probe.stdout)['databaseId']}")
        elif "release not found" in probe.stderr.lower():
            gh(repo, "release", "create", tag, "--verify-tag", "--draft",
               "--title", source["feature"]["title"], "--notes-file", str(notes))
            found = json.loads(gh(repo, "release", "view", tag, "--json", "databaseId"))
            item = api(f"repos/{repo}/releases/{found['databaseId']}")
        else:
            raise RuntimeError(probe.stderr[-2000:])
    if f"feature-release:{source['feature_id']}" not in item.get("body", ""):
        raise ValueError("Existing release is not owned by this feature; preserve its contents")
    require_safe_publication(item.get("body", ""))
    existing = {a["name"]: a for a in item.get("assets", [])}
    for file in files:
        digest = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
        if file.name in existing:
            asset = existing[file.name]
            if (item["draft"] and asset.get("state") == "starter"
                    and asset.get("size") == 0 and not asset.get("digest")):
                # GitHub may retain an empty failed-upload placeholder. No published
                # bytes are removed, and the owned draft retains its original source.
                current = api(f"repos/{repo}/releases/assets/{asset['id']}")
                if (current.get("state") != "starter" or current.get("size") != 0
                        or current.get("digest") or current["name"] != file.name):
                    raise ReleasePending("The failed upload changed while preparing recovery")
                command(["gh", "api", f"repos/{repo}/releases/assets/{asset['id']}", "-X", "DELETE"])
                gh(repo, "release", "upload", tag, str(file), timeout=180)
            elif asset.get("digest") != digest or asset.get("state", "uploaded") != "uploaded":
                raise ValueError("Existing immutable release asset differs from the CI package")
        else:
            gh(repo, "release", "upload", tag, str(file), timeout=180)
    item = api(f"repos/{repo}/releases/{item['id']}")
    uploaded = {a["name"]: a for a in item.get("assets", [])}
    for file in files:
        asset = uploaded.get(file.name, {})
        if (asset.get("state") != "uploaded"
                or asset.get("digest") != "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()):
            raise ReleasePending("Keep the draft until every CI asset has a verified upload")
    if item["draft"]:
        gh(repo, "release", "edit", tag, "--draft=false", "--latest")
    return api(f"repos/{repo}/releases/tags/{tag}")


def ci_distributions(repo: str, ci: dict, commit: str, number: str, state: Path) -> list[Path]:
    """Bind the reusable package cache to its exact successful CI and bytes."""
    if ci["headSha"] != commit or ci["conclusion"] != "success":
        raise ValueError("Distribution artifact must come from successful exact-commit CI")
    target, receipt = state / "ci-distributions", state / "ci-distributions.json"
    saved = json.loads(receipt.read_text()) if receipt.exists() else {}
    expected = {f"evalarc-{number}-py3-none-any.whl", f"evalarc-{number}.tar.gz"}
    if saved:
        if saved["commit"] != commit or saved["run"] != ci["databaseId"]:
            raise ValueError("CI distribution cache belongs to a different build")
        if set(saved["files"]) != expected or {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()
        } != saved["files"]:
            raise ValueError("CI distribution cache changed after verification")
    else:
        # An interrupted download has no receipt and cannot become a release.
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        gh(repo, "run", "download", str(ci["databaseId"]), "--name", "release-distributions",
           "--dir", str(target), timeout=180)
        files = list(target.iterdir())
        if {p.name for p in files} != expected or any(not p.is_file() or p.is_symlink() for p in files):
            raise ValueError("Exact CI must supply only the expected wheel and source archive")
        write_json(receipt, {"commit": commit, "run": ci["databaseId"],
                            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}})
    return sorted(target.iterdir())


def publish(root: Path, source: dict, state: Path) -> dict:
    """Return only after installable distribution readback; pending work is durable."""
    repo = source["repo"]
    name = repo.removeprefix("noteflowai/")
    commit = source["merge_commit"]
    if name not in REPOS or repo != "noteflowai/" + name or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Unexpected release identity")
    if source.get("status") not in {"published", "source-verified"}:
        raise ValueError("Source publication has not passed its checks")
    state.mkdir(parents=True, exist_ok=True)
    command(["git", "fetch", "origin", "main"], cwd=root)
    number = version(root, name, commit)
    tag = number if name == WP else "v" + number
    if name == WP:
        wordpress = source.get("wordpress_org", {})
        package = source.get("package", {})
        if (wordpress.get("status") != "published" or wordpress.get("version") != number
                or wordpress.get("package_sha256") != package.get("sha256")):
            raise ValueError("WordPress.org receipt must verify this version's exact CI package")
        file = Path(package["path"])
        if hashlib.sha256(file.read_bytes()).hexdigest() != package["sha256"]:
            raise ValueError("WordPress CI package changed")
    ensure_tag(repo, tag, commit)
    proof = {"version": number, "tag": tag, "commit": commit, "status": "pending"}
    write_json(state / "release.json", proof)
    registries = {}
    if name == WP:
        item = managed_release(repo, tag, source, [file], state, number)
        downloads = asset_files(repo, item, state, (".zip",))
        if len(downloads) != 1 or downloads[0].read_bytes() != file.read_bytes():
            raise ValueError("GitHub plugin asset differs from WordPress CI artifact")
        registries["wordpress"] = source["wordpress_org"]
    elif name == "physical-ai-radar":
        item = managed_release(repo, tag, source, [], state, number)
    elif name == "evalarc":
        ci = next(r for r in source["workflows"] if r["workflowName"] == "CI"
                  and r["headSha"] == commit and r["conclusion"] == "success")
        files = ci_distributions(repo, ci, commit, number, state)
        item = managed_release(repo, tag, source, files, state, number)
        downloads = asset_files(repo, item, state, (".whl", ".tar.gz"))
        try:
            registries["pypi"] = verify_pypi(name, number, downloads)
        except ReleasePending:
            dispatch(repo, "publish-pypi.yml", tag, commit, state,
                     {"tag": tag, "repository": "pypi"})
            raise
    else:
        run = workflow(repo, "release.yml", commit)
        item = optional_api(f"repos/{repo}/releases/tags/{tag}")
        if item is None:
            retry_tag_workflow(repo, run, state)
        try:
            files = asset_files(repo, item, state, (".tgz",) if name == NPM else (".whl", ".tar.gz"))
            expected = ({f"{name}-{number}.tgz"} if name == NPM else
                        {f"robot_reel-{number}-py3-none-any.whl", f"robot_reel-{number}.tar.gz"})
            if not expected.issubset({p.name for p in files}):
                raise ReleasePending("The tagged release is missing a required package")
        except ReleasePending:
            retry_tag_workflow(repo, run, state)
        if name == NPM:
            try:
                registries["npm"] = verify_npm(number, files)
            except ReleasePending:
                retry_tag_workflow(repo, run, state)
            try:
                registries["mcp"] = verify_mcp(number)
            except ReleasePending:
                if run and run["status"] == "completed":
                    dispatch(repo, "registry-recovery.yml", tag, commit, state, {"tag": tag})
                raise
        else:
            try:
                registries["pypi"] = verify_pypi(name, number, files)
            except ReleasePending:
                retry_tag_workflow(repo, run, state)
        if run is None:
            raise ValueError("No workflow proves this tagged release was built")
        # Registry byte verification can finish an upload whose final network read failed.
        jobs = json.loads(gh(repo, "run", "view", str(run["databaseId"]), "--json", "jobs"))["jobs"]
        required = ["GitHub release"] if name == NPM else ["version", "github"]
        if not all(any(j["name"] == n and j["conclusion"] == "success" for j in jobs) for n in required):
            retry_tag_workflow(repo, run, state)
        notes = state / "release-notes.md"
        marker = f"feature-release:{source['feature_id']}"
        if marker not in item.get("body", ""):
            notes.write_text(item.get("body", "") + "\n\n" + release_notes(source, number))
            require_safe_publication(notes.read_text())
            gh(repo, "release", "edit", tag, "--notes-file", str(notes))
            item = api(f"repos/{repo}/releases/tags/{tag}")
        proof["workflow"] = run
    proof.update(status="verified", url=item["html_url"], registries=registries,
                 release_id=item["id"], assets=[
                     {k:a.get(k) for k in ["name", "digest", "browser_download_url", "download_count"]}
                     for a in item.get("assets", [])])
    write_json(state / "release.json", proof)
    return proof


def backfill(root: Path, receipt: Path, service_state: Path, delivery: Path | None = None) -> dict:
    """Finish distribution for a previously completed feature without creating a feature."""
    try:
        from .feature_updates import queue
    except ImportError:
        from feature_updates import queue
    source = json.loads(receipt.read_text())
    if source.get("status") != "published" or not source.get("completed_on"):
        raise ValueError("Backfill requires a completed source publication receipt")
    if delivery:
        source["delivery"] = json.loads(delivery.read_text())
    state = receipt.parent
    source["distribution"] = publish(root, source, state / "distribution")
    source.setdefault("release_verified_at", datetime.now(timezone.utc).isoformat())
    write_json(state / "distribution-complete.json", source)
    queue(source, service_state)
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resume distribution for a completed source release")
    parser.add_argument("--backfill", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--delivery", type=Path)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/ai-feature-agent")
    args = parser.parse_args()
    print(json.dumps(backfill(args.root, args.backfill, args.state, args.delivery)["distribution"]))
