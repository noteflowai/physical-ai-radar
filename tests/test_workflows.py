"""Untrusted triggers must never reach the self-hosted runner.

This repository is public, and a self-hosted runner is a machine someone owns. A
workflow that runs on `pull_request` runs code from the pull request's branch, so
pointing such a workflow at `runs-on: self-hosted` hands the machine to anyone who
can open a pull request. `workflow_dispatch` and `schedule` can only be started by
someone with write access, so those are the safe triggers for that runner.

The check is textual on purpose: the pipeline is standard library only, so there is
no YAML parser available. It reads the top-level `on:` block and looks for
`self-hosted` anywhere in the file, which is the conservative direction -- a file
mentioning self-hosted in a comment still has to justify its triggers.
"""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT/".github"/"workflows"
# Anything an outside contributor or a drive-by event can start.
UNTRUSTED = {
    "pull_request", "pull_request_target", "issue_comment", "issues", "fork",
    "watch", "discussion", "discussion_comment", "public", "workflow_run",
    "repository_dispatch", "push",
}


def triggers(text: str) -> set[str]:
    """Top-level keys of a workflow's `on:` block."""
    lines = text.splitlines()
    found: set[str] = set()
    inside = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            stripped = line.split(":", 1)[0].strip()
            if inside:
                break
            # `on:` may be a block or inline: `on: [push]` / `on: workflow_dispatch`
            if stripped in {"on", '"on"', "'on'", "True", "true"}:
                inside = True
                _, _, rest = line.partition(":")
                rest = rest.strip().strip("[]")
                if rest:
                    found.update(part.strip().strip("'\"") for part in rest.split(",") if part.strip())
                    return found
            continue
        if inside and line.startswith(("  ", "\t")) and not line.startswith(("    ", "\t\t")):
            key = line.strip().split(":", 1)[0].strip("- '\"")
            if key:
                found.add(key)
    return found


