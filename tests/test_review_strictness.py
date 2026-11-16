"""The strictness clauses of the two planning reviews are pinned: weaken or delete one and a test fails.

The reviews are model prompts, so what keeps them strict is their wording. `test_nightly_review_evidence.py` pins that the
planner and the reviews carry the same controller rules and the same account of what the reviewer can see; nothing pinned the
sentences that make the reviewer demand evidence and reject the invented kind. A mutation that turned "Require concrete
evidence, ..." into "Accept a reason, ..." passed the whole suite (04's review of PR218).

Each clause below is a sentence of the instruction a reviewer is given (one is the rest of a sentence that the controller rules
interrupt), read from the prompt that `develop()` really
sends (not from a copy of the source), and it must appear there exactly once, before the JSON the reviewer is asked about:
a plan that quotes a clause cannot stand in for it. Changing one of these sentences on purpose means changing this list in the
same commit, which is the review this test is for. The clauses that keep a reviewer from asking for too much ("Do not demand a
new feature merely to fill a quota") are pinned too, so the balance cannot shift either way unseen. What this does not show:
how a model reads the words.
"""
from __future__ import annotations

import unittest

from test_nightly_review_evidence import PromptHarness

# What a no-change review is told. Strictness first, then the limits on it.
NO_CHANGE_STRICT = (
    "Require concrete evidence, a justified reason, honest uncertainty and a useful follow-up date.",
    "Reject ignored known defects or invented user evidence.",
    "This decision has no edits, execution or release authority.",
    'Return {"approved":true|false,"findings":[]}.',
)
NO_CHANGE_BALANCE = (
    "Do not demand a new feature merely to fill a quota.",
)

# What a feature-plan review is told.
PLAN_STRICT = (
    "Review this feature plan for ONE complete capability, project fit, compatibility, alignment with its Now milestone, "
    "concrete problem evidence, expected outcome and follow-up date.",
    "Check that fixes/maintenance do not hide a new feature under a patch version.",
    "testability and realistic scope.",
    "For UI changes, require a complete user path, professional visual direction consistent with the product, relevant "
    "states and feasible interaction/accessibility validation.",
    "The controller adds release_requirements to WordPress plans and always enforces new behavioral tests and product docs.",
    "For trend-inspired work, check source provenance, actual repository gap, baseline, feasible access and distinct "
    "project ownership; reject name-dropping or invented integration evidence.",
    "For decision_probe, review the exact inputs and question/options contract, its necessity for this one feature and "
    "truthful synthetic-data scope.",
    "Inspect the correction history for regressions of earlier fixes.",
    "Report all currently identifiable blockers together, with concrete affected behavior and minimal corrections within "
    "this scope.",
    'Return {"approved":true|false,"findings":[]}.',
)
PLAN_BALANCE = (
    "do not reject solely for missing release boilerplate.",
    "Review only the retained capability: optional types, modes or integrations may be removed before approval. "
    "Do not demand additional features.",
)


def instruction(prompt: str) -> str:
    """The text a reviewer is told, without the JSON it is asked about (the last line of the prompt)."""
    return prompt.rsplit("\n", 1)[0]


def missing(clauses, text: str) -> list[str]:
    """Clauses that are not in `text` exactly once."""
    return [clause for clause in clauses if text.count(clause) != 1]


class ReviewStrictnessTest(PromptHarness):
    def no_change_instruction(self) -> str:
        sent = self.run_no_change()
        return instruction(next(prompt for label, prompt in sent.items() if label.startswith("no-change-review")))

    def plan_instruction(self) -> str:
        return instruction(self.run_plan()["plan-review-1"])

    def test_the_no_change_review_keeps_its_strict_clauses_and_its_limits(self):
        text = self.no_change_instruction()
        self.assertEqual(missing(NO_CHANGE_STRICT, text), [])
        self.assertEqual(missing(NO_CHANGE_BALANCE, text), [])

    def test_the_plan_review_keeps_its_strict_clauses_and_its_limits(self):
        text = self.plan_instruction()
        self.assertEqual(missing(PLAN_STRICT, text), [])
        self.assertEqual(missing(PLAN_BALANCE, text), [])

    def test_a_clause_quoted_by_the_thing_under_review_does_not_count(self):
        # the plan is part of the prompt, after the instruction; a plan that contains a clause must not satisfy the test
        quoting = dict(self.plan, behavior=" ".join(NO_CHANGE_STRICT + PLAN_STRICT))
        full = self.run_plan(quoting)["plan-review-1"]
        self.assertGreater(full.count(NO_CHANGE_STRICT[0]), 0)           # it is in the prompt, in the plan
        self.assertEqual(instruction(full).count(NO_CHANGE_STRICT[0]), 0)  # but not in what the reviewer is told

    def test_the_check_notices_a_weakened_a_deleted_and_a_duplicated_clause(self):
        # a fixed baseline made of the pinned clauses, so this shows what the check can see whatever is sent today
        text = "Review this no-change assessment. " + " ".join(NO_CHANGE_STRICT + NO_CHANGE_BALANCE)
        self.assertEqual(missing(NO_CHANGE_STRICT + NO_CHANGE_BALANCE, text), [])
        strict = NO_CHANGE_STRICT[0]
        for name, changed in {
                "weakened (04's mutation)": text.replace(strict, "Accept a reason, honest uncertainty and a useful follow-up date."),
                "one word softened": text.replace("Require concrete", "Consider concrete"),
                "deleted": text.replace(strict, ""),
                "the reject clause dropped": text.replace(NO_CHANGE_STRICT[1], ""),
                "the reject clause softened": text.replace("Reject ignored", "Note ignored"),
                "duplicated": text + " " + strict}.items():
            with self.subTest(name):
                self.assertNotEqual(changed, text)
                self.assertNotEqual(missing(NO_CHANGE_STRICT, changed), [])

    def test_every_pinned_clause_is_in_the_current_prompt_exactly_once(self):
        # the list and the prompts agree on a fresh read, the guard against the list drifting from what is sent
        # the plan run first: the no-change run replaces the task the harness started with
        plan_text = self.plan_instruction()
        no_change_text = self.no_change_instruction()
        for clauses, text in ((NO_CHANGE_STRICT + NO_CHANGE_BALANCE, no_change_text), (PLAN_STRICT + PLAN_BALANCE, plan_text)):
            for clause in clauses:
                with self.subTest(clause[:50]):
                    self.assertEqual(text.count(clause), 1)


if __name__ == "__main__":
    unittest.main()
