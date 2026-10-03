import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import feature_release, security_gate


class PublicationSecurityTests(unittest.TestCase):
    def test_security_failure_stops_release_creation_and_assets(self):
        source = {"feature": {"title": "safe"}, "feature_id": "test"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(feature_release, "release_notes", return_value="safe"), \
                patch.object(feature_release, "optional_api", return_value=None), \
                patch.object(feature_release, "require_safe_publication",
                             side_effect=RuntimeError("publication-security-blocked")), \
                patch.object(feature_release, "gh") as gh, \
                patch.object(feature_release.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                feature_release.managed_release("owner/repo", "v1", source, [], Path(directory), "1")
            gh.assert_not_called()
            run.assert_not_called()

    def test_gateway_failure_and_wrong_source_cannot_become_approval(self):
        good = {"policy": "aws-security-first-v1", "state": "passed",
                "source_sha256": hashlib.sha256(b"safe").hexdigest(),
                "publication_approved": False, "cloud_write_authorized": False,
                "findings": []}
        for code, report in ((43, good), (0, dict(good, source_sha256="other")),
                             (0, dict(good, findings=[{"code": "blocked"}]))):
            with patch.object(security_gate.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], code, json.dumps(report).encode(), b"")):
                with self.assertRaisesRegex(RuntimeError, "^publication-security-blocked-or-unavailable$"):
                    security_gate.require_safe_publication("safe")
        with patch.object(security_gate.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(RuntimeError):
                security_gate.require_safe_publication("safe")
