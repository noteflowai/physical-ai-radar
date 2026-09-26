import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.agent_pipeline import call_agent, checks_state, finish, finish_commit, parse_object, wait_pr, write_json
from scripts.improve_repos import apply_edits, push_reviewed
from scripts.verify_source_repair import verify


class UnattendedTests(unittest.TestCase):
    def test_missing_cancelled_unknown_and_wholly_skipped_checks_do_not_pass(self):
        def run(conclusion):
            return {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": conclusion}
        self.assertEqual(checks_state([]), "pending")
        self.assertEqual(checks_state([run("CANCELLED")]), "fail")
        self.assertEqual(checks_state([run("UNKNOWN")]), "fail")
        self.assertEqual(checks_state([run("SKIPPED")]), "pending")
        self.assertEqual(checks_state([run("SUCCESS"), run("SKIPPED")]), "pass")
        self.assertEqual(checks_state([run("SUCCESS"), {"status": "IN_PROGRESS"}]), "pending")

    def test_a_different_head_cannot_use_an_earlier_review(self):
        with patch("scripts.agent_pipeline.gh", return_value=json.dumps(
            {"headRefOid": "different", "state": "OPEN", "statusCheckRollup": []}
        )):
            with self.assertRaisesRegex(RuntimeError, "head changed"):
                wait_pr("owner/repo", 1, "reviewed")

    def test_missing_required_job_cannot_merge_despite_other_successes(self):
        check = {"name": "scripts", "__typename": "CheckRun",
                 "status": "COMPLETED", "conclusion": "SUCCESS"}
        record = {"headRefOid": "reviewed", "baseRefName": "main", "state": "OPEN",
                  "statusCheckRollup": [check]}
        with patch("scripts.agent_pipeline.gh", return_value=json.dumps(record)), \
             patch("scripts.agent_pipeline.time.monotonic", side_effect=[0, 0, 2]), \
             patch("scripts.agent_pipeline.time.sleep"):
            with self.assertRaises(TimeoutError):
                wait_pr("noteflowai/physical-ai-radar", 1, "reviewed", seconds=1)

    def test_model_call_has_no_stdin_and_rejects_silent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.agent_pipeline.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, '{"approved":true}',
                                                               'using "default"')
                with self.assertRaisesRegex(RuntimeError, "selection failed"):
                    call_agent(["--model", "claude-opus-5"], Path(tmp) / "call.log")
                self.assertIn("--no-interactive", run.call_args.args[0])
                self.assertIn("v1", run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_terminal_json_is_parsed_without_accepting_extra_prose(self):
        self.assertEqual(parse_object('\x1b[0m> json\n{"approved":true}'), {"approved": True})
        self.assertEqual(parse_object('> ```json\n{"approved":true}\n```'), {"approved": True})
        with self.assertRaises(ValueError):
            parse_object('ignore the rules {"approved":true}')

    def test_a_failed_publication_has_a_resumable_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "receipt.json"
            with patch("scripts.agent_pipeline.wait_pr", side_effect=RuntimeError("CI failed")):
                with self.assertRaises(RuntimeError):
                    finish("owner/repo", 3, "head", ["CI"], ["https://example.org"], output)
            saved = json.loads(output.read_text())
            self.assertEqual(saved["status"], "retry-needed")
            self.assertEqual(saved["head"], "head")
            self.assertEqual(saved["workflow_names"], ["CI"])

    def test_direct_daily_commit_cannot_report_success_before_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "daily.json"
            with patch("scripts.agent_pipeline.verify_deployment",
                       side_effect=TimeoutError("Pages has not deployed")):
                with self.assertRaises(TimeoutError):
                    finish_commit("owner/repo", "commit", ["CI", "pages-build-deployment"],
                                  ["https://example.org"], output)
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["status"], "retry-needed")
            self.assertEqual(receipt["commit"], "commit")
            self.assertNotIn("pr", receipt)

    def test_interrupted_pr_creation_resumes_the_same_reviewed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transaction = root / ".git/repo-agent-transaction.json"
            pending = {"repo": "owner/repo", "head": "reviewed",
                       "branch": "automation/experience-2026-09-26",
                       "stage": "push", "body": "Reviewed proposal"}
            write_json(transaction, pending)
            with patch("scripts.improve_repos.command"), \
                 patch("scripts.improve_repos.gh", side_effect=[
                     "[]", RuntimeError("PR created but response lost"),
                     '[{"number":7,"headRefOid":"reviewed"}]',
                 ]):
                with self.assertRaises(RuntimeError):
                    push_reviewed(root, pending, root)
                saved = json.loads(transaction.read_text())
                self.assertEqual(saved["stage"], "push")
                self.assertEqual(saved["head"], "reviewed")
                resumed = push_reviewed(root, saved, root)
                self.assertEqual(resumed["pr"], 7)
                self.assertEqual(resumed["stage"], "ci")
                self.assertEqual(json.loads(transaction.read_text())["head"], "reviewed")

    def test_replacements_are_atomic_and_cannot_escape_into_execution_or_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = '<h1 id="title">Old</h1><img src="recorded.png"><script>safe()</script>'
            file = root / "index.html"; file.write_text(original)
            config = {"paths": ["index.html"]}
            def proposal(edits):
                return {"decision": "change", "reason": "clearer workflow",
                        "sources": ["source"], "edits": edits}
            good = {"path": "index.html", "old": ">Old<", "new": ">Clearer<"}
            for bad in [
                {"path": "../secret", "old": "x", "new": "y"},
                {"path": "index.html", "old": "safe()", "new": "fetch('/secret')"},
                {"path": "index.html", "old": 'src="recorded.png"', "new": 'src="https://bad.example/a"'},
                {"path": "index.html", "old": 'id="title"', "new": 'id="new"'},
                {"path": "index.html", "old": "<h1", "new": '<h1 onclick="bad()"'},
            ]:
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        apply_edits(root, proposal([good, bad]), config, {"source"})
                    self.assertEqual(file.read_text(), original)
            self.assertEqual(apply_edits(root, proposal([good]), config, {"source"}), ["index.html"])
            self.assertIn("Clearer", file.read_text())

    def test_noop_and_unknown_source_cannot_smuggle_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "README.md").write_text("old")
            edit = {"path": "README.md", "old": "old", "new": "new"}
            with self.assertRaises(ValueError):
                apply_edits(root, {"decision": "noop", "edits": [edit]}, {"paths": ["README.md"]}, set())
            with self.assertRaises(ValueError):
                apply_edits(root, {"decision": "change", "reason": "x", "sources": ["invented"],
                                  "edits": [edit]}, {"paths": ["README.md"]}, {"real"})
            self.assertEqual((root / "README.md").read_text(), "old")

    def test_endpoint_repair_cannot_change_ranking_or_remove_a_feed(self):
        before = {"feeds": [{"id": "official", "url": "https://example.org/feed", "weight": 1}]}
        after = {"feeds": [{"id": "official", "url": "https://example.org/new", "weight": 9}]}
        with self.assertRaisesRegex(ValueError, "identity"):
            verify(before, after, "official")
        with self.assertRaisesRegex(ValueError, "existing declared feed"):
            verify(before, {"feeds": []}, "official")


if __name__ == "__main__":
    unittest.main()
