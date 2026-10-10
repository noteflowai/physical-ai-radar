"""Roadmap admission and crash recovery must not manufacture code or releases."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.agent_pipeline import write_json
from scripts.feature_governance import load_roadmap, next_version, validate_decision
from scripts.feature_outcomes import outcome_exit
from scripts.feature_policy import PROJECTS, allowed, readable
from scripts.feature_report import report
from publication_fixture import isolate_private_recovery

DAY = "2026-09-29"
ROADMAP = """# Roadmap
Updated: 2026-09-29

## Now — validate existing value
| EA-01 | Real evidence import | Preserve every observation |

## Next
| EA-02 | New provider | Only after user validation |
"""


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        isolate_private_recovery(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "ROADMAP.md").write_text(ROADMAP)
        (self.repo / "src/reader.py").write_text("def read(records):\n    return list(records)\n")
        self.state = self.root / "state"
        self.active = self.state / "tasks/evalarc/active.json"
        self.decision = {
            "work_type": "no-change", "milestone_id": "EA-01",
            "problem_evidence": "The inspected reader preserves all observations; no failed user case is available.",
            "expected_outcome": "Collect a permitted real export and observe a first-use trial before expanding imports.",
            "follow_up_on": "2026-10-02",
            "reason": "The milestone needs a real user trial before another import capability is justified.",
            "read_paths": ["src/reader.py"],
        }
        for target, kwargs in [
            ("checkout", {"return_value": self.repo}),
            ("command", {"return_value": "base"}),
            ("context", {"return_value": {"mission": "evaluation", "test_runners": ["pytest"]}}),
            ("calendar_day", {"return_value": DAY}),
        ]:
            mocked = patch.object(developer, target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_decision(self, day=DAY):
        return developer.develop("evalarc", {}, self.root / "workspace", self.state, day)

    def approve(self):
        return patch.object(developer, "ask", side_effect=[
            copy.deepcopy(self.decision), {"approved": True, "findings": []}])

    def test_later_stale_missing_and_symlink_roadmaps_fail_closed(self):
        roadmap = load_roadmap(self.repo, "base", DAY)
        self.assertEqual(set(roadmap["now"]), {"EA-01"})
        with self.assertRaisesRegex(ValueError, "Now"):
            validate_decision({**self.decision, "milestone_id": "EA-02"}, roadmap, DAY)
        stale = load_roadmap(self.repo, "base", "2026-11-10")
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_decision({**self.decision, "work_type": "feature"}, stale, "2026-11-10")
        path = self.repo / "ROADMAP.md"
        path.unlink()
        with patch.object(developer, "ask") as ask:
            with self.assertRaisesRegex(ValueError, "ROADMAP"):
                self.run_decision()
            ask.assert_not_called()
        elsewhere = self.root / "outside.md"
        elsewhere.write_text(ROADMAP)
        path.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "regular"):
            load_roadmap(self.repo, "base", DAY)

    def test_roadmap_is_readable_but_cannot_be_changed_by_worker(self):
        for config in PROJECTS.values():
            self.assertTrue(readable("ROADMAP.md", config))
            self.assertFalse(allowed("ROADMAP.md", config))

    def test_no_change_requires_real_review_and_never_publishes_or_repeats_today(self):
        with self.approve() as ask, patch.object(developer, "queue_update") as queue, \
                patch.object(developer, "advance") as advance:
            result = self.run_decision()
            self.assertEqual(ask.call_count, 2)
            payload = json.loads(ask.call_args.args[0].rsplit("\n", 1)[1])
            self.assertIn("src/reader.py", payload["context"]["inspected_files"])
            self.assertEqual(result["status"], "no-change")
            self.assertEqual(result["governance"]["revision"], "base")
            self.assertEqual(len(result["governance"]["sha256"]), 64)
            self.assertFalse(self.active.exists())
            self.assertFalse((self.state / "allowances").exists())
            queue.assert_not_called()
            advance.assert_not_called()
        with patch.object(developer, "ask") as ask, patch.object(developer, "checkout") as checkout:
            self.assertEqual(self.run_decision(), result)
            ask.assert_not_called()
            checkout.assert_not_called()
        self.assertEqual(outcome_exit([result]), 0)
        self.assertEqual(outcome_exit([result, {"status": "deferred"}]), 20)
        row = next(x for x in report(self.state, DAY, metrics=False)["projects"]
                   if x["repo"] == "evalarc")["outcomes"][0]
        self.assertEqual(row["follow_up_on"], "2026-10-02")
        self.assertEqual(row["status"], "no-change")

    def test_rejected_no_change_is_not_terminal_success(self):
        calls = [copy.deepcopy(self.decision), {"approved": False, "findings": ["Known defect ignored."]}] * 3
        with patch.object(developer, "ask", side_effect=calls):
            with self.assertRaisesRegex(RuntimeError, "Known defect"):
                self.run_decision()
        self.assertTrue(self.active.exists())
        self.assertFalse(list(self.active.parent.glob("*/terminal.json")))

    def test_crash_before_terminal_resumes_the_approved_decision_without_model_or_release(self):
        original = developer.write_json

        def crash(path, value):
            if path.name == "terminal.json":
                raise OSError("disk interrupted")
            return original(path, value)

        with self.approve(), patch.object(developer, "write_json", side_effect=crash):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.run_decision()
        self.assertEqual(json.loads(self.active.read_text())["phase"], "no-change")
        with patch.object(developer, "ask") as ask, patch.object(developer, "queue_update") as queue:
            self.assertEqual(self.run_decision()["status"], "no-change")
            ask.assert_not_called()
            queue.assert_not_called()
        self.assertFalse(self.active.exists())

    def test_crash_after_terminal_recovers_day_receipt_without_allowance_or_announcement(self):
        original = developer.write_json

        def crash(path, value):
            if path == self.state / DAY / "evalarc/result.json":
                raise OSError("receipt interrupted")
            return original(path, value)

        with self.approve(), patch.object(developer, "write_json", side_effect=crash):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.run_decision()
        with patch.object(developer, "ask") as ask, patch.object(developer, "queue_update") as queue:
            result = self.run_decision()
            ask.assert_not_called()
            queue.assert_not_called()
        self.assertEqual(json.loads((self.state / DAY / "evalarc/result.json").read_text()), result)
        self.assertFalse((self.state / "allowances").exists())

    def test_forged_effects_and_followup_dates_are_rejected(self):
        roadmap = load_roadmap(self.repo, "base", DAY)
        for change in [{"follow_up_on": DAY}, {"follow_up_on": "2027-01-01"},
                       {"edits": [{"path": "src/reader.py"}]},
                       {"decision_probe": {"purpose": "spend money"}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_decision({**self.decision, **change}, roadmap, DAY)

    def test_old_approved_plan_does_not_require_new_governance_fields(self):
        (self.repo / "ROADMAP.md").unlink()
        write_json(self.active, {"id": "legacy", "repo": "noteflowai/evalarc",
                                "base": "base", "branch": "automation/legacy",
                                "phase": "implement", "attempts": 0, "plan": {"title": "approved"}})
        with patch.object(developer, "ask") as ask:
            result = developer.develop("evalarc", {}, self.root / "workspace",
                                       self.state, DAY, plan_only=True)
            self.assertEqual(result["status"], "planned")
            ask.assert_not_called()

    def test_work_kind_controls_semantic_version_without_no_change_release(self):
        self.assertEqual(next_version("1.48.0"), "1.49.0")
        self.assertEqual(next_version("1.48.0", "fix"), "1.48.1")
        self.assertEqual(next_version("0.16.3", "maintenance"), "0.16.4")
        with self.assertRaises(ValueError):
            next_version("0.16.3", "no-change")


if __name__ == "__main__":
    unittest.main()
