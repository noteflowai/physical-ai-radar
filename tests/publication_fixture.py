"""Isolated domain fixtures; operator policy has its own integration tests."""
import os
from pathlib import Path
from unittest.mock import patch


def isolate_private_recovery(test_case):
    """Keep synthetic domain states independent of the operator's live recovery."""
    environment = patch.dict(os.environ, {"NOTEFLOW_FEATURE_RECOVERY": ""})
    environment.start()
    test_case.addCleanup(environment.stop)


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