class WorkflowSafetyTest(unittest.TestCase):
    def test_self_hosted_workflows_only_run_on_trusted_triggers(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "self-hosted" not in text:
                continue
            unsafe = triggers(text) & UNTRUSTED
            self.assertEqual(
                unsafe, set(),
                f"{path.name} runs on the self-hosted runner but can be triggered by "
                f"{sorted(unsafe)}; that hands the machine to anyone who can cause that event",
            )

    def test_every_workflow_declares_its_triggers(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            self.assertTrue(triggers(path.read_text(encoding="utf-8")),
                            f"{path.name}: no triggers parsed, so the check above cannot protect it")

    def test_nothing_trusts_every_tool(self) -> None:
        # Measured on kiro-cli 2.21.2: --trust-all-tools bypasses the agent's own
        # write allowedPaths/deniedPaths, which is the only enforced boundary here.
        # The agents now run from scripts rather than workflows, so check both.
        candidates = sorted(WORKFLOWS.glob("*.yml")) + sorted((ROOT/"scripts").glob("*.sh"))
        self.assertGreaterEqual(len(candidates), 3, "nothing was scanned")
        for path in candidates:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                self.assertNotIn("--trust-all-tools", line,
                                 f"{path.name}:{number} would run an agent without path limits")

    def test_the_trigger_parser_reads_both_forms(self) -> None:
        block = "name: x\non:\n  pull_request:\n  workflow_dispatch:\njobs:\n  a:\n    runs-on: [self-hosted]\n"
        inline = "name: x\non: [push, workflow_dispatch]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
        scalar = "name: x\non: workflow_dispatch\njobs:\n  a:\n    runs-on: [self-hosted, tokyo]\n"
        nested = ("name: x\non:\n  schedule:\n    - cron: \"30 1 * * *\"\n  workflow_dispatch:\n"
                  "    inputs:\n      force:\n        default: false\njobs:\n  a:\n    runs-on: [self-hosted]\n")
        self.assertEqual(triggers(block), {"pull_request", "workflow_dispatch"})
        self.assertEqual(triggers(inline), {"push", "workflow_dispatch"})
        self.assertEqual(triggers(scalar), {"workflow_dispatch"})
        self.assertEqual(triggers(nested), {"schedule", "workflow_dispatch"},
                         "nested input keys must not be read as triggers")

    def test_the_check_would_catch_a_pull_request_on_self_hosted(self) -> None:
        # The failure this file exists to prevent.
        offending = "name: x\non:\n  pull_request:\njobs:\n  a:\n    runs-on: [self-hosted, tokyo]\n"
        self.assertTrue(triggers(offending) & UNTRUSTED)


class DraftGateTests(unittest.TestCase):
    """The nightly script hands the agent its own mistakes before anything else judges them."""

    SCRIPT = (ROOT/"scripts/draft_daily_notes.sh").read_text(encoding="utf-8")

    def test_the_validator_runs_before_the_suite_and_feeds_back_to_the_drafter(self):
        gate = self.SCRIPT.index('python3 -m pairadar.notes check "$DAY"')
        self.assertLess(gate, self.SCRIPT.index("python3 -m unittest discover"),
                        "the suite's traceback is no instruction a model can act on")
        correction = self.SCRIPT[gate:self.SCRIPT.index("\n}", gate)]
        self.assertIn("FIX_ROUNDS", correction, "corrections must be bounded")
        self.assertIn("${problems}", correction, "the findings are the drafter's next instruction")

    def test_the_reviewer_is_not_the_drafter(self):
        review = self.SCRIPT[self.SCRIPT.index('ask_first "$REVIEW"'):]
        self.assertTrue(review.startswith('ask_first "$REVIEW" "$REVIEW_MODELS" "$DRAFTERS"'),
                        "a model must not approve its own draft")
        self.assertIn("REVIEW_ROUNDS", review, "revisions after a rejection must be bounded")

    def test_the_script_records_the_models_that_ran(self):
        stamp = self.SCRIPT.rindex("python3 -m pairadar.notes stamp")
        self.assertIn('--reviewer-model "$REVIEW_MODEL"', self.SCRIPT[stamp:stamp + 200])
        self.assertLess(self.SCRIPT.index("python3 -m pairadar.notes stamp"),
                        self.SCRIPT.index("python3 -m pairadar --rerender"),
                        "the pages name the drafter, so stamp before rebuilding them")

    def test_the_draft_merges_only_on_a_pass_of_the_commit_it_pushed(self):
        # "no checks reported" once fell through the old case statement as green.
        gate = self.SCRIPT[self.SCRIPT.index("checks_state()"):]
        self.assertIn('if length == 0 then "none"', gate)
        self.assertIn('if [ "$STATE" != "pass" ]', gate)
        self.assertIn('gh pr merge "$PR" --merge --match-head-commit "$HEAD_SHA"', gate)
        self.assertEqual(self.SCRIPT.count("gh pr merge"), 1)


class AgentBoundaryTests(unittest.TestCase):
    """The READMEs promise the agents cannot reach the network. Keep that true."""

    FORBIDDEN = {"shell", "execute_bash", "web_fetch", "web_search", "use_aws"}

    def test_the_drafting_agents_declare_no_shell_and_no_network(self):
        for name in ("radar-analyst", "radar-reviewer"):
            config = json.loads((ROOT/".kiro/agents"/f"{name}.json").read_text())
            for field in ("tools", "allowedTools"):
                declared = set(config.get(field) or [])
                self.assertEqual(declared & self.FORBIDDEN, set(),
                                 f"{name}.{field}: the source boundary is enforced here, "
                                 f"not only described in the README")


class NightlyJobTests(unittest.TestCase):
    """The unattended jobs share one clone and run without anyone watching."""

    SCRIPTS = sorted((ROOT/"scripts").glob("*.sh"))

    def test_every_job_takes_the_shared_lock(self):
        # Each job resets the clone hard; two at once destroy each other's work.
        self.assertGreaterEqual(len(self.SCRIPTS), 3, "nothing was scanned")
        for path in self.SCRIPTS + [ROOT/"scripts"/"radar-run"]:
            with self.subTest(script=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("radar.lock", text)
                self.assertRegex(text, r"flock (-n|-w \S+) 9")

    def test_every_model_call_is_bounded(self):
        for path in self.SCRIPTS:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "kiro-cli chat" in line and not line.lstrip().startswith("#"):
                    self.assertIn("timeout", line, f"{path.name}:{number} calls a model with no time limit")

    def test_every_model_call_names_its_model_after_checking_its_agent(self):
        # The machine's default model was unavailable on 09-24 and 09-25; relying on it
        # lost both nights.
        for path in self.SCRIPTS:
            text = path.read_text(encoding="utf-8")
            calls = re.findall(r'^.*(?<![\w-])ask "\$\w+".*$', text, re.M)
            if "kiro-cli chat" not in text:
                continue
            with self.subTest(script=path.name):
                self.assertTrue(calls, "no model call found")
                for call in calls:
                    self.assertIn('--model "$model"', call, "only ask_first calls ask, with a model")
                self.assertLess(text.index("kiro-cli agent validate"), text.index("ask_first \""),
                                "validate the agent before spending a model call")

    def test_a_published_day_is_not_published_again(self):
        script = (ROOT/"scripts"/"publish_daily.sh").read_text(encoding="utf-8")
        gate = script.index('"$(published_day)" = "$DAY"')
        self.assertLess(gate, script.index("python3 -m pairadar --limit"), "check before fetching")

    def test_the_fallback_wakes_on_its_own_and_only_acts_on_a_missed_day(self):
        text = (WORKFLOWS/"daily.yml").read_text(encoding="utf-8")
        self.assertIn("schedule", triggers(text))
        steps = text.split("- name: ")[1:]
        gated = [step.splitlines()[0] for step in steps if "steps.fresh.outputs.run == 'true'" in step]
        self.assertIn("Generate today's radar", gated)
        self.assertIn("Commit when something changed", gated)
