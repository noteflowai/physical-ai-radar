"""Regression coverage for executable daily feature work and durable publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.develop_repos import apply, checkpoint, complete, develop, fit_context, parse_feature_object, requested_context, require_review, restore, validate_feature, validate_plan
from scripts.feature_policy import PROJECTS, acceptance_command, allowed, readable
from scripts.feature_runtime import execute
from scripts.agent_pipeline import REQUIRED_PR_CHECKS, finish
from scripts.feature_wordpress import SLUG, package_files, publish as publish_wordpress, version


class FeaturePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = PROJECTS["physical-ai-radar"]
        self.plan = {
            "title": "feat: filter research by source",
            "problem": "Readers cannot isolate one evidence source",
            "behavior": "Source selection returns only matching papers",
            "why_this_repo": "Evidence discovery is Radar's responsibility",
            "acceptance": ["A source filter excludes unmatched papers"],
            "read_paths": ["pairadar/model.py", "tests/test_filter.py"],
            "test_path": "tests/test_filter.py", "test_runner": "unittest",
            "gpu_test_path": None,
        }

    def test_five_specialists_and_wordpress_required_checks(self):
        self.assertEqual(len(PROJECTS), 5)
        self.assertIn("ai-chat-for-amazon-bedrock", PROJECTS)
        checks = REQUIRED_PR_CHECKS["noteflowai/ai-chat-for-amazon-bedrock"]
        self.assertIn("Official Plugin Check", checks)
        self.assertIn("Release gate on PHP 7.4", checks)
        self.assertIn("Release gate on PHP 8.3", checks)

    def test_functional_source_and_gpu_test_are_allowed(self):
        self.assertTrue(allowed("robot_reel/replay.py", PROJECTS["robot-reel"]))
        self.assertTrue(allowed("includes/class-report-search.php",
                                PROJECTS["ai-chat-for-amazon-bedrock"]))
        self.plan.update(gpu_test_path="tests/test_gpu_filter.py",
                         gpu_reason="Compare a batched tensor filter against the CPU result")
        validate_plan(self.plan, self.config)

    def test_path_traversal_controls_and_dependency_changes_stay_closed(self):
        for path in ["../outside.py", "/tmp/out.py", "tests/../../outside.py",
                     "tests/.secret.py", "scripts/develop_repos.py", ".github/workflows/ci.yml",
                     "pyproject.toml", "tests/AGENTS.md", "tests/foo/../bar.py"]:
            with self.subTest(path=path):
                self.assertFalse(allowed(path, self.config))
        self.assertTrue(readable("pyproject.toml", self.config))
        self.assertFalse(allowed("pyproject.toml", self.config))

    def test_acceptance_cannot_be_arbitrary_shell(self):
        for runner in ["bash", "sh", "python -c"]:
            with self.assertRaises(ValueError):
                acceptance_command({**self.plan, "test_runner": runner}, self.config)
        with self.assertRaises(ValueError):
            acceptance_command({**self.plan, "test_path": "pairadar/model.py"}, self.config)

    def test_acceptance_is_discoverable_as_a_future_regression(self):
        for path in ["tests/check_filter.py", "tests/nested/test_filter.py"]:
            with self.assertRaisesRegex(ValueError, "future regression discovery"):
                acceptance_command({**self.plan, "test_path": path}, self.config)
        robot = PROJECTS["robot-reel"]
        plan = {"test_path": "tests/new-feature.cjs", "test_runner": "node"}
        with self.assertRaises(ValueError):
            acceptance_command(plan, robot)
        plan["test_path"] = "tests/new-feature.test.cjs"
        self.assertEqual(acceptance_command(plan, robot), ["node", "--test", plan["test_path"]])
        self.assertIn(["node", "--test"], robot["checks"])

    def proposal(self):
        return {"edits": [
            {"path": "pairadar/model.py", "old": "return items", "new": "return items[:1]"},
            {"path": "tests/test_filter.py", "old": None, "new": "assert True\n"},
            {"path": "docs/filter.md", "old": None, "new": "Filter usage\n"},
        ]}

    def test_all_edits_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pairadar").mkdir()
            file = root / "pairadar/model.py"
            file.write_text("return items")
            proposal = self.proposal()
            proposal["edits"].append({"path": "../outside.py", "old": None, "new": "bad"})
            with self.assertRaises(ValueError):
                apply(root, proposal, self.config, self.plan)
            self.assertEqual(file.read_text(), "return items")
            self.assertFalse((root / "tests/test_filter.py").exists())

    def test_feature_requires_product_test_and_documentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pairadar").mkdir()
            (root / "pairadar/model.py").write_text("return items")
            for omitted in range(3):
                proposal = self.proposal()
                proposal["edits"].pop(omitted)
                with self.assertRaises(ValueError):
                    apply(root, proposal, self.config, self.plan)
            changed = apply(root, self.proposal(), self.config, self.plan)
            self.assertEqual(len(changed), 3)
            self.assertTrue((root / "tests/test_filter.py").exists())

    def test_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "pairadar").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                apply(root, self.proposal(), self.config, self.plan)

    def test_existing_regression_tests_cannot_be_weakened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pairadar").mkdir()
            (root / "pairadar/model.py").write_text("return items")
            (root / "tests").mkdir()
            (root / "tests/test_filter.py").write_text("assert important_behavior()")
            proposal = self.proposal()
            proposal["edits"][1] = {"path": "tests/test_filter.py",
                                    "old": "assert important_behavior()", "new": "pass"}
            with self.assertRaises(ValueError):
                apply(root, proposal, self.config, self.plan)

    def test_review_must_be_explicit_and_unresolved_findings_empty(self):
        for result in [{}, {"approved": True}, {"approved": "true", "findings": []},
                       {"approved": True, "findings": ["broken behavior"]}]:
            with self.assertRaises(ValueError):
                require_review(result)
        require_review({"approved": True, "findings": []})

    def test_author_cli_narration_does_not_discard_one_valid_object(self):
        text = '> I need the export handler first.\n> {"need_files":["includes/export.php"]}'
        self.assertEqual(parse_feature_object(text), {"need_files": ["includes/export.php"]})
        for text in ['{"edits": []}\n{"edits": []}', '{"broken":\n{"edits": []}', 'no object']:
            with self.assertRaises(ValueError):
                parse_feature_object(text)

    def test_path_only_source_request_and_missing_file_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pairadar").mkdir()
            (root / "pairadar/model.py").write_text("behavior = 1\n")
            self.assertEqual(requested_context(root, {"path": "pairadar/model.py"}, self.config),
                             ("pairadar/model.py", "behavior = 1\n"))
            self.assertEqual(requested_context(root, {"path": "pairadar/missing.py"}, self.config),
                             ("pairadar/missing.py", "<file does not exist>"))

    @patch("scripts.develop_repos.command", return_value="tests/test_filter.py\n")
    def test_directory_search_returns_tracked_product_locations(self, command):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests/test_filter.py").write_text("def test_insights():\n    pass\n")
            key, value = requested_context(root, {"path": "tests", "search": "insights"}, self.config)
            self.assertEqual(key, "tests [search insights]")
            self.assertIn("tests/test_filter.py:1: def test_insights()", value)
            with self.assertRaises(ValueError):
                requested_context(root, {"path": "../tests", "search": "insights"}, self.config)

    def test_context_budget_counts_json_escaping_and_keeps_recent_reads(self):
        files = {"old": '"' * 100, "recent": "useful source"}
        fit_context(files, 100)
        self.assertEqual(files, {"recent": "useful source"})

    @patch("scripts.develop_repos.calendar_day", return_value="2026-09-27")
    def test_allowance_persisted_before_active_transaction_is_removed(self, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "active.json"
            active.write_text("{}")
            state = root / "task"
            result = {"status": "published", "feature_id": "yesterday-feature"}
            complete(result, state, active, root, "evalarc", "2026-09-26")
            self.assertFalse(active.exists())
            saved = json.loads((root / "2026-09-26/evalarc/result.json").read_text())
            self.assertEqual(saved["feature_id"], "yesterday-feature")
            self.assertEqual(saved["completed_on"], "2026-09-26")
            self.assertEqual(saved["released_on"], "2026-09-27")
            self.assertTrue((root / "allowances/2026-09-27/evalarc.json").is_file())

    @patch("scripts.develop_repos.calendar_day", return_value="2026-09-27")
    @patch("scripts.develop_repos.checkout")
    def test_catchup_release_consumes_next_evenings_allowance(self, checkout, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "active.json"
            active.write_text("{}")
            result = {"status": "published", "feature_id": "overnight-feature"}
            complete(result, root / "task", active, root, "evalarc", "2026-09-26")
            later = develop("evalarc", {}, root / "workspace", root, "2026-09-27")
            self.assertTrue(later["daily_limit_reused"])
            self.assertEqual(later["feature_id"], "overnight-feature")
            checkout.assert_not_called()

    @patch("scripts.develop_repos.calendar_day", return_value="2026-09-27")
    @patch("scripts.develop_repos.checkout")
    def test_finished_active_transaction_does_not_consume_a_second_day(self, checkout, calendar):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "tasks/evalarc"
            (directory / "previous").mkdir(parents=True)
            (directory / "active.json").write_text(json.dumps({"id": "previous"}))
            result = {"repo": "noteflowai/evalarc", "status": "published",
                      "feature_id": "previous", "completed_on": "2026-09-26"}
            (directory / "previous/complete.json").write_text(json.dumps(result))
            checkout.side_effect = RuntimeError("new task starts")
            with self.assertRaisesRegex(RuntimeError, "new task starts"):
                develop("evalarc", {}, root / "workspace", root, "2026-09-27")
            self.assertFalse((directory / "active.json").exists())
            self.assertTrue((root / "2026-09-26/evalarc/result.json").exists())
            self.assertFalse((root / "2026-09-27/evalarc/result.json").exists())

    @patch("scripts.develop_repos.execute", return_value={"exit_code": 1, "executed_commands": []})
    @patch("scripts.develop_repos.archive")
    @patch("scripts.develop_repos.image_for", return_value="fixed-image")
    def test_harness_crash_is_not_a_failing_acceptance_test(self, image, archive, execute):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = {"plan": self.plan, "base": "base", "head": "head", "changed": []}
            with self.assertRaisesRegex(ValueError, "Acceptance must fail"):
                validate_feature(root, task, self.config, root, root)
            self.assertEqual(execute.call_count, 1)

    def test_unpublished_commit_restores_from_bundle_after_clone_loss(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp)
            source, replacement, state = top / "source", top / "replacement", top / "state"
            source.mkdir()
            state.mkdir()

            def git(*args, cwd=source):
                return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                               stderr=subprocess.DEVNULL).strip()

            git("init", "-q")
            git("config", "user.name", "Feature test")
            git("config", "user.email", "test@example.invalid")
            (source / "module.py").write_text("value = 1\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            git("clone", "-q", str(source), str(replacement))
            (source / "module.py").write_text("value = 2\n")
            git("commit", "-qam", "feature")
            head = git("rev-parse", "HEAD")
            task = {"head": head, "base": base, "branch": "automation/feature-fixture"}
            checkpoint(source, task, state, state / "active.json")
            self.assertTrue((state / task["bundle"]).exists())
            restore(replacement, task, state)
            self.assertEqual(git("rev-parse", "HEAD", cwd=replacement), head)
            self.assertEqual((replacement / "module.py").read_text(), "value = 2\n")

    @patch("scripts.feature_runtime.subprocess.run")
    def test_container_timeout_cleans_up_and_retains_failure_receipt(self, run):
        import subprocess
        run.side_effect = [subprocess.TimeoutExpired("docker", 10), None]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = execute(root, "sha256:fixed", [["python", "tests/test_x.py"]],
                             root / "state", "timeout", timeout=10)
            self.assertEqual(record["exit_code"], 124)
            argv = run.call_args_list[0].args[0]
            self.assertIn("--network", argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn("--cap-drop", argv)
            self.assertFalse(any("docker.sock" in a for a in argv))
            self.assertEqual(run.call_args_list[1].args[0][:3], ["docker", "rm", "-f"])


class WordPressPackageTests(unittest.TestCase):
    @patch("scripts.develop_repos.image_for")
    @patch("scripts.develop_repos.command")
    def test_plugin_version_must_advance_before_validation_or_merge(self, command, image):
        command.side_effect = ["Stable tag: 1.46.0\n", "<?php\n * Version: 1.46.0\n"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readme.txt").write_text("Stable tag: 1.46.0\n")
            (root / (SLUG + ".php")).write_text("<?php\n * Version: 1.46.0\n")
            task = {"repo": "noteflowai/" + SLUG, "plan": {}, "base": "base"}
            with self.assertRaisesRegex(ValueError, "1.47.0"):
                validate_feature(root, task, PROJECTS[SLUG], root, root)
            image.assert_not_called()

    @patch("scripts.agent_pipeline.verify_deployment", return_value=([{"conclusion": "success"}], []))
    @patch("scripts.agent_pipeline.wait_pr")
    def test_merged_source_is_not_reported_as_published_plugin(self, wait_pr, verify):
        wait_pr.return_value = {"state": "MERGED", "mergeCommit": {"oid": "merge"},
                                "url": "https://example.invalid/pr"}
        with tempfile.TemporaryDirectory() as tmp:
            result = finish("noteflowai/ai-chat-for-amazon-bedrock", 1, "reviewed",
                            ["Quality gate"], [], Path(tmp) / "receipt.json")
            self.assertEqual(result["status"], "source-verified")
            self.assertEqual(result["merge_commit"], "merge")
            with self.assertRaises(ValueError):
                finish("noteflowai/robot-reel", 1, "reviewed", ["Check"], [],
                       Path(tmp) / "other.json")

    def package(self, root, extra=None):
        import zipfile
        target = root / "plugin.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr(SLUG + "/readme.txt", "Stable tag: 1.47.0\n")
            archive.writestr(SLUG + "/" + SLUG + ".php", "<?php\n * Version: 1.47.0\n")
            if extra:
                archive.writestr(*extra)
        return target

    def test_version_and_content_identity_ignore_zip_container_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = package_files(self.package(Path(tmp)))
            self.assertEqual(version(files), "1.47.0")
            self.assertEqual(set(files), {"readme.txt", SLUG + ".php"})

    def test_archive_traversal_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = self.package(Path(tmp), (SLUG + "/../secret", "bad"))
            with self.assertRaises(ValueError):
                package_files(archive)

    @patch("scripts.feature_wordpress.remote_files")
    @patch("scripts.feature_wordpress.subprocess.run")
    def test_existing_tag_with_different_content_is_never_overwritten(self, run, remote):
        from types import SimpleNamespace
        run.return_value = SimpleNamespace(returncode=0)
        remote.return_value = {"readme.txt": b"different"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "different contents"):
                publish_wordpress(self.package(root), root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["svn", "info"])

    @patch("scripts.feature_wordpress.command", return_value="Stable tag: 1.47.0\n")
    @patch("scripts.feature_wordpress.subprocess.run")
    def test_nonincreasing_version_cannot_publish(self, run, command):
        from types import SimpleNamespace
        run.return_value = SimpleNamespace(returncode=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "advance"):
                publish_wordpress(self.package(root), root)
        self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
