"""Publisher credentials stay outside feature code, command arguments and logs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.feature_wordpress import commit_svn


class WordPressCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.token = self.root / "token"
        self.token.write_text("test-only-secret\n")
        self.token.chmod(0o600)
        self.config = self.root / "wordpress.json"
        self.config.write_text(json.dumps({"username": "publisher", "password_file": str(self.token)}))
        self.args = ["svnmucc", "--non-interactive", "-r", "123", "mkdir", "https://example.invalid/tag"]

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_private_token_reaches_only_stdin_and_is_not_cached(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "r124 committed", "")
        self.assertEqual(commit_svn(self.args, self.config), "r124 committed")
        args = run.call_args.args[0]
        self.assertNotIn("test-only-secret", " ".join(args))
        self.assertEqual(run.call_args.kwargs["input"], "test-only-secret\n")
        self.assertIn("--password-from-stdin", args)
        self.assertIn("--no-auth-cache", args)
        self.assertIn("publisher", args)

    @patch("scripts.feature_wordpress.command", return_value="cached auth used")
    def test_existing_cache_remains_supported_without_config(self, command):
        self.assertEqual(commit_svn(self.args, self.root / "absent.json"), "cached auth used")
        command.assert_called_once_with(self.args, timeout=300)

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_credential_is_removed_from_error_and_success_output(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "echo: test-only-secret denied")
        with self.assertRaises(RuntimeError) as error:
            commit_svn(self.args, self.config)
        self.assertNotIn("test-only-secret", str(error.exception))
        self.assertIn("[REDACTED]", str(error.exception))
        run.return_value = subprocess.CompletedProcess([], 0, "test-only-secret", "")
        self.assertEqual(commit_svn(self.args, self.config), "[REDACTED]")

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_timeout_does_not_expose_captured_output(self, run):
        run.side_effect = subprocess.TimeoutExpired(self.args, 300, output="test-only-secret")
        with self.assertRaisesRegex(RuntimeError, "resume") as error:
            commit_svn(self.args, self.config)
        self.assertNotIn("test-only-secret", str(error.exception))

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_shared_file_symlink_and_fifo_are_rejected_before_starting_svn(self, run):
        self.token.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            commit_svn(self.args, self.config)
        self.token.unlink()
        target = self.root / "target"
        target.write_text("test-only-secret")
        target.chmod(0o600)
        self.token.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "private"):
            commit_svn(self.args, self.config)
        self.token.unlink()
        os.mkfifo(self.token, 0o600)
        with self.assertRaisesRegex(ValueError, "private"):
            commit_svn(self.args, self.config)
        run.assert_not_called()

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_missing_empty_multiline_and_large_tokens_fail_closed(self, run):
        for contents in ["", "line1\nline2", "x" * 16385]:
            with self.subTest(size=len(contents)):
                self.token.write_text(contents)
                with self.assertRaisesRegex(ValueError, "private"):
                    commit_svn(self.args, self.config)
        self.token.unlink()
        with self.assertRaisesRegex(ValueError, "private"):
            commit_svn(self.args, self.config)
        run.assert_not_called()

    @patch("scripts.feature_wordpress.subprocess.run")
    def test_bad_config_never_falls_back_to_another_identity(self, run):
        for settings in [
            {"username": "publisher", "password": "test-only-secret"},
            {"username": "publisher", "password_file": "relative/token"},
            {"username": "", "password_file": str(self.token)},
            {"username": "publisher\ninjected", "password_file": str(self.token)},
            [],
        ]:
            self.config.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, "config") as error:
                commit_svn(self.args, self.config)
            self.assertNotIn("test-only-secret", str(error.exception))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
