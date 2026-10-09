"""No remote writes: exercise the fallback adapter against a recording gh stub."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/request-fallback-ci"
COMMIT = "a" * 40


class FallbackCI(unittest.TestCase):
    def run_adapter(self, *, head=COMMIT, count="0", read_exit=0, dispatch_exit=0,
                    expected=COMMIT):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stub = root / "gh"
            stub.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$TRACE"\n'
                'if [[ "$1" == api ]]; then\n'
                '  [[ "$READ_EXIT" == 0 ]] || exit "$READ_EXIT"\n'
                '  if [[ "$4" == */git/ref/heads/main ]]; then printf "%s\\n" "$HEAD";\n'
                '  else printf "%s\\n" "$COUNT"; fi\n'
                'else exit "$DISPATCH_EXIT"; fi\n'
            )
            stub.chmod(0o700)
            trace = root / "trace"
            result = subprocess.run(
                ["bash", str(SCRIPT)], capture_output=True,
                env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                     "TRACE": str(trace), "HEAD": head, "COUNT": count,
                     "READ_EXIT": str(read_exit), "DISPATCH_EXIT": str(dispatch_exit),
                     "EXPECTED_RADAR_COMMIT": expected}, timeout=5)
            calls = trace.read_text().splitlines() if trace.exists() else []
            return result.returncode, calls

    def test_missing_ci_requests_only_the_original_named_workflow(self):
        code, calls = self.run_adapter()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn(f"head_sha={COMMIT}&exclude_pull_requests=true", calls[1])
        self.assertEqual(calls[2], "workflow run ci.yml --ref main --repo noteflowai/physical-ai-radar")

    def test_existing_run_is_observed_without_dispatch_or_rerun(self):
        code, calls = self.run_adapter(count="2")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)

    def test_invalid_identity_changed_head_or_unavailable_inventory_never_dispatch(self):
        for args in ({"expected": "main"}, {"head": "b" * 40},
                     {"count": "null"}, {"read_exit": 1}):
            with self.subTest(args=args):
                code, calls = self.run_adapter(**args)
                self.assertNotEqual(code, 0)
                self.assertFalse(any(c.startswith("workflow ") for c in calls))

    def test_dispatch_failure_stops_after_one_request(self):
        code, calls = self.run_adapter(dispatch_exit=1)
        self.assertNotEqual(code, 0)
        self.assertEqual(sum(c.startswith("workflow ") for c in calls), 1)
