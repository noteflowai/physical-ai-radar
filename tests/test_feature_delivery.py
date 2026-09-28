"""Post-test metadata corrections must not discard verified implementation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.feature_policy import PROJECTS


class DeliveryHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src/feature.py").write_text("def feature(): return 'complete'\n")
        log = self.root / "checks.log"
        log.write_text("242 tests passed; controller execution complete\n")
        self.active = self.root / "active.json"
        self.delivery = {
            "summary": "Validate a recorded choice against current skill content",
            "usage": "skills route apply --request request.json --response response.json",
            "limitations": "Synthetic fixtures only; default threshold is uncalibrated. "
                           "The controller ran 242 tests successfully.",
            "upgrade": "No stored data changes; roll back the package if needed",
        }
        self.task = {
            "id": "delivery-fixture", "repo": "noteflowai/evalarc", "phase": "acceptance-review",
            "attempts": 1, "base": "base", "head": "reviewed-head",
            "plan": {"title": "feat: verified choice", "read_paths": ["src/feature.py"]},
            "checks": [{"log": str(log), "log_sha256": "digest", "exit_code": 0,
                        "candidate_commit": "reviewed-head"}],
            "delivery": {**self.delivery, "limitations": "I have no tools and did not run tests."},
        }

    def test_author_gets_actual_receipts_before_review_and_cache_tracks_evidence(self):
        def author(prompt, *_args, **_kwargs):
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            self.assertEqual(payload["checks"], self.task["checks"])
            self.assertIn("242 tests passed", payload["logs"][0])
            self.assertIn("src/feature.py", payload["context"]["inspected_files"])
            return {"delivery": copy.deepcopy(self.delivery)}
        with patch.object(developer, "ask", side_effect=author) as ask:
            developer.finalize_delivery(self.root, self.task, self.root, self.active)
            developer.finalize_delivery(self.root, self.task, self.root, self.active)
            self.assertEqual(ask.call_count, 1)
            self.task["checks"][0]["log_sha256"] = "new-verification"
            developer.finalize_delivery(self.root, self.task, self.root, self.active)
            self.assertEqual(ask.call_count, 2)
        self.assertEqual(self.task["delivery"], self.delivery)

    def test_metadata_author_cannot_apply_source_edits(self):
        original = (self.root / "src/feature.py").read_bytes()
        with patch.object(developer, "ask", return_value={
            "delivery": self.delivery, "edits": [{"path": "src/feature.py", "new": "stub"}],
        }):
            with self.assertRaises(developer.DeliveryCorrectionRequired):
                developer.finalize_delivery(self.root, self.task, self.root, self.active)
        self.assertEqual((self.root / "src/feature.py").read_bytes(), original)
        self.assertNotIn("delivery_evidence_key", self.task)

    def test_metadata_only_rejection_keeps_commit_and_requires_another_final_review(self):
        with patch.object(developer, "command", return_value="reviewed diff"), \
                patch.object(developer, "ask", side_effect=[
                    {"delivery": self.delivery},
                    {"approved": False, "findings": ["Clarify synthetic scope"],
                     "correction_scope": "delivery"},
                ]) as ask, patch.object(developer, "push") as push:
            with self.assertRaises(developer.DeliveryCorrectionRequired):
                developer.advance(self.root, self.task, PROJECTS["evalarc"], self.root,
                                  self.active, self.root, "evalarc", "2026-09-28")
        self.assertEqual(ask.call_count, 2)
        push.assert_not_called()
        self.assertEqual(self.task["head"], "reviewed-head")
        self.assertEqual(self.task["phase"], "acceptance-review")
        self.assertNotIn("delivery_evidence_key", self.task)

    def test_source_blocker_never_uses_metadata_only_retry(self):
        with patch.object(developer, "command", return_value="reviewed diff"), \
                patch.object(developer, "ask", side_effect=[
                    {"delivery": self.delivery},
                    {"approved": False, "findings": ["Wrong stale-result behavior"],
                     "correction_scope": "implementation"},
                ]), patch.object(developer, "push") as push:
            with self.assertRaises(ValueError) as failure:
                developer.advance(self.root, self.task, PROJECTS["evalarc"], self.root,
                                  self.active, self.root, "evalarc", "2026-09-28")
        self.assertNotIsInstance(failure.exception, developer.DeliveryCorrectionRequired)
        push.assert_not_called()
