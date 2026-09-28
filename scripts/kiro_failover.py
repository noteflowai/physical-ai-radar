#!/usr/bin/env python3
"""Run scheduled Kiro chats with a private backup API key on exhausted credits.

Install this file as ~/.local/share/kiro-failover/bin/kiro-cli, ahead of the
real ~/.local/bin/kiro-cli on the scheduled jobs' PATH. The backup key lives in
~/.config/kiro-failover/backup.key (owner-only directory and file), never here.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

QUOTA = re.compile(
    r"monthly request limit reached|monthly usage limit reached|"
    r"quota exceeded|insufficient credits|spending limit reached|"
    r"usage limit exceeded|overage limit reached",
    re.IGNORECASE,
)
KEY = re.compile(r"ksk_[A-Za-z0-9_-]{20,}")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
active: subprocess.Popen[str] | None = None


def quota_diagnostic(result: subprocess.CompletedProcess[str]) -> bool:
    # Model answers may discuss quotas on stdout. Kiro reports account errors
    # on stderr, including when it exits successfully without an answer.
    return bool(QUOTA.search(ANSI.sub("", result.stderr)))


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted key]")
    return KEY.sub("[redacted key]", text)


def emit(result: subprocess.CompletedProcess[str], *secrets: str) -> None:
    sys.stdout.write(redact(result.stdout, *secrets))
    sys.stderr.write(redact(result.stderr, *secrets))


def status(code: int) -> int:
    return 128 - code if code < 0 else code


def forward(signum: int, _frame: object) -> None:
    if active is not None:
        try:
            os.killpg(active.pid, signum)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    global active
    active = subprocess.Popen(
        command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True,
    )
    try:
        stdout, stderr = active.communicate()
        return subprocess.CompletedProcess(command, active.returncode, stdout, stderr)
    finally:
        active = None


def backup_key(path: Path) -> str:
    directory = path.parent
    folder_info, file_info = directory.lstat(), path.lstat()
    if (not stat.S_ISDIR(folder_info.st_mode) or not stat.S_ISREG(file_info.st_mode)
            or folder_info.st_uid != os.getuid() or file_info.st_uid != os.getuid()
            or stat.S_IMODE(folder_info.st_mode) & 0o077
            or stat.S_IMODE(file_info.st_mode) & 0o077):
        raise ValueError("backup key must be in an owner-only directory and file")
    value = path.read_text(encoding="utf-8").strip()
    if not KEY.fullmatch(value):
        raise ValueError("backup key has an unexpected format")
    return value


def main(args: list[str]) -> int:
    real = os.environ.get("KIRO_REAL_CLI", str(Path.home() / ".local/bin/kiro-cli"))
    command = [real, *args]
    if not (args and args[0] == "chat" and "--no-interactive" in args):
        os.execvpe(real, command, os.environ)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    primary = os.environ.get("KIRO_API_KEY", "")
    first = run(command, os.environ.copy())
    if not quota_diagnostic(first):
        emit(first, primary)
        return status(first.returncode)

    # If Kiro already produced output, it may also have used tools; another
    # attempt could repeat side effects.
    if ANSI.sub("", first.stdout).strip():
        emit(first, primary)
        sys.stderr.write("[kiro-failover] quota after output; refusing duplicate run\n")
        return 1

    path = Path(os.environ.get(
        "KIRO_BACKUP_KEY_FILE", str(Path.home() / ".config/kiro-failover/backup.key"),
    ))
    try:
        second_key = backup_key(path)
        if second_key == primary:
            raise ValueError("backup key matches the active key")
    except (OSError, ValueError) as error:
        emit(first, primary)
        sys.stderr.write(f"[kiro-failover] backup unavailable: {error}\n")
        return 1

    sys.stderr.write("[kiro-failover] primary quota exhausted; retrying with backup key\n")
    second = run(command, {**os.environ, "KIRO_API_KEY": second_key})
    if second.returncode == 0 and not quota_diagnostic(second):
        emit(second, primary, second_key)
        return 0

    # Keep the quota marker for article/course's existing Codex fallback.
    emit(first, primary, second_key)
    emit(second, primary, second_key)
    sys.stderr.write("[kiro-failover] backup request failed\n")
    return status(second.returncode) or 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
