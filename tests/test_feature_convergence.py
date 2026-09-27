"""Replays of planning restarts, exhausted budgets and next-day recovery."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.agent_pipeline import write_json
from scripts.feature_outcomes import outcome_exit
from scripts.feature_planning import previous_deferral
from scripts.feature_prompts import acceptance_paths
from scripts.feature_report import report


class PlanningRestartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/records.py").write_text("def existing():\n    return []\n")
        self.state = self.root / "state"
        self.active = self.state / "tasks/evalarc/active.json"
        self.task = {"id": "fixture", "repo": "noteflowai/evalarc", "base": "base",
                     "branch": "automation/fixture", "phase": "plan", "attempts": 0}
        write_json(self.active, self.task)
        self.plan = {
            "title": "feat: reject duplicate record identifiers",
            "problem": "Repeated identifiers silently replace earlier observations",
            "behavior": "The existing reader rejects duplicate IDs with a field-specific error.",
            "why_this_repo": "Preserve experiment evidence in the existing reader.",
            "acceptance": ["Duplicate IDs produce an error and no output artifact"],
            "read_paths": ["src/records.py"], "test_runner": "pytest",
            "test_path": acceptance_paths("fixture", ["pytest"])["pytest"],
            "target_user": "Engineer reading recorded evaluations",
            "success_metric": "No duplicate ID can silently replace an observation",
            "usage": "evalarc verify recorded-results",
            "limitations": "Only the existing record format; no new report types",
            "upgrade": "No stored data is changed; revert the added validation",
        }
        for target, kwargs in [
            ("checkout", {"return_value": self.repo}),
            ("command", {"return_value": "base"}),
            ("context", {"return_value": {"mission": "evaluation", "test_runners": ["pytest"]}}),
            ("calendar_day", {"return_value": "2026-09-28"}),
        ]:
            mocked = patch.object(developer, target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_feature(self):
        return developer.develop("evalarc", {}, self.root / "workspace",
                                 self.state, "2026-09-28", plan_only=True)

    def test_restart_keeps_full_candidate_findings_source_and_unique_attempts(self):
        def first(prompt, state, label, *, review=False):
            if review:
                return {"approved": False, "findings": ["Define the empty input behavior."]}
            if label.startswith("plan-1-"):
                return copy.deepcopy(self.plan)
            raise KeyboardInterrupt("simulate process termination")

        with patch.object(developer, "ask", side_effect=first):
            with self.assertRaises(KeyboardInterrupt):
                self.run_feature()
        saved = json.loads(self.active.read_text())
        self.assertEqual(saved["planning"]["candidate"], self.plan)
        self.assertEqual(saved["planning"]["history"][0]["attempt"], 1)
        failure = self.active.parent / "fixture/plan-1-failure.json"
        original_failure = failure.read_bytes()
        seen = []

        def resumed(prompt, state, label, *, review=False):
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            seen.append(label)
            self.assertEqual(payload["correction"]["previous_candidate"], self.plan)
            self.assertIn("empty input", payload["correction"]["recent_findings"][0]["feedback"])
            self.assertIn("src/records.py", payload["context"]["inspected_files"])
            if review:
                return {"approved": True, "findings": []}
            self.assertEqual(payload["correction"]["mode"], "narrow")
            return copy.deepcopy(self.plan)

        with patch.object(developer, "ask", side_effect=resumed):
            self.assertEqual(self.run_feature()["status"], "planned")
        self.assertEqual(seen, ["plan-3-read-0", "plan-review-3"])
        self.assertEqual(failure.read_bytes(), original_failure)

    def test_ninth_plan_rejection_is_terminal_without_an_extra_paid_worker(self):
        self.task["planning_attempts"] = 8
        write_json(self.active, self.task)
        with patch.object(developer, "ask", side_effect=[
            copy.deepcopy(self.plan), {"approved": False, "findings": ["Undefined empty input."]},
        ]) as ask:
            result = self.run_feature()
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["failed_phase"], "plan")
        self.assertEqual(result["planning_attempts"], 9)
        self.assertEqual(result["implementation_attempts"], 0)
        self.assertEqual(result["retry_after"], "2026-09-29")
        self.assertFalse(self.active.exists())

    def test_approved_ninth_plan_does_not_exhaust_implementation_budget(self):
        self.task.update(planning_attempts=9, phase="implement", plan=self.plan)
        write_json(self.active, self.task)
        self.assertEqual(self.run_feature()["status"], "planned")
        self.assertTrue(self.active.exists())

    def test_next_day_inherits_legacy_proposal_but_not_approval_or_old_source(self):
        directory = self.active.parent / "2026-09-27-old"
        write_json(directory / "terminal.json", {"status": "deferred", "completed_on": "2026-09-27"})
        write_json(directory / "retired-task.json",
                   {"id": "2026-09-27-old", "phase": "plan", "attempts": 0,
                    "planning_attempts": 9, "feedback": "Missing stable content identity"})
        write_json(directory / "plan-2-read-0.json", {"result": self.plan})
        self.assertEqual(previous_deferral(self.active.parent, "2026-09-27"), {})
        recovery = previous_deferral(self.active.parent, "2026-09-28")
        self.assertEqual(recovery["recovery_of"], "2026-09-27-old")
        self.assertEqual(recovery["planning"]["candidate"], self.plan)
        self.assertNotIn("plan", recovery)
        self.assertNotIn("source_base", recovery["planning"])
        self.assertIn("stable content", recovery["planning"]["history"][0]["feedback"])
        # An already-approved feature is never silently replaced through recovery.
        write_json(directory / "retired-task.json",
                   {"id": "2026-09-27-old", "phase": "implement", "attempts": 9, "plan": self.plan})
        self.assertEqual(previous_deferral(self.active.parent, "2026-09-28"), {})

    def test_report_recovers_actual_phase_from_legacy_retirement(self):
        write_json(self.state / "2026-09-27/evalarc/result.json",
                   {"status": "deferred", "feature_id": "old", "reason": "Nine implementation attempts failed"})
        write_json(self.active.parent / "old/retired-task.json",
                   {"phase": "plan", "planning_attempts": 9, "attempts": 0, "feedback": "Unknown labels"})
        data = report(self.state, "2026-09-27", metrics=False)
        row = next(x for x in data["projects"] if x["repo"] == "evalarc")["outcomes"][0]
        self.assertEqual(row["failed_phase"], "plan")
        self.assertEqual(row["implementation_attempts"], 0)
        self.assertNotIn("implementation", row["reason"])
        self.assertEqual(row["last_feedback"], "Unknown labels")


class TerminalOutcomeTests(unittest.TestCase):
    def test_only_exhausted_work_is_terminal_and_never_successful_delivery(self):
        self.assertEqual(outcome_exit([{"status": "published"}, {"status": "deferred"}]), 20)
        self.assertEqual(outcome_exit([{"status": "deferred"}, {"status": "retry-needed"}]), 1)
        self.assertEqual(outcome_exit([{"status": "published"}]), 0)
        self.assertEqual(outcome_exit([{"status": "planned"}]), 1)

    def test_real_bootstrap_does_not_retry_terminal_deferrals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin"
            (origin / "scripts").mkdir(parents=True)
            script = origin / "scripts/nightly.sh"
            script.write_text("#!/bin/bash\nprintf 'worker-called\\n'\nexit 20\n")
            script.chmod(0o755)
            for args in [("init", "-b", "main"), ("config", "user.name", "Test"),
                         ("config", "user.email", "test@example.invalid"),
                         ("add", "."), ("commit", "-m", "fixture")]:
                subprocess.run(["git", *args], cwd=origin, capture_output=True, check=True)
            bootstrap = Path(__file__).resolve().parents[1] / "scripts/radar-run"
            result = subprocess.run(["bash", str(bootstrap), "scripts/nightly.sh"],
                                    env={**os.environ, "RADAR_REPO": str(root / "clone"),
                                         "RADAR_CLONE_URL": str(origin), "RADAR_BRANCH": "main",
                                         "RADAR_LOCK": str(root / "lock"), "RADAR_TIMEOUT": "5s"},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("worker-called"), 1)
            self.assertIn("COMPLETED WITH DEFERRALS", result.stdout)
            self.assertNotIn("automatic retry", result.stdout)
