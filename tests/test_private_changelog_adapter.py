import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch

from scripts.feature_versions import changelog


class PrivateChangelogAdapterTests(unittest.TestCase):
    def receipt(self):
        before = "## Unreleased\n\n- Existing change.\n\n## 0.17.4\n"
        after = ("## Unreleased\n\n## 0.17.5 — 2026-10-05\n\n"
                 "- New change.\n\n- Existing change.\n\n## 0.17.4\n")
        return before, after, {
            "schema_version": 1, "changelog": after,
            "input_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "output_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "publication_approved": False, "model_calls": 0,
        }

    def test_native_adapter_uses_pinned_wrapper_with_a_credential_free_environment(self):
        before, after, receipt = self.receipt()
        with patch("scripts.feature_versions.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, json.dumps(receipt), "")) as run:
            self.assertEqual(changelog(before, "0.17.5", "2026-10-05", "- New change."), after)
        args, kwargs = run.call_args
        self.assertTrue(args[0][0].endswith("/.local/bin/noteflow-release-changelog"))
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "LC_ALL"})
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(json.loads(kwargs["input"]),
                         {"changelog": before, "version": "0.17.5",
                          "day": "2026-10-05", "entry": "- New change."})

    def test_mutated_or_authoritative_receipts_refuse_before_native_writes(self):
        before, _, receipt = self.receipt()
        for change in [{"input_sha256": "0" * 64}, {"output_sha256": "0" * 64},
                       {"publication_approved": True}, {"model_calls": 1},
                       {"schema_version": 2}, {"changelog": None},
                       {"unexpected": True}]:
            with self.subTest(change=change), patch(
                    "scripts.feature_versions.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps({**receipt, **change}), "")):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    changelog(before, "0.17.5", "2026-10-05", "- New change.")

    def test_missing_failed_or_timed_out_helper_has_no_fallback(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired([], 10)]:
            with patch("scripts.feature_versions.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    changelog("## 0.17.4\n", "0.17.5", "2026-10-05", "- New.")
        for result in [subprocess.CompletedProcess([], 1, '{"error":"refused"}', ""),
                       subprocess.CompletedProcess([], 0, "{", "")]:
            with patch("scripts.feature_versions.subprocess.run", return_value=result):
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    changelog("## 0.17.4\n", "0.17.5", "2026-10-05", "- New.")


if __name__ == "__main__":
    unittest.main()
