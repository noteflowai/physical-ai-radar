"""Original container identity must survive uncertainty and block replay."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import feature_runtime as runtime


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.check_id = "a" * 64
        self.state = self.root / self.check_id
        self.state.mkdir()
        self.image = "sha256:" + "b" * 64
        self.provider = "c" * 40
        (self.state / "started.json").write_text(json.dumps({"schema_version": 1,
            "check_id": self.check_id, "provider_commit": self.provider, "image": self.image}))
        self.identifier = "d" * 64
        self.calls = []
        self.rm_code = 0
        self.list_code = 0
        self.list_stdout = ""
        self.start_signal = False
        self.duplicate_signal = False
        self.create_failure = False
        self.created_id = self.identifier
        self.timeout = False
        self.authority_calls = 0
        self.fail_authority_on = None

    def check_authority(self, *args):
        self.authority_calls += 1
        if self.fail_authority_on == self.authority_calls:
            raise runtime.CleanupUnconfirmed("injected authority failure")

    def docker_run(self, argv, **kwargs):
        self.calls.append(argv)
        action = argv[1]
        if action == "create":
            initial = json.loads((self.state / "execution/lifecycle.json").read_text())
            self.assertEqual(initial["state"], "started")
            self.assertEqual(initial["check_id"], self.check_id)
            self.assertEqual(initial["provider_commit"], self.provider)
            self.assertEqual(initial["container_name"], argv[argv.index("--name") + 1])
            if self.create_failure:
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 0, self.created_id + "\n", "")
        if action == "start":
            self.assertEqual(argv[-1], self.identifier)
            record = json.loads((self.state / "execution/lifecycle.json").read_text())
            self.assertEqual(record["container_id"], self.identifier)
            kwargs["stdout"].write("execution " + self.identifier + "\n")
            if self.start_signal:
                os.kill(os.getpid(), signal.SIGTERM)
            if self.timeout:
                raise subprocess.TimeoutExpired(argv, 2)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "rm":
            self.assertEqual(argv[-1], self.identifier)
            if self.duplicate_signal:
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
            return subprocess.CompletedProcess(argv, self.rm_code, "", "injected" if self.rm_code else "")
        if action == "container":
            if self.list_code and kwargs.get("check"):
                raise subprocess.CalledProcessError(self.list_code, argv)
            return subprocess.CompletedProcess(argv, self.list_code, self.list_stdout, "")
        raise AssertionError(argv)

    def execute(self):
        authority = {"daemon_id": "fixture", "socket": "/test", "socket_device": 1, "socket_inode": 2}
        with patch.object(runtime, "_docker_authority", return_value=(["docker"], {}, authority)), \
             patch.object(runtime, "_check_authority", side_effect=self.check_authority), \
             patch.object(runtime.subprocess, "run", side_effect=self.docker_run):
            return runtime.execute(self.snapshot, self.image, [["true"]], self.state,
                                   "execution", timeout=2, workspace_bytes=128 * 1024**2, cpus=1)

    def test_timeout_receipt_only_after_exact_removal_and_empty_readback(self):
        self.timeout = True
        result = self.execute()
        self.assertEqual(result["exit_code"], 124)
        self.assertEqual(result["cleanup"]["state"], "confirmed")
        self.assertEqual([a[1] for a in self.calls], ["create", "start", "rm", "container"])
        self.assertTrue((self.state / "execution/receipt.json").exists())
        runtime.require_resolved_execution(self.state, "execution")

    def test_cleanup_errors_and_unreadable_authority_do_not_write_terminal_or_allow_replay(self):
        for mode in ("rm", "readback", "present", "authority", "post-rm-authority", "post-readback-authority"):
            with self.subTest(mode=mode):
                self.setUp()
                self.timeout = True
                if mode == "rm": self.rm_code = 1
                if mode == "readback": self.list_code = 1
                if mode == "present": self.list_stdout = self.identifier + "\n"
                if mode == "authority": self.fail_authority_on = 3
                if mode == "post-rm-authority": self.fail_authority_on = 4
                if mode == "post-readback-authority": self.fail_authority_on = 5
                with self.assertRaises(runtime.CleanupUnconfirmed):
                    self.execute()
                self.assertFalse((self.state / "execution/receipt.json").exists())
                record = json.loads((self.state / "execution/lifecycle.json").read_text())
                self.assertEqual(record["state"], "cleanup-unconfirmed")
                self.assertEqual(record["container_id"], self.identifier)
                if mode == "authority":
                    self.assertNotIn("rm", [call[1] for call in self.calls])
                if mode == "post-readback-authority":
                    self.assertEqual([call[1] for call in self.calls], ["create", "start", "rm", "container"])
                    self.assertEqual(self.authority_calls, 5)
                with self.assertRaises(runtime.CleanupUnconfirmed):
                    runtime.require_resolved_execution(self.state, "execution")
                before = len(self.calls)
                with self.assertRaises(runtime.CleanupUnconfirmed):
                    self.execute()
                self.assertEqual(len(self.calls), before)

    def test_bound_image_mismatch_is_rejected_before_docker(self):
        (self.state / "started.json").write_text(json.dumps({"schema_version": 1,
            "check_id": self.check_id, "provider_commit": self.provider, "image": "sha256:" + "e" * 64}))
        with self.assertRaises(runtime.CleanupUnconfirmed):
            self.execute()
        self.assertFalse(self.calls)
        self.assertFalse((self.state / "execution/receipt.json").exists())

    def test_unconfirmed_create_keeps_original_name_and_forbids_replay(self):
        self.create_failure = True
        with self.assertRaises(runtime.CleanupUnconfirmed) as caught: self.execute()
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        record = json.loads((self.state / "execution/lifecycle.json").read_text())
        self.assertEqual(record["state"], "cleanup-unconfirmed")
        self.assertIsNone(record["container_id"])
        self.assertEqual(record["container_name"], "ai-feature-" + record["execution_id"])
        self.assertEqual([c[1] for c in self.calls], ["create"])
        with self.assertRaises(runtime.CleanupUnconfirmed): self.execute()

    def test_precreate_authority_failure_blocks_replay_without_docker(self):
        self.fail_authority_on = 1
        with self.assertRaises(runtime.CleanupUnconfirmed):
            self.execute()
        self.assertFalse(self.calls)
        self.assertFalse((self.state / "execution/receipt.json").exists())
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_cleanup_transport_errors_preserve_cause_and_block_replay(self):
        for action in ("rm", "container"):
            for error in (OSError("transport failed"), subprocess.TimeoutExpired(action, 1)):
                with self.subTest(action=action, error=type(error).__name__):
                    self.setUp()
                    original_run = self.docker_run

                    def fail_cleanup(argv, **kwargs):
                        if argv[1] == action:
                            self.calls.append(argv)
                            raise error
                        return original_run(argv, **kwargs)

                    with patch.object(self, "docker_run", side_effect=fail_cleanup):
                        with self.assertRaises(runtime.CleanupUnconfirmed) as caught:
                            self.execute()
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertFalse((self.state / "execution/receipt.json").exists())
                    record = json.loads((self.state / "execution/lifecycle.json").read_text())
                    self.assertEqual(record["state"], "cleanup-unconfirmed")
                    self.assertEqual(record["container_id"], self.identifier)
                    with self.assertRaises(runtime.CleanupUnconfirmed):
                        runtime.require_resolved_execution(self.state, "execution")

    def test_cleanup_storage_failure_leaves_started_identity_and_no_receipt(self):
        original_write = runtime._durable_json
        error = OSError("storage unavailable")

        def fail_cleanup_write(path, record, **kwargs):
            if record.get("state") in ("cleanup-confirmed", "cleanup-unconfirmed"):
                raise error
            return original_write(path, record, **kwargs)

        with patch.object(runtime, "_durable_json", side_effect=fail_cleanup_write):
            with self.assertRaises(runtime.CleanupUnconfirmed) as caught:
                self.execute()
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual([call[1] for call in self.calls], ["create", "start", "rm", "container"])
        record = json.loads((self.state / "execution/lifecycle.json").read_text())
        self.assertEqual(record["state"], "started")
        self.assertEqual(record["container_id"], self.identifier)
        self.assertFalse((self.state / "execution/receipt.json").exists())
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_invalid_create_identity_never_targets_cleanup(self):
        self.created_id = "bad"
        with self.assertRaises(runtime.CleanupUnconfirmed): self.execute()
        self.assertEqual([c[1] for c in self.calls], ["create"])
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_term_and_duplicate_signals_cleanup_once_without_terminal_receipt(self):
        self.start_signal = True
        self.duplicate_signal = True
        with self.assertRaises(SystemExit) as stopped: self.execute()
        self.assertEqual(stopped.exception.code, 143)
        self.assertEqual([a[1] for a in self.calls], ["create", "start", "rm", "container"])
        self.assertFalse((self.state / "execution/receipt.json").exists())
        self.assertEqual(json.loads((self.state / "execution/lifecycle.json").read_text())["state"], "cleanup-confirmed")
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_stale_receipt_cannot_stand_in_for_later_execution(self):
        self.execute()
        output = self.state / "execution"
        receipt = json.loads((output / "receipt.json").read_text())
        receipt["cleanup"]["execution_id"] = "f" * 32
        (output / "receipt.json").write_text(json.dumps(receipt))
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_each_execution_log_is_bound_to_its_own_identity(self):
        first = self.execute()
        first_log = Path(first["log"])
        first_bytes = first_log.read_bytes()
        self.identifier = "f" * 64
        self.created_id = self.identifier
        second = self.execute()
        self.assertNotEqual(first["cleanup"]["execution_id"], second["cleanup"]["execution_id"])
        self.assertNotEqual(first["log"], second["log"])
        self.assertEqual(first_log.read_bytes(), first_bytes)
        self.assertEqual(Path(second["log"]).name, "output-" + second["cleanup"]["execution_id"] + ".log")
        self.assertNotEqual(first["log_sha256"], second["log_sha256"])

    def test_prestart_authority_failure_never_becomes_execution_timeout(self):
        for prior in ("none", "legacy-log", "receipt"):
            for error in (subprocess.TimeoutExpired("authority", 10), OSError("authority failed")):
                with self.subTest(prior=prior, error=type(error).__name__):
                    self.setUp()
                    output = self.state / "execution"
                    old_receipt = None
                    if prior == "receipt":
                        first = self.execute()
                        old_receipt = (output / "receipt.json").read_bytes()
                        old_log = Path(first["log"]).read_bytes()
                    elif prior == "legacy-log":
                        output.mkdir()
                        (output / "output.log").write_text("old log")
                    self.calls.clear()
                    self.authority_calls = 0
                    self.identifier = "f" * 64
                    self.created_id = self.identifier
                    def fail_before_start(*args):
                        self.authority_calls += 1
                        if self.authority_calls == 2:
                            raise error
                    with patch.object(self, "check_authority", side_effect=fail_before_start):
                        with self.assertRaises(type(error)):
                            self.execute()
                    self.assertEqual([call[1] for call in self.calls], ["create", "rm", "container"])
                    self.assertEqual((output / "receipt.json").read_bytes() if prior == "receipt" else None, old_receipt)
                    if prior == "receipt":
                        self.assertEqual(Path(first["log"]).read_bytes(), old_log)
                    else:
                        self.assertFalse((output / "receipt.json").exists())
                        if prior == "legacy-log":
                            self.assertEqual((output / "output.log").read_text(), "old log")
                    with self.assertRaises(runtime.CleanupUnconfirmed):
                        runtime.require_resolved_execution(self.state, "execution")

    def test_prestart_failure_with_unconfirmed_cleanup_preserves_original_identity(self):
        self.rm_code = 1

        def fail_before_start(*args):
            self.authority_calls += 1
            if self.authority_calls == 2:
                raise subprocess.TimeoutExpired("authority", 10)

        with patch.object(self, "check_authority", side_effect=fail_before_start):
            with self.assertRaises(runtime.CleanupUnconfirmed):
                self.execute()
        self.assertEqual([call[1] for call in self.calls], ["create", "rm"])
        record = json.loads((self.state / "execution/lifecycle.json").read_text())
        self.assertEqual(record["state"], "cleanup-unconfirmed")
        self.assertEqual(record["container_id"], self.identifier)
        self.assertFalse((self.state / "execution/receipt.json").exists())
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_storage_failure_before_identity_prevents_create(self):
        with patch.object(runtime, "_docker_authority", return_value=(["docker"], {}, {})), \
             patch.object(runtime, "_durable_json", side_effect=OSError("disk full")), \
             patch.object(runtime.subprocess, "run") as call:
            with self.assertRaises(OSError):
                runtime.execute(self.snapshot, self.image, [["true"]], self.state, "execution")
            call.assert_not_called()

    def test_unconfirmed_state_blocks_even_if_other_evidence_looks_confirmed(self):
        output = self.state / "execution"
        output.mkdir()
        (output / "lifecycle.json").write_text(json.dumps({
            "schema_version": 1, "state": "cleanup-unconfirmed",
            "execution_id": "e" * 32, "container_id": self.identifier,
            "removal_returncode": 0, "readback_returncode": 0, "readback_ids": [],
        }))
        (output / "receipt.json").write_text(json.dumps({"cleanup": {
            "state": "confirmed", "execution_id": "e" * 32, "container_id": self.identifier,
        }}))
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_missing_marker_with_execution_history_is_not_replayed(self):
        output = self.state / "execution"
        output.mkdir()
        (output / ("execution-" + "e" * 32 + ".json")).write_text('{}')
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")

    def test_unreadable_history_fails_closed_for_reader_and_execute(self):
        output = self.state / "execution"
        output.mkdir()
        (output / ("execution-" + "e" * 32 + ".json")).write_text('{}')
        for error in (PermissionError("denied"), OSError("I/O error"), FileNotFoundError("raced")):
            with self.subTest(error=type(error).__name__):
                with patch.object(runtime.os, "scandir", side_effect=error):
                    with self.assertRaises(runtime.CleanupUnconfirmed) as caught:
                        runtime.require_resolved_execution(self.state, "execution")
                    self.assertIs(caught.exception.__cause__, error)
                    with patch.object(runtime.subprocess, "run") as docker:
                        with self.assertRaises(runtime.CleanupUnconfirmed):
                            runtime.execute(self.snapshot, self.image, [["true"]], self.state, "execution")
                        docker.assert_not_called()

    def test_missing_history_directory_and_empty_directory_are_clear(self):
        runtime.require_resolved_execution(self.state, "execution")
        (self.state / "execution").mkdir()
        runtime.require_resolved_execution(self.state, "execution")

    def test_legacy_receipt_and_corrupt_marker_are_not_reused(self):
        output = self.state / "execution"
        output.mkdir()
        (output / "receipt.json").write_text('{}')
        with self.assertRaises(runtime.CleanupUnconfirmed):
            runtime.require_resolved_execution(self.state, "execution")
        (output / "lifecycle.json").write_text('{')
        with self.assertRaises(ValueError):
            runtime.require_resolved_execution(self.state, "execution")


if __name__ == "__main__":
    unittest.main()
