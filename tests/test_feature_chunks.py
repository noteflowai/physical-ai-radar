"""Long author responses must resume without applying or trusting partial features."""
import copy
import html
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.develop_repos import parse_author_chunk, parse_feature_object, requested_context
from scripts.feature_chunks import add_chunk, draft_for, inspect_draft, manifest
from scripts.feature_policy import PROJECTS


class ChunkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src/cli.py").write_text("old anchor\n")
        self.config = PROJECTS["evalarc"]
        self.task = {}
        self.plan = {"title": "One complete feature", "test_path": "tests/test_new.py"}
        self.draft = draft_for(self.task, "base", self.plan)
        self.edit = {"path": "src/new.py", "old": None, "new": "def feature():\n"}

    def test_truncated_new_test_recovers_only_complete_preceding_objects(self):
        good = json.dumps(self.edit)
        text = 'CLI narration\n> {"edits":[' + good + ',{"path":"tests/test_new.py","old":null,"new":"ass'
        with self.assertRaises(json.JSONDecodeError):
            parse_feature_object(text)
        value = parse_author_chunk(text)
        self.assertEqual(value["edits"], [self.edit])
        self.assertTrue(value["continue"])
        self.assertTrue(value["transport_recovered"])
        self.assertFalse((self.root / "src/new.py").exists())

    def test_ambiguous_complete_json_and_missing_first_edit_are_not_salvaged(self):
        for text in ['{"edits":[]}\n{"unfinished":', '{"edits":[',
                     '{"need_files":["src/cli.py"], "broken":',
                     '{"edits":[]}\n{"edits":[]}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_author_chunk(text)

    def test_observed_v1_unicode_transport_preserves_entities_operators_and_templates(self):
        # Exact stdout shape observed from Kiro v1 after its terminal renderer.
        text = (r'> json' + '\n' +
                r'{"literal":"\u0026lt;script\u0026gt;alert(1)\u0026lt;/script\u0026gt;",'
                r'"amp":"\u0026amp;","operator":"left \u0026\u0026 right",'
                r'"template":"\u0060hello\u0060"}')
        self.assertEqual(parse_feature_object(html.unescape(text)), {
            "literal": "&lt;script&gt;alert(1)&lt;/script&gt;", "amp": "&amp;",
            "operator": "left && right", "template": "`hello`",
        })

    def test_large_new_file_can_resume_after_restart_and_virtual_read_is_inert(self):
        draft, proposal = add_chunk(self.root, self.draft, {
            "edits": [{**self.edit, "file_complete": False}], "continue": True,
        }, self.config)
        self.assertIsNone(proposal)
        self.task["draft"] = draft
        restarted = json.loads(json.dumps(self.task))
        resumed = draft_for(restarted, "base", self.plan)
        key, text = inspect_draft(self.root, resumed, "src/new.py", self.config, requested_context)
        self.assertEqual((key, text), ("src/new.py", self.edit["new"]))
        self.assertFalse((self.root / "src/new.py").exists())
        draft, proposal = add_chunk(self.root, resumed, {
            "edits": [{"path": "src/new.py", "append": True, "new": "    return 42\n"}],
            "delivery": {"summary": "A complete function"},
        }, self.config)
        self.assertEqual(proposal["edits"][0]["new"], "def feature():\n    return 42\n")
        self.assertEqual(draft["open_files"], [])
        self.assertEqual(manifest(self.root, draft, self.config)[0]["file_complete"], True)

    def test_incomplete_file_cannot_finalize_or_append_to_existing_file(self):
        draft, _ = add_chunk(self.root, self.draft, {
            "edits": [{**self.edit, "file_complete": False}], "continue": True,
        }, self.config)
        original = copy.deepcopy(draft)
        for response in [
            {"edits": [], "delivery": {}},
            {"edits": [{"path": "src/cli.py", "append": True, "new": "bad"}], "continue": True},
            {"edits": [{"path": "../escape", "old": None, "new": "bad"}], "continue": True},
            {"edits": [{"path": "src/cli.py", "old": "missing", "new": "bad"}], "continue": True},
        ]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                add_chunk(self.root, draft, response, self.config)
            self.assertEqual(draft, original)
        self.assertEqual((self.root / "src/cli.py").read_text(), "old anchor\n")

    def test_draft_is_invalidated_by_base_or_plan_change_but_not_restart(self):
        self.task["draft"]["edits"] = [self.edit]
        self.task["source_notes"] = "Verified source"
        self.assertEqual(draft_for(self.task, "base", self.plan)["edits"], [self.edit])
        self.assertEqual(draft_for(self.task, "new-base", self.plan)["edits"], [])
        self.assertNotIn("source_notes", self.task)
        self.task["draft"]["edits"] = [self.edit]
        self.assertEqual(draft_for(self.task, "new-base", {**self.plan, "title": "Different"})["edits"], [])

    def test_legacy_complete_proposal_and_sequential_virtual_edits(self):
        edits = [{"path": "src/cli.py", "old": "old anchor", "new": "new anchor"},
                 {"path": "src/cli.py", "old": "new anchor", "new": "finished"}]
        draft, proposal = add_chunk(self.root, self.draft, {"edits": edits, "delivery": {}}, self.config)
        self.assertEqual(proposal, {"edits": edits, "delivery": {}})
        self.assertEqual(inspect_draft(
            self.root, draft, "src/cli.py", self.config, requested_context)[1], "finished\n")
        self.assertEqual((self.root / "src/cli.py").read_text(), "old anchor\n")

    def test_controller_restart_sends_saved_manifest_and_keeps_checkout_clean(self):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        git("init", "-q")
        git("config", "user.name", "Feature test")
        git("config", "user.email", "test@example.invalid")
        git("add", ".")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", base)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            active = state / "tasks/evalarc/active.json"
            active.parent.mkdir(parents=True)
            active.write_text(json.dumps({
                "id": "fixture", "repo": "noteflowai/evalarc", "base": base,
                "branch": "automation/fixture", "phase": "implement", "attempts": 0,
                "feedback": "Independent review: preserve held-out split isolation",
                "plan": {**self.plan, "read_paths": ["src/cli.py"]},
            }))
            with patch.object(developer, "checkout", return_value=self.root), \
                    patch.object(developer, "context", return_value={}), \
                    patch.object(developer, "ask", side_effect=[
                        {"edits": [self.edit], "continue": True, "transport_recovered": True},
                        KeyboardInterrupt("simulated worker restart"),
                    ]):
                with self.assertRaises(KeyboardInterrupt):
                    developer.develop("evalarc", {}, state / "workspace", state, "2026-09-28")

            def resumed_author(prompt, *_args, **_kwargs):
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                self.assertEqual(payload["saved_draft"][0]["path"], "src/new.py")
                self.assertIn("preserve held-out split isolation", payload["feedback"])
                self.assertIn("Output truncated", payload["transport_notice"])
                self.assertEqual(payload["snapshot"]["applied_candidate_edits"], False)
                self.assertFalse((self.root / "src/new.py").exists())
                self.assertEqual(git("status", "--porcelain"), "")
                raise KeyboardInterrupt("resume verified")
            with patch.object(developer, "checkout", return_value=self.root), \
                    patch.object(developer, "context", return_value={}), \
                    patch.object(developer, "ask", side_effect=resumed_author):
                with self.assertRaisesRegex(KeyboardInterrupt, "resume verified"):
                    developer.develop("evalarc", {}, state / "workspace", state, "2026-09-28")

    def test_rejected_complete_feature_is_retained_for_focused_correction(self):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        git("init", "-q")
        git("config", "user.name", "Feature test")
        git("config", "user.email", "test@example.invalid")
        git("add", ".")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", base)
        edits = [{"path": "src/cli.py", "old": "old anchor", "new": "new behavior"},
                 {"path": "docs/feature.md", "old": None, "new": "Complete feature usage\n"},
                 {"path": "tests/test_new.py", "old": None, "new": "assert 1 == 2\n"}]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            active = state / "tasks/evalarc/active.json"
            active.parent.mkdir(parents=True)
            active.write_text(json.dumps({
                "id": "fixture", "repo": "noteflowai/evalarc", "base": base,
                "branch": "automation/fixture", "phase": "implement", "attempts": 0,
                "plan": {**self.plan, "read_paths": ["src/cli.py"]},
            }))
            calls = []
            def author(prompt, *_args, review=False, **_kwargs):
                calls.append(review)
                if len(calls) == 1:
                    return {"edits": edits, "delivery": {}}
                if review:
                    self.assertIn("BEFORE candidate execution", prompt)
                    return {"approved": False, "findings": ["Correct contradictory assertion"]}
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                self.assertEqual(len(payload["saved_draft"]), 3)
                self.assertIn("contradictory assertion", payload["feedback"])
                self.assertEqual(git("status", "--porcelain"), "")
                self.assertEqual((self.root / "src/cli.py").read_text(), "old anchor\n")
                saved = json.loads(active.read_text())["draft"]
                fixed, proposal = add_chunk(self.root, saved, {
                    "edits": [{"path": "tests/test_new.py", "old": "assert 1 == 2",
                               "new": "assert 1 == 1"}], "delivery": {},
                }, self.config)
                self.assertEqual(proposal["edits"][:3], edits)
                self.assertIn("assert 1 == 1", inspect_draft(
                    self.root, fixed, "tests/test_new.py", self.config, requested_context)[1])
                raise KeyboardInterrupt("focused correction verified")
            with patch.object(developer, "checkout", return_value=self.root), \
                    patch.object(developer, "context", return_value={}), \
                    patch.object(developer, "prepare_release", return_value={"metadata_files": []}), \
                    patch.object(developer, "ask", side_effect=author):
                with self.assertRaisesRegex(KeyboardInterrupt, "focused correction verified"):
                    developer.develop("evalarc", {}, state / "workspace", state, "2026-09-28")
            self.assertEqual(calls, [False, True, False])


if __name__ == "__main__":
    unittest.main()
