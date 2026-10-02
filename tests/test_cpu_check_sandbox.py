from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feature_runtime


class CpuCheckSandboxTests(unittest.TestCase):
    def run_sandbox(self, **options):
        commands = []

        def invoke(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
                feature_runtime.subprocess, "run", side_effect=invoke):
            root = Path(temporary)
            snapshot = root / "source"
            snapshot.mkdir()
            result = feature_runtime.execute(snapshot, "sha256:" + "a" * 64,
                                             [["python3", "-V"]], root / "state",
                                             "probe", **options)
        return next(argv for argv in commands if argv[:2] == ["docker", "run"]), result

    def test_opt_in_cpu_check_has_bounded_tmpfs_and_read_only_root(self):
        argv, result = self.run_sandbox(workspace_bytes=128 * 1024**2, cpus=1,
                                        memory_bytes=2 * 1024**3)
        self.assertIn("--read-only", argv)
        self.assertEqual(argv[argv.index("--log-driver") + 1], "none")
        self.assertEqual(argv[argv.index("--memory") + 1], str(2 * 1024**3))
        self.assertEqual(argv[argv.index("--cpus") + 1], "1")
        self.assertNotIn("--gpus", argv)
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertTrue(any(x.startswith("/workspace:") and "size=134217728" in x for x in argv))
        self.assertTrue(any(x.startswith("/tmp:") and "size=134217728" in x for x in argv))
        self.assertFalse(result["gpu"])

    def test_existing_callers_keep_resource_defaults(self):
        argv, _ = self.run_sandbox()
        self.assertEqual(argv[argv.index("--memory") + 1], "24g")
        self.assertNotIn("--read-only", argv)
        self.assertNotIn("--tmpfs", argv)

    def test_invalid_resource_requests_fail_before_launch(self):
        for options in ({"cpus": 0}, {"cpus": True}, {"memory_bytes": 0},
                        {"workspace_bytes": 1}, {"workspace_bytes": 2 * 1024**3},
                        {"workspace_bytes": 128 * 1024**2, "gpu": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(feature_runtime.subprocess, "run") as launch:
                root = Path(temporary)
                with self.assertRaises(ValueError):
                    feature_runtime.execute(root, "image", [], root / "state", "invalid", **options)
                launch.assert_not_called()
