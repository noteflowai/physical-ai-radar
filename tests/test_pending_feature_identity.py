"""The first pending publication receipt already names its feature (01 nightly-evalarc recovery, item 2).

Hermetic: every receipt lives in a temporary directory and every GitHub/deployment call is patched; no
test reads the repository's mutable state (radar/history.json) or the user's ~/.local/state.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import agent_pipeline, develop_repos
from scripts.agent_pipeline import feature_identity, finish

PLAN = {"title": "feat: one feature", "problem": "p", "behavior": "b", "acceptance": ["a"]}
IDENTITY = {"feature": PLAN, "feature_id": "evalarc-feature-1"}
MERGED = {"state": "MERGED", "mergeCommit": {"oid": "m" * 40}, "url": "https://github.com/o/r/pull/3"}


class Interrupted(BaseException):
    """Stands in for a process stopped while PR checks are awaited (not an Exception: no retry write)."""


class PendingFeatureIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name) / "publication.json"

    def saved(self):
        return json.loads(self.output.read_text())

    def test_an_interruption_after_the_pending_write_leaves_the_identity_on_disk(self):
        seen = {}

        def stopped(*args, **kwargs):
            seen.update(self.saved())  # what is durable at the moment the wait begins
            raise Interrupted()

        with patch.object(agent_pipeline, "wait_pr", side_effect=stopped):
            with self.assertRaises(Interrupted):
                finish("o/r", 3, "head", ["CI"], ["https://example.org"], self.output, IDENTITY)
        self.assertEqual(seen["status"], "pending")
        self.assertEqual((seen["feature"], seen["feature_id"]), (PLAN, "evalarc-feature-1"))
        self.assertEqual(self.saved(), seen)  # nothing else was written after the interruption

    def test_a_failed_wait_keeps_the_identity_in_the_retry_record(self):
        with patch.object(agent_pipeline, "wait_pr", side_effect=RuntimeError("CI failed")):
            with self.assertRaises(RuntimeError):
                finish("o/r", 3, "head", ["CI"], ["https://example.org"], self.output, IDENTITY)
        saved = self.saved()
        self.assertEqual(saved["status"], "retry-needed")
        self.assertEqual(feature_identity(saved), IDENTITY)

    def test_completion_writes_the_same_receipt_with_the_identity(self):
        with patch.object(agent_pipeline, "wait_pr", return_value=MERGED), \
                patch.object(agent_pipeline, "verify_deployment", return_value=(["run"], ["page"])):
            receipt = finish("o/r", 3, "head", ["CI"], ["https://example.org"], self.output, IDENTITY)
        self.assertEqual(receipt, {"repo": "o/r", "pr": 3, "head": "head", "merge_commit": "m" * 40,
                                   "url": MERGED["url"], "workflows": ["run"], "public_readback": ["page"],
                                   "status": "published", **IDENTITY})
        self.assertEqual(self.saved(), receipt)

    def test_without_an_identity_every_record_is_as_before(self):
        seen = {}

        def stopped(*args, **kwargs):
            seen.update(self.saved())
            raise RuntimeError("CI failed")

        with patch.object(agent_pipeline, "wait_pr", side_effect=stopped):
            with self.assertRaises(RuntimeError):
                finish("o/r", 3, "head", ["CI"], ["https://example.org"], self.output)
        self.assertEqual(seen, {"repo": "o/r", "pr": 3, "head": "head", "workflow_names": ["CI"],
                                "urls": ["https://example.org"], "status": "pending"})
        self.assertNotIn("feature_id", self.saved())

    def test_a_partial_or_unknown_identity_is_refused_before_any_write(self):
        for identity in ({"feature_id": "x"}, {"feature": PLAN}, {**IDENTITY, "extra": 1}):
            with self.subTest(identity=sorted(identity)):
                with self.assertRaisesRegex(ValueError, "exactly feature and feature_id"):
                    finish("o/r", 3, "head", ["CI"], ["https://example.org"], self.output, identity)
                self.assertFalse(self.output.exists())

    def test_resume_keeps_the_identity_of_a_saved_pending(self):
        directory = self.output.parent
        pending = {"repo": "o/r", "pr": 3, "head": "head", "workflow_names": ["CI"],
                   "urls": ["https://example.org"], "status": "pending", **IDENTITY}
        self.output.write_text(json.dumps(pending))
        argv = ["agent_pipeline.py", "resume", "--directory", str(directory)]
        with patch.object(sys, "argv", argv), \
                patch.object(agent_pipeline, "reconcile_superseded", return_value=None), \
                patch.object(agent_pipeline, "wait_pr", side_effect=RuntimeError("network down")):
            with self.assertRaises(RuntimeError):
                agent_pipeline.main()
        saved = self.saved()
        self.assertEqual(saved["status"], "retry-needed")
        self.assertEqual(feature_identity(saved), IDENTITY)
        self.assertEqual(feature_identity({"repo": "o/r", "status": "pending"}), {})  # legacy receipt


class PublishPassesTheIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.task = {"repo": "noteflowai/physical-ai-radar", "pr": 3, "head": "h" * 40,
                     "reviewed_head": "h" * 40, "base": "b" * 40, "plan": PLAN, "id": "evalarc-feature-1",
                     "checks": [], "changed": []}
        self.config = {"workflows": ["CI"], "urls": ["https://example.org"]}
        pr = {"state": "OPEN", "headRefOid": "h" * 40, "mergeCommit": None, "url": MERGED["url"]}
        for name, value in (("gh", lambda *a, **k: json.dumps(pr)),
                            ("command", lambda *a, **k: json.dumps({"sha": "b" * 40}))):
            patcher = patch.object(develop_repos, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_publish_hands_the_task_identity_to_the_first_pending_write(self):
        calls = []

        def fake_finish(*args):
            calls.append(args)
            return {"status": "published", **args[6]}

        with patch.object(develop_repos, "finish", side_effect=fake_finish):
            result = develop_repos.publish(self.state, self.task, self.config, self.state)
        self.assertEqual(calls[0][6], IDENTITY)
        self.assertEqual(feature_identity(result), IDENTITY)

    def test_a_receipt_naming_another_feature_is_refused(self):
        other = {"status": "published", "feature": PLAN, "feature_id": "someone-else"}
        with patch.object(develop_repos, "finish", return_value=other):
            with self.assertRaisesRegex(RuntimeError, "different feature_id"):
                develop_repos.publish(self.state, self.task, self.config, self.state)
        self.assertFalse((self.state / "publication.json").exists())


if __name__ == "__main__":
    unittest.main()
