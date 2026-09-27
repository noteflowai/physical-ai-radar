"""Controller-owned release metadata. Never delegate dependency edits to a model."""
from __future__ import annotations

import json
from pathlib import Path
import re

try:
    from .agent_pipeline import command
except ImportError:
    from agent_pipeline import command

VERSION = r"\d+\.\d+\.\d+"
NPM = "dsh-skills-anywhere"
WP = "ai-chat-for-amazon-bedrock"


def version(root: Path, name: str, ref: str | None = None) -> str:
    def read(path):
        return command(["git", "show", f"{ref}:{path}"], cwd=root) if ref else (root / path).read_text()
    return read_version(name, read)


def read_version(name: str, read) -> str:
    """Use one metadata contract for local release preparation and remote preflight."""
    if name == NPM:
        value = json.loads(read("package.json"))["version"]
    elif name in {"evalarc", "robot-reel"}:
        text = read("pyproject.toml")
        project = re.split(r"(?m)^\[project\]\s*$", text)[1].split("\n[", 1)[0]
        value = re.search(r'(?m)^version = "([^"]+)"$', project)[1]
    elif name == WP:
        value = re.search(r"(?m)^Stable tag:\s*(\S+)", read("readme.txt"))[1]
    elif name == "physical-ai-radar":
        value = re.search(r"(?m)^## (" + VERSION + r")\b", read("CHANGELOG.md"))[1]
    else:
        raise ValueError("Unknown release project")
    if not re.fullmatch(VERSION, value):
        raise ValueError("Release metadata needs a stable semantic version")
    return value


def prepare(root: Path, name: str, base: str, plan: dict, day: str,
            *, delivery: dict | None = None) -> dict:
    """Run before code review/validation; the entire resulting diff gets reviewed."""
    old = version(root, name, base)
    major, minor, _ = map(int, old.split("."))
    new = f"{major}.{minor + 1}.0"
    if name == WP:
        if version(root, name) != new:
            raise ValueError(f"WordPress feature must advance to {new}")
        return {"version": new, "tag": new, "metadata_files": []}
    if version(root, name) != old:
        raise ValueError("The controller, not the feature author, advances the version")
    changes = {}

    def edit(path, transform):
        file = root / path
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Release metadata cannot follow a symlink")
        before = changes.get(path, file.read_text())
        after = transform(before)
        if after != before:
            changes[path] = after

    def json_version(text):
        value = json.loads(text)
        value["version"] = new
        return json.dumps(value, indent=2, ensure_ascii=False) + "\n"

    if name == NPM:
        edit("package.json", json_version)
        for path in [".claude-plugin/plugin.json", "plugin.json"]:
            edit(path, json_version)

        def marketplace(text):
            value = json.loads(text)
            for item in value.get("plugins", []):
                if item.get("name") == name:
                    item["version"] = new
            return json.dumps(value, indent=2, ensure_ascii=False) + "\n"

        def server(text):
            value = json.loads(text)
            value["version"] = new
            for item in value.get("packages", []):
                if item.get("identifier") == name:
                    item["version"] = new
            return json.dumps(value, indent=2, ensure_ascii=False) + "\n"

        edit(".claude-plugin/marketplace.json", marketplace)
        edit("server.json", server)
        pin = re.compile(re.escape(name) + "@" + VERSION + r"(?:-[0-9A-Za-z.-]+)?")
        tarball = re.compile(r"https://github\.com/noteflowai/dsh-skills-anywhere/releases/download/"
                             r"v" + VERSION + r"/dsh-skills-anywhere-" + VERSION + r"\.tgz")
        for path in [".claude-plugin/plugin.json", "mcp.json", "docs/CHECKING.md",
                     "README.md", "README.zh.md"]:
            edit(path, lambda t: tarball.sub(
                f"https://github.com/noteflowai/{name}/releases/download/v{new}/{name}-{new}.tgz",
                pin.sub(name + "@" + new, t)))
        edit("action.yml", lambda t: re.sub(
            r"(?m)^(    default: )" + VERSION + r"$", lambda m: m[1] + new, t))
    elif name in {"evalarc", "robot-reel"}:
        edit("pyproject.toml", lambda t: t.replace(f'version = "{old}"', f'version = "{new}"', 1))
        edit("CITATION.cff", lambda t: re.sub(
            r"(?m)^date-released: .+$", "date-released: " + day,
            re.sub(r"(?m)^version: .+$", "version: " + new, t)))
        def install_links(text):
            text = re.sub(re.escape(name) + r"(\[[A-Za-z0-9_,-]+\])?==" + re.escape(old) + r"\b",
                          lambda m: name + (m[1] or "") + "==" + new, text)
            return text.replace(f"https://pypi.org/project/{name}/{old}/",
                                f"https://pypi.org/project/{name}/{new}/").replace(
                                    f"noteflowai/{name}@v{old}", f"noteflowai/{name}@v{new}")
        for path in ["README.md", "README.zh-CN.md"]:
            edit(path, install_links)
        if name == "evalarc":
            edit("src/evalarc/__init__.py", lambda t: t.replace(
                f'__version__ = "{old}"', f'__version__ = "{new}"'))
            edit("package.json", json_version)
            if (root / "package-lock.json").exists():
                def lock_version(text):
                    value = json.loads(text)
                    value["version"] = new
                    if "" in value.get("packages", {}):
                        value["packages"][""]["version"] = new
                    return json.dumps(value, indent=2) + "\n"
                edit("package-lock.json", lock_version)
        else:
            # Pinned walkthroughs (e.g. first-claim-review.md) retain their original
            # CLI, evidence archive and checksum together. Only current install guides move.
            for path in ["README.md", "README.zh-CN.md", "docs/offline-lab.md",
                         "docs/distribution.md", "docs/lerobot.md"]:
                edit(path, lambda t: install_links(t).replace(
                    f"/releases/download/v{old}/robot_reel-{old}-",
                    f"/releases/download/v{new}/robot_reel-{new}-").replace(
                    f"robot_reel-{old}-", f"robot_reel-{new}-").replace(
                    f"/releases/tag/v{old}", f"/releases/tag/v{new}").replace(
                    f"Version **{old}**", f"Version **{new}**").replace(
                    f"# Robot Reel {old} —", f"# Robot Reel {new} —"))
    title = re.sub(r"\s+", " ", plan["title"].removeprefix("feat:").strip())
    summary = delivery["summary"] if delivery else plan["behavior"]
    behavior = re.sub(r"\s+", " ", summary.strip())
    entry = f"## {new} — {day}\n\n- {title}. {behavior}\n\n"
    edit("CHANGELOG.md", lambda t: re.sub(r"(?m)^## ", lambda m: entry + "## ", t, count=1))
    if "CHANGELOG.md" not in changes:
        raise ValueError("Release needs an existing changelog section")
    for path, text in changes.items():
        (root / path).write_text(text)
    if version(root, name) != new:
        raise ValueError("Release version did not advance consistently")
    return {"version": new, "tag": "v" + new, "metadata_files": sorted(changes)}
