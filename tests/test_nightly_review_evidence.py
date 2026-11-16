"""The planner and both planning reviews must see the same controller rules and evidence rules.

An independent reviewer that was not told a controller rule treats a plan that quotes it as
having invented it, and a reviewer that cannot see a source range or the recent commits cannot
check a claim that rests on them. These tests pin what the planner is told, what the reviewers
are told, and what the controller refuses to shorten without saying so.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.agent_pipeline import write_json
from scripts.feature_prompts import (
    CONTROLLER_RULES, DROP_ORDER, MAX_INSPECT_RANGES, SIGNATURE_ONLY_BYTES, acceptance_paths,
    bounded_prompt, controller_rules_for_reviewer, reviewer_visibility_for_planner)
from publication_fixture import isolate_private_recovery

DAY = "2026-09-28"
ROADMAP = ("# Roadmap\nUpdated: 2026-09-28\n\n## Now\n"
           "| EA-01 | Preserve recorded evidence | Reject duplicate IDs |\n")


class PromptHarness(unittest.TestCase):
    """Drive the real planning stage with a recording stand-in for the model."""

    def setUp(self):
        isolate_private_recovery(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/records.py").write_text("def existing():\n    return []\n")
        (self.repo / "ROADMAP.md").write_text(ROADMAP)
        self.state = self.root / "state"
        self.active = self.state / "tasks/evalarc/active.json"
        write_json(self.active, {"id": "fixture", "repo": "noteflowai/evalarc", "base": "base",
                                 "branch": "automation/fixture", "phase": "plan", "attempts": 0})
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
            ("calendar_day", {"return_value": DAY}),
        ]:
            mocked = patch.object(developer, target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_plan(self, plan=None, approved=True):
        """Return {label: prompt} for every model call of one planning run."""
        seen = {}
        plan = copy.deepcopy(plan or self.plan)

        def ask(prompt, state, label, *, review=False):
            seen[label] = prompt
            return {"approved": approved, "findings": [] if approved else ["fix it"]} if review \
                else copy.deepcopy(plan)

        with patch.object(developer, "ask", side_effect=ask):
            developer.develop("evalarc", {}, self.root / "workspace", self.state, DAY, plan_only=True)
        return seen

    def run_no_change(self):
        decision = {
            "work_type": "no-change", "milestone_id": "EA-01",
            "problem_evidence": "The inspected reader preserves all observations; no failed user case.",
            "expected_outcome": "Collect a permitted real export before expanding imports.",
            "follow_up_on": "2026-10-02",
            "reason": "The milestone needs a real user trial before another import capability.",
            "read_paths": ["src/records.py"],
        }
        write_json(self.active, {"id": "fixture2", "repo": "noteflowai/evalarc", "base": "base",
                                 "branch": "automation/fixture2", "phase": "plan", "attempts": 0})
        seen = {}

        def ask(prompt, state, label, *, review=False):
            seen[label] = prompt
            return {"approved": True, "findings": []} if review else copy.deepcopy(decision)

        with patch.object(developer, "ask", side_effect=ask), \
                patch.object(developer, "queue_update"), patch.object(developer, "advance"):
            developer.develop("evalarc", {}, self.root / "workspace", self.state, DAY)
        return seen


class SharedControllerRulesTest(PromptHarness):
    def prompts(self):
        planned = self.run_plan()
        planner = planned["plan-1-read-0"]
        plan_review = planned["plan-review-1"]
        no_change_review = next(p for label, p in self.run_no_change().items()
                                if label.startswith("no-change-review"))
        return planner, plan_review, no_change_review

    def test_every_controller_rule_is_in_the_planner_and_both_reviews_verbatim(self):
        planner, plan_review, no_change_review = self.prompts()
        self.assertGreaterEqual(len(CONTROLLER_RULES), 2)
        for rule in CONTROLLER_RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, planner)
                self.assertIn(rule, plan_review)
                self.assertIn(rule, no_change_review)

    def test_both_reviews_carry_the_identical_marked_rule_block(self):
        _, plan_review, no_change_review = self.prompts()
        block = controller_rules_for_reviewer()
        self.assertIn("Controller-supplied rules", block)
        self.assertEqual(plan_review.count(block), 1)
        self.assertEqual(no_change_review.count(block), 1)

    def test_the_rule_block_holds_exactly_the_rules_the_planner_is_given(self):
        # The reviewer's block is built from the same tuple as the planner's sentences, so a
        # rule added to one place cannot be missing from the other.
        block = controller_rules_for_reviewer()
        for rule in CONTROLLER_RULES:
            self.assertEqual(block.count(rule), 1)
        remainder = block
        for rule in CONTROLLER_RULES:
            remainder = remainder.replace(rule, "")
        self.assertNotIn("roadmap", remainder.lower())
        self.assertNotIn("documentation", remainder.lower())

    def test_the_two_known_rules_are_present_so_an_edit_of_either_is_noticed(self):
        self.assertIn("A stale roadmap forbids new features.", CONTROLLER_RULES)
        self.assertTrue(any(r.startswith("Pure documentation may be identified") for r in CONTROLLER_RULES))


class ReviewerVisibilityTest(PromptHarness):
    def test_planner_is_told_the_dropped_fields_in_the_order_they_are_dropped(self):
        text = self.run_plan()["plan-1-read-0"]
        note = reviewer_visibility_for_planner()
        self.assertIn(note, text)
        listed = note.split("in this order: ", 1)[1].split(";", 1)[0]
        self.assertEqual(listed.split(", "), list(DROP_ORDER))
        self.assertIn("unverified", note)

    def test_planner_is_told_the_signature_limit_and_range_cap_the_code_uses(self):
        text = self.run_plan()["plan-1-read-0"]
        self.assertIn(f"{SIGNATURE_ONLY_BYTES} bytes", text)
        self.assertIn(f"at most {MAX_INSPECT_RANGES} ranges", text)
        self.assertEqual(SIGNATURE_ONLY_BYTES, 24000)
        self.assertEqual(MAX_INSPECT_RANGES, 5)

    def test_signature_limit_in_the_prompt_is_the_one_text_file_enforces(self):
        big = "def f():\n    pass\n" * (SIGNATURE_ONLY_BYTES // 18 + 50)
        (self.repo / "src/big.py").write_text(big)
        self.assertGreater(len(big.encode()), SIGNATURE_ONLY_BYTES)
        self.assertIn("lines; request need_files", developer.text_file(self.repo, "src/big.py"))
        small = "x = 1\n" * 10
        (self.repo / "src/small.py").write_text(small)
        self.assertEqual(developer.text_file(self.repo, "src/small.py"), small)


class DropOrderTest(unittest.TestCase):
    def context(self):
        return {"mission": "m", "readmes": {"README.md": "r" * 40000}, "tree": ["t" * 100] * 300,
                "research": [{"n": "x" * 20000}], "issues": [{"b": "i" * 20000}],
                "recent_commits": "37ce84f one\n1111111 two\n"}

    def test_recent_commits_is_the_last_field_dropped(self):
        self.assertEqual(DROP_ORDER[-1], "recent_commits")
        self.assertEqual(set(DROP_ORDER), {"readmes", "tree", "research", "issues", "recent_commits"})

    def test_recent_commits_survives_when_dropping_the_bulkier_fields_is_enough(self):
        context = self.context()
        full = len(("P:" + json.dumps({"context": context}, ensure_ascii=False,
                                       separators=(",", ":"))).encode())
        without_recent = copy.deepcopy(context)
        for field in DROP_ORDER[:-1]:
            without_recent.pop(field)
        smallest = len(("P:" + json.dumps({"context": without_recent}, ensure_ascii=False,
                                          separators=(",", ":"))).encode())
        self.assertGreater(full, smallest + 200)
        text = bounded_prompt("P:", {}, context, budget=smallest + 100)
        decoded = json.loads(text.removeprefix("P:"))
        self.assertIn("recent_commits", decoded["context"])
        self.assertEqual(decoded["context"]["omitted_background"], list(DROP_ORDER[:-1]))

    def test_recent_commits_is_dropped_only_after_every_other_field_and_is_then_named(self):
        def size(dropped):
            ctx = self.context()
            for field in dropped:
                ctx.pop(field)
            if dropped:
                ctx["omitted_background"] = list(dropped)
            return len(("P:" + json.dumps({"context": ctx}, ensure_ascii=False,
                                           separators=(",", ":"))).encode())

        keeps_recent = size(DROP_ORDER[:-1])
        drops_recent = size(DROP_ORDER)
        self.assertLess(drops_recent, keeps_recent)
        budget = drops_recent + (keeps_recent - drops_recent) // 2
        text = bounded_prompt("P:", {}, self.context(), budget=budget)
        decoded = json.loads(text.removeprefix("P:"))
        self.assertNotIn("recent_commits", decoded["context"])
        self.assertEqual(decoded["context"]["omitted_background"], list(DROP_ORDER))

    def test_input_context_is_not_mutated(self):
        context = self.context()
        before = copy.deepcopy(context)
        bounded_prompt("P:", {}, context, budget=300)
        self.assertEqual(context, before)


class CutRangeTest(PromptHarness):
    def numbered_file(self, name, lines, width=60):
        text = "\n".join(f"{n:04d} " + "x" * (width - 5) for n in range(1, lines + 1)) + "\n"
        (self.repo / "src" / name).write_text(text)
        return text

    def plan_with_range(self, name, start, end):
        return {**self.plan, "read_paths": ["src/records.py"],
                "inspect_ranges": [{"path": "src/" + name, "start_line": start, "end_line": end}]}

    def run_expecting_correction(self, plan):
        calls = []

        def ask(prompt, state, label, *, review=False):
            calls.append(label)
            return {"approved": True, "findings": []} if review else copy.deepcopy(plan)

        with patch.object(developer, "ask", side_effect=ask):
            try:
                developer.develop("evalarc", {}, self.root / "workspace", self.state, DAY,
                                  plan_only=True)
            except RuntimeError as error:
                return calls, str(error)
        return calls, None

    def test_a_range_cut_by_the_byte_budget_is_returned_to_the_planner_not_reviewed(self):
        # Mirrors the observed case: a plan listed 279-414 of a 766-line file and the
        # reviewer was shown 279-373 only, because earlier excerpts already used the budget.
        self.numbered_file("lerobot.py", 766, width=170)
        for i in range(4):
            self.numbered_file(f"filler{i}.py", 100, width=100)
        plan = self.plan_with_range("lerobot.py", 279, 414)
        reads = {"need_files": [{"path": f"src/filler{i}.py", "start_line": 1, "end_line": 100}
                                for i in range(4)]}
        calls = []

        def ask(prompt, state, label, *, review=False):
            calls.append(label)
            if review:
                return {"approved": True, "findings": []}
            return copy.deepcopy(reads if label == "plan-1-read-0" else plan)

        with patch.object(developer, "ask", side_effect=ask):
            with self.assertRaises(RuntimeError):
                developer.develop("evalarc", {}, self.root / "workspace", self.state, DAY,
                                  plan_only=True)
        self.assertEqual(calls[:2], ["plan-1-read-0", "plan-1-read-1"])
        self.assertFalse([label for label in calls if label.startswith("plan-review")])
        failure = (self.active.parent / "fixture/plan-1-failure.json").read_text()
        self.assertIn("would not see these requested ranges in full", failure)
        self.assertIn("src/lerobot.py lines 279-414 would be cut at line", failure)
        self.assertIn("request a narrower range", failure)
        task = json.loads(self.active.read_text())
        self.assertNotIn("src/lerobot.py [lines", json.dumps(task["planning"]["inspected_files"]))

    def test_a_range_over_the_line_cap_is_also_reported(self):
        self.numbered_file("long.py", 600, width=20)
        calls, _ = self.run_expecting_correction(self.plan_with_range("long.py", 1, 400))
        self.assertNotIn("plan-review-1", calls)
        failure = (self.active.parent / "fixture/plan-1-failure.json").read_text()
        self.assertIn("src/long.py lines 1-400 would be cut at line 240", failure)

    def test_every_cut_range_is_reported_in_one_correction(self):
        self.numbered_file("a.py", 600, width=20)
        self.numbered_file("b.py", 600, width=20)
        plan = {**self.plan, "inspect_ranges": [
            {"path": "src/a.py", "start_line": 1, "end_line": 400},
            {"path": "src/b.py", "start_line": 10, "end_line": 500}]}
        self.run_expecting_correction(plan)
        failure = (self.active.parent / "fixture/plan-1-failure.json").read_text()
        self.assertIn("src/a.py lines 1-400", failure)
        self.assertIn("src/b.py lines 10-500", failure)

    def test_a_range_that_fits_is_reviewed_and_kept(self):
        self.numbered_file("ok.py", 300, width=20)
        calls, error = self.run_expecting_correction(self.plan_with_range("ok.py", 10, 200))
        self.assertIsNone(error)
        self.assertIn("plan-review-1", calls)

    def test_a_range_past_the_end_of_the_file_is_not_a_cut(self):
        self.numbered_file("short.py", 50, width=20)
        calls, error = self.run_expecting_correction(self.plan_with_range("short.py", 1, 200))
        self.assertIsNone(error)
        self.assertIn("plan-review-1", calls)

    def test_a_search_request_is_not_treated_as_a_cut(self):
        self.numbered_file("find.py", 400, width=20)
        plan = {**self.plan, "inspect_ranges": [{"path": "src/find.py", "search": "0123"}]}
        calls, error = self.run_expecting_correction(plan)
        self.assertIsNone(error)
        self.assertIn("plan-review-1", calls)

    def test_helper_returns_none_for_malformed_requests_and_read_errors(self):
        helper = developer.range_cut_message
        self.assertIsNone(helper(self.repo, "src/x.py", "src/x.py", "text"))
        self.assertIsNone(helper(self.repo, {"path": "src/x.py", "start_line": "1", "end_line": 3},
                                 "src/x.py [lines 1-3]", "text"))
        self.assertIsNone(helper(self.repo, {"path": "src/nope.py", "start_line": 1, "end_line": 3},
                                 'read_error {"path": "src/nope.py"}', "boom"))

    def test_a_single_line_over_the_excerpt_budget_is_reported(self):
        message = developer.range_cut_message(
            self.repo, {"path": "src/x.py", "start_line": 7, "end_line": 7}, "src/x.py [lines 7-7]",
            "<single source line exceeds the excerpt budget>")
        self.assertIn("line 7 alone exceeds", message)


if __name__ == "__main__":
    unittest.main()
