"""Every unattended role honors the selected model without silent substitution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.agent_model import MODEL, EFFORT
from scripts.develop_repos import ask
from scripts.improve_repos import model_json


class PinnedAgentModelTests(unittest.TestCase):
    @patch("scripts.improve_repos.command")
    @patch("scripts.improve_repos.call_agent")
    def test_author_and_review_use_fable_high_in_fresh_tool_free_calls(self, call, command):
        def answer(args, output, cwd):
            self.assertNotIn("--resume", args)
            self.assertEqual(args[args.index("--model") + 1], "claude-fable-5.1")
            self.assertEqual(args[args.index("--effort") + 1], "high")
            policy = json.loads((cwd / ".kiro/agents/repo-maintainer.json").read_text())
            self.assertEqual(policy["tools"], [])
            self.assertEqual(policy["allowedTools"], [])
            output.with_suffix(".log.stdout").write_text('{"approved":true,"findings":[]}')
        call.side_effect = answer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for label, review in [("author", False), ("review", True)]:
                result = ask("Isolated probe", root, label, review=review)
                self.assertTrue(result["approved"])
                receipt = json.loads((root / (label + ".json")).read_text())
                self.assertEqual(receipt["requested_model"], MODEL)
                self.assertEqual(receipt["effort"], EFFORT)
                self.assertEqual(receipt["prior_failures"], [])
        self.assertEqual(call.call_count, 2)

    @patch("scripts.improve_repos.command")
    @patch("scripts.improve_repos.call_agent", side_effect=RuntimeError("temporarily unavailable"))
    def test_unavailable_fable_does_not_invoke_another_model(self, call, command):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
                model_json("probe", Path(tmp), "probe")
            self.assertFalse((Path(tmp) / "probe.json").exists())
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0][3], MODEL)

    @patch("scripts.improve_repos.command")
    @patch("scripts.improve_repos.call_agent")
    def test_invalid_json_also_retries_only_via_the_controller(self, call, command):
        def answer(args, output, cwd):
            output.with_suffix(".log.stdout").write_text("invalid JSON")
        call.side_effect = answer
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                model_json("probe", Path(tmp), "probe")
        call.assert_called_once()

    @patch("scripts.improve_repos.call_agent")
    def test_stale_caller_cannot_select_an_old_model(self, call):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "pinned to claude-fable-5.1"):
                model_json("probe", Path(tmp), "probe", "claude-sonnet-5")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
