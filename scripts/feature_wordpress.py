"""Publish the exact CI-checked WordPress package in one SVN transaction."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time
import urllib.request
import zipfile

try:
    from .agent_pipeline import command, write_json
except ImportError:
    from agent_pipeline import command, write_json

SLUG = "ai-chat-for-amazon-bedrock"
SVN = f"https://plugins.svn.wordpress.org/{SLUG}"


def package_files(package: Path) -> dict[str, bytes]:
    files = {}
    with zipfile.ZipFile(package) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = PurePosixPath(info.filename)
            if (path.parts[0] != SLUG or ".." in path.parts
                    or path.is_absolute() or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Invalid WordPress package path")
            relative = "/".join(path.parts[1:])
            if not relative or relative in files or info.file_size > 10_000_000:
                raise ValueError("Invalid or duplicate WordPress package entry")
            files[relative] = archive.read(info)
    if sum(map(len, files.values())) > 50_000_000:
        raise ValueError("WordPress package exceeds publication budget")
    if not {"readme.txt", SLUG + ".php"}.issubset(files):
        raise ValueError("Package lacks the plugin entry point or readme")
    return files


def version(files: dict[str, bytes]) -> str:
    readme = re.search(rb"(?mi)^Stable tag:\s*(\d+\.\d+\.\d+)\s*$", files["readme.txt"])
    header = re.search(rb"(?m)^\s*\*\s*Version:\s*(\d+\.\d+\.\d+)\s*$",
                       files[SLUG + ".php"])
    if not readme or not header or readme[1] != header[1]:
        raise ValueError("The installable package version is inconsistent")
    return readme[1].decode()


def remote_files(url: str) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="feature-wp-readback-") as tmp:
        root = Path(tmp) / "plugin"
        command(["svn", "export", "--quiet", "--non-interactive", url, str(root)], timeout=180)
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}


def publish(package: Path, state: Path) -> dict:
    files = package_files(package)
    release = version(files)
    tag_url = SVN + "/tags/" + release
    receipt_file = state / "wordpress-publication.json"
    expected = {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}
    info = subprocess.run(["svn", "info", "--non-interactive", tag_url],
                          capture_output=True, text=True, timeout=45)
    if info.returncode == 0:
        if remote_files(tag_url) != files:
            raise ValueError("Published WordPress version already exists with different contents")
    else:
        current = command(["svn", "cat", "--non-interactive", SVN + "/trunk/readme.txt"])
        match = re.search(r"(?mi)^Stable tag:\s*(\d+\.\d+\.\d+)\s*$", current)
        if not match or tuple(map(int, release.split("."))) <= tuple(map(int, match[1].split("."))):
            raise ValueError("A new complete WordPress feature must advance the published version")
        revision = command(["svn", "info", "--non-interactive", "--show-item", "revision",
                            SVN]).strip()
        with tempfile.TemporaryDirectory(prefix="feature-wp-release-") as tmp:
            root = Path(tmp)
            # Both trunk and the immutable version tag receive exactly the checked ZIP.
            # svnmucc commits the whole update atomically; the user's SVN checkout is unused.
            actions = ["rm", SVN + "/trunk", "mkdir", SVN + "/trunk", "mkdir", tag_url]
            dirs = set()
            for name in files:
                dirs.update(str(p) for p in PurePosixPath(name).parents if str(p) != ".")
            for directory in sorted(dirs, key=lambda s: (s.count("/"), s)):
                for target in [SVN + "/trunk", tag_url]:
                    actions += ["mkdir", target + "/" + directory]
            for index, (name, body) in enumerate(sorted(files.items())):
                local = root / str(index)
                local.write_bytes(body)
                for target in [SVN + "/trunk", tag_url]:
                    actions += ["put", str(local), target + "/" + name]
            message = state / "svn-message.txt"
            message.write_text(f"Release {release}: independently reviewed daily feature; CI-checked package\n")
            command(["svnmucc", "--non-interactive", "-r", revision,
                     "-F", str(message), *actions], timeout=300)
        if remote_files(tag_url) != files or remote_files(SVN + "/trunk") != files:
            raise RuntimeError("WordPress SVN readback differs from the checked package")
    record = {"status": "svn-published", "version": release, "tag": tag_url,
              "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "files": expected}
    write_json(receipt_file, record)
    download = f"https://downloads.wordpress.org/plugin/{SLUG}.{release}.zip"
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(download, timeout=45) as response:
                data = response.read(50_000_001)
            if len(data) > 50_000_000:
                raise ValueError("Published package exceeds readback budget")
            with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
                tmp.write(data)
                tmp.flush()
                if package_files(Path(tmp.name)) != files:
                    raise ValueError("WordPress download does not yet match the checked package")
            record.update(status="published", download=download,
                          download_sha256=hashlib.sha256(data).hexdigest())
            write_json(receipt_file, record)
            return record
        except Exception as error:
            record["last_readback_error"] = str(error)
            write_json(receipt_file, record)
            time.sleep(20)
    raise RuntimeError("SVN release committed; public ZIP readback will resume automatically")
