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
    """The nightly script must diagnose the agent's own most likely mistake."""

    def test_the_draft_is_parsed_before_the_suite_sees_it(self):
        script = (ROOT/"scripts/draft_daily_notes.sh").read_text()
        gate = script.index("the draft is not valid JSON")
        suite = script.index("python3 -m unittest discover")
        self.assertLess(gate, suite, "parse the agent's JSON before running the suite")
        self.assertIn("restore_tree", script[gate:gate + 200])


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
