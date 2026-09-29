#!/usr/bin/env python3
"""Run scheduled Kiro chats with two private backup keys on exhausted credits.

Install this file as ~/.local/share/kiro-failover/bin/kiro-cli, ahead of the
real ~/.local/bin/kiro-cli on the scheduled jobs' PATH. The ordered backups live
in ~/.config/kiro-failover/{backup,backup2}.key (owner-only), never here.
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
    r"monthly (?:request|usage) limit (?:has been )?reached|"
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


def emit_reconcile(result: subprocess.CompletedProcess[str], *secrets: str) -> None:
    # Legacy domain callers match a literal quota phrase to invoke Codex.
    # Preserve partial output but prevent that phrase from authorizing replay.
    for text, target in [(result.stdout, sys.stdout), (result.stderr, sys.stderr)]:
        target.write(QUOTA.sub("[provider capacity error; reconcile first]", redact(text, *secrets)))
    sys.stderr.write("[kiro-failover] RECONCILE_REQUIRED: output observed; refusing duplicate run\n")


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
        emit_reconcile(first, primary)
        return 1

    secrets = [primary]
    current = first
    for name, variable, filename in [
        ("backup", "KIRO_BACKUP_KEY_FILE", "backup.key"),
        ("backup2", "KIRO_BACKUP2_KEY_FILE", "backup2.key"),
    ]:
        path = Path(os.environ.get(variable, str(Path.home() / ".config/kiro-failover" / filename)))
        try:
            key = backup_key(path)
            if key in secrets:
                raise ValueError("credential already attempted")
        except (OSError, ValueError) as error:
            sys.stderr.write(f"[kiro-failover] {name} unavailable: {error}\n")
            continue

        secrets.append(key)
        sys.stderr.write(f"[kiro-failover] trying {name} credential\n")
        current = run(command, {**os.environ, "KIRO_API_KEY": key})
        quota = quota_diagnostic(current)
        if not quota and current.returncode == 0:
            emit(current, *secrets)
            return 0
        if ANSI.sub("", current.stdout).strip():
            # Applies to EVERY attempt, not just the primary. Do not expose a
            # previous quota diagnostic that could trigger the caller's replay.
            emit_reconcile(current, *secrets)
            return status(current.returncode) or 1
        if not quota:
            # Preserve the legacy empty-output failure signal for the existing
            # domain fallback. A non-quota failure does not rotate more keys.
            emit(first, *secrets)
            emit(current, *secrets)
            sys.stderr.write("[kiro-failover] Monthly request limit reached; backup request failed before output\n")
            return status(current.returncode) or 1

    # At most three distinct credentials. Keep the final provider quota marker
    # for article/course's existing fallback after every attempt had no output.
    emit(current, *secrets)
    sys.stderr.write("[kiro-failover] Monthly request limit reached; credential chain exhausted before output\n")
    return status(current.returncode) or 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
