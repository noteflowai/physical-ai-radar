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
from publication_fixture import isolate_private_recovery, security_home


class PlanningRestartTests(unittest.TestCase):
    def setUp(self):
        isolate_private_recovery(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/records.py").write_text("def existing():\n    return []\n")
        (self.repo / "ROADMAP.md").write_text(
            "# Roadmap\nUpdated: 2026-09-28\n\n## Now\n"
            "| EA-01 | Preserve recorded evidence | Reject duplicate IDs |\n")
        self.state = self.root / "state"
        self.active = self.state / "tasks/evalarc/active.json"
        self.task = {"id": "fixture", "repo": "noteflowai/evalarc", "base": "base",
                     "branch": "automation/fixture", "phase": "plan", "attempts": 0}
        write_json(self.active, self.task)
        self.plan = {
            "work_type": "fix", "milestone_id": "EA-01",
            "problem_evidence": "The existing reader silently replaces repeated record identifiers.",
            "expected_outcome": "Every recorded observation retains its identity or fails explicitly.",
            "follow_up_on": "2026-09-29",
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
        revised = {**self.plan, "behavior": self.plan["behavior"] + " Empty input is rejected."}

        def resumed(prompt, state, label, *, review=False):
            payload = json.loads(prompt.rsplit("\n", 1)[1])
            seen.append(label)
            self.assertEqual(payload["correction"]["previous_candidate"], self.plan)
            self.assertIn("empty input", payload["correction"]["recent_findings"][0]["feedback"])
            self.assertIn("src/records.py", payload["context"]["inspected_files"])
            if review:
                self.assertEqual(payload["plan"]["behavior"], revised["behavior"])
                self.assertNotEqual(payload["plan"], payload["correction"]["previous_candidate"])
                return {"approved": True, "findings": []}
            self.assertEqual(payload["correction"]["mode"], "narrow")
            return copy.deepcopy(revised)

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
        self.assertEqual(result["reason_code"], "plan-budget-exhausted")
        self.assertFalse(self.active.exists())

    def test_approved_ninth_plan_does_not_exhaust_implementation_budget(self):
        self.task.update(planning_attempts=9, phase="implement", plan=self.plan)
        write_json(self.active, self.task)
        self.assertEqual(self.run_feature()["status"], "planned")
        self.assertTrue(self.active.exists())

    def test_changed_main_defers_approved_plan_before_worker_or_checkout(self):
        self.task.update(phase="implement", planning_attempts=2, attempts=3,
                         plan=self.plan, draft={"edits": [{"path": "src/records.py"}]})
        write_json(self.active, self.task)
        publication = self.active.parent / "fixture/publication.json"
        write_json(publication, {"status": "unverified"})
        original_publication = publication.read_bytes()
        with patch.object(developer, "command", return_value="new-base") as command, \
                patch.object(developer, "ask") as ask, \
                patch.object(developer, "reset") as reset:
            result = self.run_feature()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason_code"], "approved-source-changed")
        self.assertEqual(result["implementation_attempts"], 3)
        self.assertEqual(result["planning_attempts"], 2)
        self.assertEqual(result["publication_state"], {"status": "unverified"})
        self.assertEqual(result["retry_after"], "2026-09-29")
        self.assertEqual(command.call_count, 1)  # Read main; never switch or edit source.
        ask.assert_not_called()
        reset.assert_not_called()
        self.assertFalse(self.active.exists())
        self.assertEqual(json.loads((publication.parent / "retired-task.json").read_text()),
                         self.task)
        self.assertEqual(publication.read_bytes(), original_publication)
        self.assertFalse((self.state / "allowances").exists())

    def test_saved_approval_revision_survives_a_correction_rebase(self):
        approved = {**self.plan, "governance": {"revision": "original-base"}}
        self.task.update(phase="implement", plan=approved)
        write_json(self.active, self.task)
        with patch.object(developer, "ask") as ask:
            result = self.run_feature()
        self.assertEqual(result["reason_code"], "approved-source-changed")
        ask.assert_not_called()

    def test_legacy_inspected_revision_is_used_without_governance(self):
        self.task.update(phase="implement", plan=self.plan,
                         planning={"source_base": "original-base"})
        write_json(self.active, self.task)
        with patch.object(developer, "ask") as ask:
            result = self.run_feature()
        self.assertEqual(result["reason_code"], "approved-source-changed")
        ask.assert_not_called()

    def test_changed_main_without_approval_still_inspects_and_plans(self):
        with patch.object(developer, "command", return_value="new-base"), \
                patch.object(developer, "ask", side_effect=KeyboardInterrupt("planning")) as ask:
            with self.assertRaises(KeyboardInterrupt):
                self.run_feature()
        ask.assert_called_once()
        saved = json.loads(self.active.read_text())
        self.assertEqual(saved["base"], "new-base")
        self.assertNotIn("plan", saved)

    def test_validated_transaction_keeps_its_exact_commit_when_main_changes(self):
        self.task.update(phase="validate", plan=self.plan)
        write_json(self.active, self.task)
        with patch.object(developer, "command", return_value="new-base"), \
                patch.object(developer, "restore") as restore, \
                patch.object(developer, "advance", return_value={"status": "resumed"}) as advance, \
                patch.object(developer, "ask") as ask:
            result = developer.develop("evalarc", {}, self.root / "workspace",
                                       self.state, "2026-09-28")
        self.assertEqual(result["status"], "resumed")
        restore.assert_called_once()
        advance.assert_called_once()
        ask.assert_not_called()
        self.assertEqual(json.loads(self.active.read_text()), self.task)

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

    def test_newer_publication_or_same_day_terminal_does_not_revive_old_deferral(self):
        older = self.active.parent / "2026-09-26-old"
        write_json(older / "terminal.json", {"status": "deferred", "completed_on": "2026-09-26"})
        write_json(older / "retired-task.json",
                   {"id": "old", "phase": "plan", "attempts": 0, "planning_attempts": 9})
        newer = self.active.parent / "2026-09-27-new"
        write_json(newer / "complete.json", {"status": "published", "completed_on": "2026-09-27"})
        self.assertEqual(previous_deferral(self.active.parent, "2026-09-28"), {})
        (newer / "complete.json").unlink()
        write_json(newer / "terminal.json", {"status": "deferred", "completed_on": "2026-09-28"})
        self.assertEqual(previous_deferral(self.active.parent, "2026-09-28"), {})

    def test_task_creation_recovers_legacy_plan_and_first_ever_run_starts_cleanly(self):
        self.active.unlink()
        directory = self.active.parent / "2026-09-27-old"
        write_json(directory / "terminal.json", {"status": "deferred", "completed_on": "2026-09-27"})
        write_json(directory / "retired-task.json",
                   {"id": "2026-09-27-old", "phase": "plan", "attempts": 0,
                    "planning_attempts": 9, "feedback": "Check duplicates"})
        write_json(directory / "plan-2-read-0.json", {"result": self.plan})
        with patch.object(developer, "ask", side_effect=KeyboardInterrupt("created")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_feature()
        task = json.loads(self.active.read_text())
        self.assertEqual(task["recovery_of"], "2026-09-27-old")
        self.assertEqual(task["planning_attempts"], 1)
        self.assertEqual(task["attempts"], 0)
        self.assertNotEqual(task["id"], "2026-09-27-old")
        self.assertEqual(task["planning"]["candidate"], self.plan)
        fresh_state = self.root / "first-ever"
        with patch.object(developer, "ask", side_effect=KeyboardInterrupt("created")):
            with self.assertRaises(KeyboardInterrupt):
                developer.develop("evalarc", {}, self.root / "workspace",
                                  fresh_state, "2026-09-28", plan_only=True)
        task = json.loads((fresh_state / "tasks/evalarc/active.json").read_text())
        self.assertNotIn("recovery_of", task)
        self.assertEqual(task["planning_attempts"], 1)

    def test_report_recovers_actual_phase_from_legacy_retirement(self):
        write_json(self.state / "2026-09-27/evalarc/result.json",
                   {"status": "deferred", "feature_id": "old", "reason": "Nine implementation attempts failed"})
        write_json(self.active.parent / "old/retired-task.json",
                   {"phase": "plan", "planning_attempts": 9, "attempts": 0, "feedback": "Unknown labels"})
        data = report(self.state, "2026-09-27", metrics=False)
        row = next(x for x in data["projects"] if x["repo"] == "evalarc")["outcomes"][0]
        self.assertEqual(row["failed_phase"], "plan")
        self.assertEqual(row["implementation_attempts"], 0)
        self.assertNotIn("Nine implementation", row["reason"])
        self.assertEqual(row["last_feedback"], "Unknown labels")

    def test_report_preserves_non_budget_deferral_reason(self):
        write_json(self.state / "2026-09-27/evalarc/result.json",
                   {"status": "deferred", "feature_id": "old", "reason": "Maintainer cancelled proposal"})
        write_json(self.active.parent / "old/retired-task.json",
                   {"phase": "implement", "attempts": 1})
        data = report(self.state, "2026-09-27", metrics=False)
        row = next(x for x in data["projects"] if x["repo"] == "evalarc")["outcomes"][0]
        self.assertEqual(row["reason"], "Maintainer cancelled proposal")
        self.assertIsNone(row["reason_code"])


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
                                    env={**os.environ, "HOME": security_home(root),
                                         "RADAR_REPO": str(root / "clone"),
                                         "RADAR_CLONE_URL": str(origin), "RADAR_BRANCH": "main",
                                         "RADAR_LOCK": str(root / "lock"), "RADAR_TIMEOUT": "5s"},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("worker-called"), 1)
            self.assertIn("COMPLETED WITH DEFERRALS", result.stdout)
            self.assertNotIn("automatic retry", result.stdout)
