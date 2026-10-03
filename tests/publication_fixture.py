"""Scheduler-only fixture; actual detection is tested in Agent Control."""
from pathlib import Path


def security_home(root):
    home = Path(root) / "isolated-home"
    tool = home / ".local/bin/noteflow-publication-check"
    tool.parent.mkdir(parents=True)
    hooks = home / "empty-hooks"
    hooks.mkdir()
    tool.write_text('#!/bin/sh\n[ "$1" = hooks-path ] || exit 43\n'
                    "printf '%s\\n' '" + str(hooks) + "'\n")
    tool.chmod(0o755)
    return str(home)
