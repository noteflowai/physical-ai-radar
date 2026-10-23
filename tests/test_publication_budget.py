"""Publication waits end inside the worker budget and leave an explicit receipt."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import agent_pipeline as ap
from scripts import develop_repos

COMMIT = "01cd56593bbff244f3ec594e6f97807033e8d581"


class Clock:
    """Fake monotonic + wall clock + sleep: sleeping advances both."""

    def __init__(self, wall=1_000_000.0):
        self.now, self.wall0, self.sleeps = 0.0, wall, []

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall0 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def runs(*items):
    return json.dumps([{"databaseId": i, "headSha": COMMIT, "status": s, "conclusion": c,
                        "workflowName": w, "url": "u"} for i, (w, s, c) in enumerate(items, 1)])


class Budget(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patches = [patch.object(ap.time, "monotonic", self.clock.monotonic),
                        patch.object(ap.time, "time", self.clock.time),
                        patch.object(ap.time, "sleep", self.clock.sleep)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        os.environ.pop(ap.WORKER_DEADLINE_ENV, None)
        os.environ.pop(ap.RECEIPT_RESERVE_ENV, None)
        self.addCleanup(os.environ.pop, ap.WORKER_DEADLINE_ENV, None)
        self.addCleanup(os.environ.pop, ap.RECEIPT_RESERVE_ENV, None)

    def worker(self, seconds=develop_repos.WORKER_SECONDS):
        os.environ[ap.WORKER_DEADLINE_ENV] = f"{self.clock.time() + seconds:.3f}"

    def test_wait_ends_before_the_worker_budget_minus_the_receipt_reserve(self):
        self.worker()
        self.assertLess(ap.wait_budget(2400), develop_repos.WORKER_SECONDS)
        self.assertEqual(ap.wait_budget(2400), develop_repos.WORKER_SECONDS - ap.DEFAULT_RECEIPT_RESERVE)
        os.environ[ap.RECEIPT_RESERVE_ENV] = "300"
        self.assertEqual(ap.wait_budget(2400), develop_repos.WORKER_SECONDS - 300)
        self.assertEqual(ap.wait_budget(60), 60)                       # its own limit if smaller
        self.clock.now = 5000                                          # budget already spent
        self.assertEqual(ap.wait_budget(2400), 0.0)
        os.environ[ap.RECEIPT_RESERVE_ENV] = "5"
        with self.assertRaises(ValueError):
            ap.wait_budget(2400)
        os.environ.pop(ap.WORKER_DEADLINE_ENV)
        os.environ.pop(ap.RECEIPT_RESERVE_ENV)
        self.assertEqual(ap.wait_budget(2400), 2400)                   # unchanged outside a worker

    def test_missing_merge_commit_run_is_reported_before_the_outer_kill(self):
        self.worker()
        with patch.object(ap, "gh", return_value="[]"), \
             patch.object(ap, "progress") as log:
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.wait_publication("noteflowai/evalarc", COMMIT, ["CI"])
        error = raised.exception
        self.assertEqual(error.code, "merge-commit-workflow-missing")
        self.assertEqual(error.commit, COMMIT)
        self.assertEqual(error.last_observed, {"CI": None})
        self.assertLessEqual(self.clock.now, develop_repos.WORKER_SECONDS - ap.DEFAULT_RECEIPT_RESERVE)
        self.assertGreaterEqual(error.waited_seconds, develop_repos.WORKER_SECONDS - ap.DEFAULT_RECEIPT_RESERVE)
        # Bounded progress: about one line per minute, never one per poll.
        lines = log.call_count
        self.assertGreaterEqual(lines, (develop_repos.WORKER_SECONDS - 120) // ap.PROGRESS_EVERY - 1)
        self.assertLessEqual(lines, (develop_repos.WORKER_SECONDS - 120) // ap.PROGRESS_EVERY + 2)
        self.assertIn("CI=missing", log.call_args_list[0].args[0])

    def test_a_running_workflow_times_out_with_its_last_state(self):
        self.worker(400)
        with patch.object(ap, "gh", return_value=runs(("CI", "in_progress", None))), \
             patch.object(ap, "progress"):
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.wait_publication("o/r", COMMIT, ["CI"])
        self.assertEqual(raised.exception.code, "workflow-running-timeout")
        self.assertEqual(raised.exception.last_observed, {"CI": {"run": 1, "status": "in_progress",
                                                                  "conclusion": None}})
        self.assertLessEqual(self.clock.now, 400 - ap.DEFAULT_RECEIPT_RESERVE)

    def test_one_missing_of_two_workflows_is_reported_as_missing(self):
        self.worker(400)
        with patch.object(ap, "gh", return_value=runs(("CI", "completed", "success"))), \
             patch.object(ap, "progress"):
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.wait_publication("o/r", COMMIT, ["CI", "pages-build-deployment"])
        self.assertEqual(raised.exception.code, "merge-commit-workflow-missing")
        self.assertIn("pages-build-deployment", str(raised.exception))

    def test_an_exhausted_budget_still_observes_once_and_succeeds_if_done(self):
        self.worker(60)                       # 60 - 120 reserve: no waiting at all
        with patch.object(ap, "gh", return_value=runs(("CI", "completed", "success"))) as gh:
            self.assertEqual(ap.wait_publication("o/r", COMMIT, ["CI"])[0]["conclusion"], "success")
        self.assertEqual(gh.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_stale_manifest_after_successful_workflows(self):
        self.worker(400)
        with patch.object(ap, "wait_publication", return_value=[{"databaseId": 7, "status": "completed",
                                                                   "conclusion": "success"}]), \
             patch.object(ap, "read_public", return_value={}), \
             patch.object(ap, "public_commit", side_effect=ValueError("Public provenance names old")):
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.verify_deployment("noteflowai/evalarc", COMMIT, ["CI"], ["https://x/manifest.json"])
        error = raised.exception
        self.assertEqual(error.code, "manifest-not-updated")
        self.assertEqual(error.last_observed["url"], "https://x/manifest.json")
        self.assertIn("old", error.last_observed["error"])
        self.assertLessEqual(self.clock.now, 400 - ap.DEFAULT_RECEIPT_RESERVE)

    def test_the_receipt_keeps_identity_and_records_the_reason(self):
        self.worker()
        merged = {"headRefOid": "reviewed", "baseRefName": "main", "state": "MERGED",
                  "statusCheckRollup": [], "mergeCommit": {"oid": COMMIT}, "url": "https://pr/93"}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "publication.json"
            with patch.object(ap, "wait_pr", return_value=merged), \
                 patch.object(ap, "gh", return_value="[]"), patch.object(ap, "progress"):
                with self.assertRaises(ap.PublicationWaitError):
                    ap.finish("noteflowai/evalarc", 93, "reviewed", ["CI"], ["https://x/m.json"], output)
            saved = json.loads(output.read_text())
        self.assertEqual({k: saved[k] for k in ("repo", "pr", "head", "workflow_names", "urls", "status")},
                         {"repo": "noteflowai/evalarc", "pr": 93, "head": "reviewed",
                          "workflow_names": ["CI"], "urls": ["https://x/m.json"], "status": "retry-needed"})
        self.assertEqual(saved["error_code"], "merge-commit-workflow-missing")
        self.assertEqual(saved["merge_commit"], COMMIT)
        self.assertEqual(saved["last_observed"], {"CI": None})
        self.assertGreater(saved["waited_seconds"], 0)
        # Never merged again, never reset: the PR was already merged, so no merge call.
        self.assertNotIn("abandoned", saved["status"])

    def test_direct_commit_receipt_records_the_same_fields(self):
        self.worker(400)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "daily.json"
            with patch.object(ap, "gh", return_value=runs(("CI", "queued", None))), \
                 patch.object(ap, "progress"):
                with self.assertRaises(ap.PublicationWaitError):
                    ap.finish_commit("o/r", COMMIT, ["CI"], ["https://x"], output)
            saved = json.loads(output.read_text())
        self.assertEqual((saved["status"], saved["commit"], saved["error_code"]),
                         ("retry-needed", COMMIT, "workflow-running-timeout"))

    def test_the_controller_gives_its_worker_the_deadline(self):
        source = Path(develop_repos.__file__).read_text()
        self.assertIn("WORKER_DEADLINE_ENV: f\"{time.time() + WORKER_SECONDS:.3f}\"", source)
        self.assertIn('receipt.parent / "worker.log", WORKER_SECONDS)', source)


if __name__ == "__main__":
    unittest.main()
