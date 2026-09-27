"""Release environment mismatches must be found before paid feature development."""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts import feature_preflight as preflight
from scripts.agent_pipeline import write_json

SHA = "a" * 40


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "protection_rules": [{"type": "branch_policy"}],
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        }

    def test_main_only_environment_reproduces_the_real_evalarc_blocker(self):
        errors = preflight.environment_errors(
            self.environment, [{"name": "main", "type": "branch"}], "v0.16.0")
        self.assertEqual(errors, ["Environment does not permit release tag v0.16.0"])

    def test_tag_rule_fixes_publication_without_removing_main(self):
        policies = [{"name": "main", "type": "branch"}, {"name": "v*", "type": "tag"}]
        self.assertEqual(preflight.environment_errors(self.environment, policies, "v0.16.0"), [])
        policies[1]["type"] = "branch"
        self.assertTrue(preflight.environment_errors(self.environment, policies, "v0.16.0"))

    def test_reviewers_block_unattended_publication_but_wait_timers_can_resume(self):
        self.environment["deployment_branch_policy"] = None
        self.environment["protection_rules"] = [{"type": "wait_timer", "wait_timer": 60}]
        self.assertEqual(preflight.environment_errors(self.environment, [], "v0.16.0"), [])
        self.environment["protection_rules"].append({"type": "required_reviewers", "reviewers": [{"id": 1}]})
        self.assertIn("interactive", preflight.environment_errors(self.environment, [], "v0.16.0")[0])

    def test_protected_branch_mode_does_not_assume_tag_permission(self):
        self.environment["deployment_branch_policy"] = {
            "protected_branches": True, "custom_branch_policies": False}
        self.assertTrue(preflight.environment_errors(self.environment, [], "v0.16.0"))


class ProjectTests(unittest.TestCase):
    def fixture(self, name="evalarc"):
        environment = preflight.ENVIRONMENTS.get(name)
        text = '{"version":"0.15.7"}' if name == "dsh-skills-anywhere" else '[project]\nversion = "0.15.7"\n'
        path = "package.json" if name == "dsh-skills-anywhere" else "pyproject.toml"
        records = {
            "": {"permissions": {"push": True}, "archived": False},
            "git/ref/heads/main": {"object": {"sha": SHA}},
            "actions/workflows?per_page=100": {"workflows": [
                {"path": ".github/workflows/" + f, "state": "active"}
                for f in preflight.WORKFLOWS[name]]},
            f"contents/{path}?ref={SHA}": {"encoding": "base64",
                                         "content": base64.b64encode(text.encode()).decode()},
            f"environments/{environment}": {
                "protection_rules": [{"type": "branch_policy"}],
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
            f"environments/{environment}/deployment-branch-policies?per_page=100": {
                "branch_policies": [{"name": "v*", "type": "tag"}]},
            "actions/variables/PYPI_PUBLISH_ENABLED": {"value": "true"},
        }
        return records

    def test_next_minor_uses_metadata_from_exact_observed_main(self):
        for name in preflight.ENVIRONMENTS:
            with self.subTest(repo=name):
                records, calls = self.fixture(name), []
                def api(path):
                    calls.append(path)
                    return deepcopy(records[path])
                result = preflight.check_project(name, api)
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["release_tag"], "v0.16.0")
                self.assertEqual(result["source_commit"], SHA)
                self.assertTrue(any(p.endswith("?ref=" + SHA) for p in calls))

    def test_resuming_a_merged_release_does_not_check_the_next_feature_tag(self):
        records = self.fixture()
        records["environments/pypi/deployment-branch-policies?per_page=100"]["branch_policies"] = [
            {"name": "v0.15.7", "type": "tag"}]
        result = preflight.check_project("evalarc", records.__getitem__, release_tag="v0.15.7")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["release_tag"], "v0.15.7")
        self.assertEqual(preflight.check_project("evalarc", records.__getitem__)["status"], "blocked")

    def test_disabled_workflow_and_missing_push_permission_are_actionable(self):
        records = self.fixture()
        records[""]["permissions"]["push"] = False
        records["actions/workflows?per_page=100"]["workflows"][-1]["state"] = "disabled_manually"
        result = preflight.check_project("evalarc", records.__getitem__)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("push permission" in e for e in result["errors"]))
        self.assertTrue(any("publish-pypi.yml" in e for e in result["errors"]))

    def test_robot_pypi_switch_must_match_required_distribution(self):
        records = self.fixture("robot-reel")
        records["actions/variables/PYPI_PUBLISH_ENABLED"]["value"] = "false"
        result = preflight.check_project("robot-reel", records.__getitem__)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("PYPI_PUBLISH_ENABLED", result["errors"][0])

    def test_api_timeout_is_a_recorded_failure_not_a_false_ready(self):
        def api(_):
            raise subprocess.TimeoutExpired(["gh", "api"], 12)
        result = preflight.check_project("evalarc", api)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["errors"])

    def test_github_queries_have_per_request_and_total_deadlines(self):
        with patch.object(preflight.time, "monotonic", side_effect=[100, 130, 146]), \
                patch.object(preflight.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            api = preflight.GitHub("evalarc")
            self.assertEqual(api(""), {})
            self.assertEqual(run.call_args.kwargs["timeout"], 12)
            with self.assertRaises(TimeoutError):
                api("actions/workflows")
            self.assertEqual(run.call_count, 1)


class ControllerTests(unittest.TestCase):
    def test_blocked_repo_skips_model_worker_while_another_repo_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ["develop_repos", "--date", "2026-09-27", "--state", str(root / "state"),
                    "--workspace", str(root / "workspace"), "--repo", "evalarc", "--repo", "robot-reel"]
            def readiness(name, **kwargs):
                return {"status": "blocked", "errors": ["Tag is not allowed"]} if name == "evalarc" else {
                    "status": "ready", "errors": []}
            started = []
            def execute(args, *rest):
                name = args[args.index("--repo") + 1]
                started.append(name)
                write_json(root / "state/2026-09-27" / name / "result.json",
                           {"repo": "noteflowai/" + name, "status": "published"})
                return 0
            with patch.object(sys, "argv", argv), \
                    patch.object(developer, "snapshot", return_value={"sources": []}), \
                    patch.object(developer, "publication_preflight", side_effect=readiness), \
                    patch.object(developer, "execute_stage", side_effect=execute), \
                    patch.object(developer, "ask") as ask, redirect_stdout(io.StringIO()):
                self.assertEqual(developer.main(), 1)
                ask.assert_not_called()
            self.assertEqual(started, ["robot-reel"])
            error = json.loads((root / "state/2026-09-27/evalarc/result.json").read_text())
            self.assertEqual(error["stage"], "publication-preflight")
            self.assertFalse((root / "state/tasks/evalarc/active.json").exists())

    def test_resume_passes_the_saved_release_tag_to_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "state/tasks/evalarc/active.json",
                       {"phase": "release", "release": {"tag": "v0.16.0"}})
            argv = ["develop_repos", "--date", "2026-09-27", "--state", str(root / "state"),
                    "--workspace", str(root / "workspace"), "--repo", "evalarc"]
            with patch.object(sys, "argv", argv), \
                    patch.object(developer, "snapshot", return_value={"sources": []}), \
                    patch.object(developer, "publication_preflight",
                                 return_value={"status": "blocked", "errors": ["fixture"]}) as check, \
                    patch.object(developer, "execute_stage") as execute, redirect_stdout(io.StringIO()):
                developer.main()
                check.assert_called_once_with("evalarc", release_tag="v0.16.0")
                execute.assert_not_called()
