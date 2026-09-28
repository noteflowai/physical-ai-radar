import copy
import json
import unittest

from scripts.feature_prompts import acceptance_paths, plan_review_prompt
from scripts.develop_repos import validate_plan
from scripts.feature_policy import PROJECTS


class FeaturePromptTests(unittest.TestCase):
    def test_large_review_preserves_contract_and_source_before_duplicate_background(self):
        plan = {"title": "review recorded decisions", "behavior": "Complete input-to-report flow",
                "decision_probe": {"cases": [{"id": "zh", "request": {"state": "中文状态"}}]}}
        context = {"mission": "Evaluation", "readmes": {"README.md": "重复背景" * 12000},
                   "inspected_files": {"src/report.py": "def report():\n    return 'observable'\n" * 1100},
                   "topic_notes": [{"id": "typed-decisions", "checked_on": "2026-09-27"}]}
        before = copy.deepcopy(context)
        text = plan_review_prompt("Review:\n", plan, context)
        self.assertLessEqual(len(text.encode()), 100000)
        decoded = json.loads(text.removeprefix("Review:\n"))
        self.assertEqual(decoded["plan"], plan)
        self.assertEqual(decoded["context"]["inspected_files"], context["inspected_files"])
        self.assertEqual(decoded["context"]["topic_notes"], context["topic_notes"])
        self.assertEqual(context, before)
        self.assertIn("readmes", decoded["context"]["omitted_background"])

    def test_core_contract_is_rejected_rather_than_silently_truncated(self):
        with self.assertRaisesRegex(ValueError, "narrow the feature"):
            plan_review_prompt("Review", {"behavior": "x" * 100001}, {"mission": "test"})

    def test_reserved_filename_is_stable_for_all_supported_runners(self):
        paths = acceptance_paths("2026-09-27-evalarc-133828858344", ["pytest", "unittest", "node", "php", "vitest"])
        self.assertEqual(paths["pytest"], "tests/test_feature_f31bb4b56f2a.py")
        self.assertEqual(paths["pytest"], paths["unittest"])
        self.assertTrue(paths["node"].endswith(".test.cjs"))
        self.assertTrue(paths["vitest"].endswith(".test.ts"))
        self.assertTrue(paths["php"].endswith(".php"))

    def test_overlong_behavior_reports_actionable_length_instead_of_missing_behavior(self):
        with self.assertRaisesRegex(ValueError, r"behavior.*5\.\.6000.*6078"):
            validate_plan({"title": "valid title", "problem": "valid problem",
                           "behavior": "x" * 6078}, PROJECTS["dsh-skills-anywhere"])


if __name__ == "__main__":
    unittest.main()
