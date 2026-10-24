"""Publication waits end inside the worker budget and leave an explicit receipt."""
import json
import os
from pathlib import Path
import subprocess
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
        seen = {}

        def executor(argv, cwd, env, log, timeout):
            seen.update(env=env, timeout=timeout, log=log)
            return 0

        class Lock:
            def fileno(self):
                return 9

        self.assertEqual(develop_repos.run_worker(["w"], Lock(), Path("worker.log"), executor), 0)
        deadline = float(seen["env"][ap.WORKER_DEADLINE_ENV])
        self.assertLess(abs(deadline - (self.clock.time() + develop_repos.WORKER_SECONDS)), 1)
        self.assertEqual(seen["timeout"], develop_repos.WORKER_SECONDS)
        self.assertEqual((seen["env"]["FEATURE_LOCK_HELD"], seen["env"]["FEATURE_LOCK_FD"]), ("1", "9"))
        # The daily controller starts workers only through run_worker.
        source = Path(develop_repos.__file__).read_text()
        self.assertEqual(source.count("execute_stage)("), 1)
        self.assertEqual(source.count("execute_stage("), 0)
        self.assertIn('run_worker(argv, lock, receipt.parent / "worker.log")', source)

    def test_the_receipt_reserve_has_an_upper_bound(self):
        self.worker()
        os.environ[ap.RECEIPT_RESERVE_ENV] = str(ap.MAX_RECEIPT_RESERVE + 1)
        with self.assertRaises(ValueError):
            ap.wait_budget(60)
        self.assertLess(ap.MAX_RECEIPT_RESERVE, develop_repos.WORKER_SECONDS)

    # --- every external call ends by the deadline (05 R159-01) -----------------

    def slow_calls(self, answer_url=None):
        """subprocess.run / urlopen that use their full timeout, then time out."""
        clock = self.clock

        def run(args, timeout=None, **kwargs):
            clock.now += timeout
            raise subprocess.TimeoutExpired(args, timeout)

        class Response:
            status, headers = 200, {"Content-Type": "application/json"}

            def __init__(self, body):
                self.body = body

            def read(self, limit):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            if answer_url and request.full_url == answer_url:
                return Response(b"{}")
            clock.now += timeout
            raise TimeoutError("timed out")

        return [patch.object(ap.subprocess, "run", run),
                patch.object(ap.urllib.request, "urlopen", urlopen)]

    def test_a_slow_readback_call_cannot_outlive_the_worker(self):
        # Workflows passed with 1 s of wait budget left; Pages API then hangs.
        self.worker(ap.DEFAULT_RECEIPT_RESERVE + 1)
        url = "https://noteflowai.github.io/physical-ai-radar/"
        merged = {"headRefOid": "h", "baseRefName": "main", "state": "MERGED",
                  "statusCheckRollup": [], "mergeCommit": {"oid": COMMIT}, "url": "https://pr/1"}
        success = [{"databaseId": 7, "status": "completed", "conclusion": "success",
                    "workflowName": "pages"}]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "publication.json"
            calls = self.slow_calls(answer_url=url)
            for p in calls:
                p.start()
            try:
                with patch.object(ap, "wait_pr", return_value=merged), \
                     patch.object(ap, "wait_publication", return_value=success):
                    with self.assertRaises(ap.PublicationWaitError) as raised:
                        ap.finish("noteflowai/physical-ai-radar", 1, "h", ["pages"], [url], output)
            finally:
                for p in calls:
                    p.stop()
            saved = json.loads(output.read_text())
        self.assertEqual(raised.exception.code, "public-readback-unreachable")
        self.assertEqual((saved["status"], saved["error_code"]), ("retry-needed", "public-readback-unreachable"))
        # Reported by the deadline minus the reserve plus at most one floored call, which
        # still leaves most of the reserve for the receipt.
        self.assertLessEqual(self.clock.now, 1 + ap.MIN_CALL_SECONDS)
        self.assertLess(self.clock.now, ap.DEFAULT_RECEIPT_RESERVE + 1 - 20)

    def test_a_hanging_public_page_cannot_outlive_the_worker(self):
        self.worker(ap.DEFAULT_RECEIPT_RESERVE + 1)
        calls = self.slow_calls()
        for p in calls:
            p.start()
        try:
            with patch.object(ap, "wait_publication", return_value=[]):
                with self.assertRaises(ap.PublicationWaitError) as raised:
                    ap.verify_deployment("noteflowai/evalarc", COMMIT, ["CI"], ["https://x/"])
        finally:
            for p in calls:
                p.stop()
        self.assertEqual(raised.exception.code, "public-readback-unreachable")
        self.assertLessEqual(self.clock.now, 1 + ap.MIN_CALL_SECONDS)

    def test_a_hanging_run_list_at_the_edge_still_reports_in_time(self):
        self.worker(ap.DEFAULT_RECEIPT_RESERVE + 50)
        replies = iter([runs(("CI", "in_progress", None))])
        real_run = ap.subprocess.run

        def run(args, timeout=None, **kwargs):
            try:
                body = next(replies)
            except StopIteration:
                self.clock.now += timeout
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

        with patch.object(ap.subprocess, "run", run), patch.object(ap, "progress"):
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.wait_publication("o/r", COMMIT, ["CI"])
        self.assertIs(ap.subprocess.run, real_run)
        self.assertEqual(raised.exception.code, "workflow-running-timeout")
        self.assertEqual(raised.exception.last_observed["CI"]["status"], "in_progress")
        self.assertLessEqual(self.clock.now, 51)

    def test_no_readable_run_list_at_all_is_github_unreachable(self):
        self.worker(60)                                   # no budget: one capped observation
        calls = self.slow_calls()
        for p in calls:
            p.start()
        try:
            with patch.object(ap, "progress"):
                with self.assertRaises(ap.PublicationWaitError) as raised:
                    ap.wait_publication("o/r", COMMIT, ["CI"])
        finally:
            for p in calls:
                p.stop()
        self.assertEqual(raised.exception.code, "github-unreachable")
        self.assertLessEqual(self.clock.now, ap.MIN_CALL_SECONDS)

    def test_with_no_budget_left_a_normal_gh_reply_is_still_observed(self):
        # 05 N159-r1-01: gh start-up plus one API round trip takes seconds; the last
        # observation must not be cut to 1 s and misreported as github-unreachable.
        self.worker(60)                                   # budget exhausted
        seen = []

        def run(args, timeout=None, **kwargs):
            seen.append(timeout)
            if timeout < 5:
                self.clock.now += timeout
                raise subprocess.TimeoutExpired(args, timeout)
            self.clock.now += 5                           # a slow but normal gh call
            return subprocess.CompletedProcess(args, 0, stdout=runs(("CI", "completed", "success")), stderr="")

        with patch.object(ap.subprocess, "run", run), patch.object(ap, "progress"):
            self.assertEqual(ap.wait_publication("o/r", COMMIT, ["CI"])[0]["conclusion"], "success")
        self.assertEqual(seen, [ap.MIN_CALL_SECONDS])

    def test_the_call_floor_ends_before_the_worker_deadline(self):
        self.assertLess(ap.MIN_CALL_SECONDS, ap.MIN_RECEIPT_RESERVE)
        self.assertEqual(ap.RECEIPT_MARGIN, ap.MIN_RECEIPT_RESERVE - ap.MIN_CALL_SECONDS)
        self.assertGreaterEqual(ap.RECEIPT_MARGIN, 20)
        self.worker(ap.MIN_RECEIPT_RESERVE)                # nothing left after the reserve
        os.environ[ap.RECEIPT_RESERVE_ENV] = str(ap.MIN_RECEIPT_RESERVE)
        self.assertEqual(ap.call_timeout(120), ap.MIN_CALL_SECONDS)
        self.assertEqual(ap.call_timeout(5), 5)           # never longer than the call's own limit
        self.clock.now = 4                                 # 4 s past the edge: only 6 s left
        self.assertEqual(ap.call_timeout(120), ap.MIN_CALL_SECONDS - 4)
        self.clock.now = ap.MIN_CALL_SECONDS               # at the hard edge: nothing starts
        with self.assertRaises(subprocess.TimeoutExpired):
            ap.call_timeout(120)

    def test_chained_slow_calls_past_the_edge_share_one_floor(self):
        # 05 N160-01: robot-reel reads 2 URLs x 3 calls after the workflows; every call is
        # slow but succeeds. At the smallest reserve the receipt must still be written.
        os.environ[ap.RECEIPT_RESERVE_ENV] = str(ap.MIN_RECEIPT_RESERVE)
        self.worker(ap.MIN_RECEIPT_RESERVE)                # the wait budget is already spent
        urls = ["https://noteflowai.github.io/robot-reel/", "https://glayguo-robot-reel.hf.space/"]
        started = []

        class Response:
            status, headers = 200, {"Content-Type": "text/plain"}

            def read(self, limit):
                return b"same bytes"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            started.append(self.clock.now)
            self.clock.now += timeout * 0.99               # slow, but inside its timeout
            return Response()

        merged = {"headRefOid": "h", "baseRefName": "main", "state": "MERGED",
                  "statusCheckRollup": [], "mergeCommit": {"oid": COMMIT}, "url": "https://pr/1"}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "publication.json"
            with patch.object(ap.urllib.request, "urlopen", urlopen), \
                 patch.object(ap, "wait_pr", return_value=merged), \
                 patch.object(ap, "wait_publication", return_value=[]):
                try:
                    ap.finish("noteflowai/robot-reel", 1, "h", ["CI"], urls, output)
                except ap.PublicationWaitError:
                    pass
            saved = json.loads(output.read_text())
        self.assertLessEqual(self.clock.now, ap.MIN_CALL_SECONDS)   # all calls together
        self.assertTrue(all(t < ap.MIN_CALL_SECONDS for t in started))
        self.assertLess(self.clock.now, ap.MIN_RECEIPT_RESERVE)      # the receipt still has time
        self.assertIn(saved["status"], {"published", "retry-needed"})

    def test_chained_calls_with_the_default_reserve_leave_the_margin(self):
        # Default reserve 120: floored calls may continue past the edge, never into the
        # last RECEIPT_MARGIN seconds before the outer kill.
        self.worker(ap.DEFAULT_RECEIPT_RESERVE)            # at the edge
        done = 0
        for _ in range(40):                                # far more calls than any repo makes
            try:
                self.clock.now += ap.call_timeout(120)     # each uses its whole timeout
                done += 1
            except subprocess.TimeoutExpired:
                break
        self.assertLessEqual(self.clock.now, ap.DEFAULT_RECEIPT_RESERVE - ap.RECEIPT_MARGIN)
        self.assertEqual(done, (ap.DEFAULT_RECEIPT_RESERVE - ap.RECEIPT_MARGIN) // ap.MIN_CALL_SECONDS)

    # --- wait_pr and readback are capped (05 R159-02) ---------------------------

    def test_wait_pr_ends_inside_the_worker_budget_and_the_receipt_is_written(self):
        self.worker(400)
        pending = json.dumps({"headRefOid": "h", "baseRefName": "main", "isCrossRepository": False,
                              "state": "OPEN", "mergeCommit": None, "url": "u",
                              "statusCheckRollup": [{"name": "CI", "status": "IN_PROGRESS",
                                                     "conclusion": ""}]})
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "publication.json"
            with patch.object(ap, "gh", return_value=pending):
                with self.assertRaises(TimeoutError):
                    ap.finish("o/r", 5, "h", ["CI"], ["https://x"], output)
            saved = json.loads(output.read_text())
        self.assertEqual(saved["status"], "retry-needed")
        self.assertIn("CI has not passed", saved["error"])
        self.assertLessEqual(self.clock.now, 400 - ap.DEFAULT_RECEIPT_RESERVE)

    def test_wait_pr_with_no_budget_still_observes_once(self):
        self.worker(60)
        passed = json.dumps({"headRefOid": "h", "baseRefName": "main", "isCrossRepository": False,
                             "state": "MERGED", "mergeCommit": {"oid": COMMIT}, "url": "u",
                             "statusCheckRollup": [{"name": "CI", "status": "COMPLETED",
                                                    "conclusion": "SUCCESS"}]})
        with patch.object(ap, "gh", return_value=passed) as gh:
            self.assertEqual(ap.wait_pr("o/r", 5, "h")["state"], "MERGED")
        self.assertEqual(gh.call_count, 1)

    def test_readback_is_capped_by_the_remaining_worker_budget(self):
        self.worker(ap.DEFAULT_RECEIPT_RESERVE + 30)       # 30 s left, below the 180 s default
        with patch.object(ap, "wait_publication", return_value=[]), \
             patch.object(ap, "read_public", return_value={}), \
             patch.object(ap, "public_commit", side_effect=ValueError("old provenance")):
            with self.assertRaises(ap.PublicationWaitError) as raised:
                ap.verify_deployment("noteflowai/evalarc", COMMIT, ["CI"], ["https://x/manifest.json"])
        self.assertEqual(raised.exception.code, "manifest-not-updated")
        self.assertLessEqual(self.clock.now, 30)


if __name__ == "__main__":
    unittest.main()
