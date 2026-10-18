import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.feature_recovery import check_original_effects, close_original

REPOSITORY = "noteflowai/evalarc"


class RecoveryShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entry = self.root / "private-recovery"
        self.env = patch.dict(os.environ, {"NOTEFLOW_FEATURE_RECOVERY": str(self.entry)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, state, code=0, repository=REPOSITORY):
        value = {"schema_version": 1, "scope": "original-feature-effect-recovery",
                 "repository": repository, "state": state}
        self.entry.write_text("#!/usr/bin/env python3\nimport sys\n"
                              f"print({json.dumps(value)!r})\nsys.exit({code})\n")
        self.entry.chmod(0o700)

    def test_configured_orphan_stops_before_paid_calls_checkout_and_new_state(self):
        self.response("reconcile", 20)
        state = self.root / "original-state"
        with patch.object(developer, "ask") as author, patch.object(developer, "checkout") as checkout:
            with self.assertRaisesRegex(ValueError, "original native effect"):
                developer.develop("evalarc", {}, self.root / "workspace", state, "2026-10-06")
        author.assert_not_called()
        checkout.assert_not_called()
        self.assertFalse(state.exists())

    def test_clear_and_original_resume_are_observations_only(self):
        for state in ("clear", "resume-original"):
            self.response(state)
            self.assertEqual(check_original_effects(self.root, REPOSITORY)["state"], state)

    def test_missing_configured_entry_wrong_identity_or_failing_result_stop(self):
        with self.assertRaises(ValueError):
            check_original_effects(self.root, REPOSITORY)
        for state, code, repository in (("clear", 0, "other/evalarc"), ("clear", 1, REPOSITORY)):
            self.response(state, code, repository)
            with self.assertRaises(ValueError):
                check_original_effects(self.root, REPOSITORY)

    def test_original_compatibility_without_operator_configuration(self):
        with patch.dict(os.environ):
            os.environ.pop("NOTEFLOW_FEATURE_RECOVERY", None)
            self.assertIsNone(check_original_effects(self.root, REPOSITORY))

    def test_close_inherits_real_descriptor_before_checkout_model_or_new_task(self):
        import fcntl
        proof = {"state": "close-original", "task_id": "original",
                 "resolution_sha256": "a" * 64}
        observation = {"state": "close-original", "active_task_id": "original",
                       "external_finalization": proof}
        terminal = {"schema_version": 1, "scope": "original-feature-effect-recovery",
                    "repository": REPOSITORY, "repo": REPOSITORY,
                    "state": "externally-finalized", "status": "externally-finalized",
                    "feature_id": "original", "resolution_sha256": "a" * 64,
                    "historical_approval_proven": False, "runtime_acceptance_proven": False,
                    "showcase_e2e_proven": False}
        lock = self.root / "daily.lock"
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.entry.write_text(
                "#!/usr/bin/env python3\nimport os,sys\n"
                "assert sys.argv[1] == 'consume-external-finalization'\n"
                "fd=int(sys.argv[sys.argv.index('--lock-fd')+1])\n"
                f"assert os.fstat(fd).st_ino == {lock.stat().st_ino}\n"
                "assert 'FLOCK' in open('/proc/self/fdinfo/'+str(fd)).read()\n"
                f"print({json.dumps(terminal)!r})\n")
            self.entry.chmod(0o700)
            state = self.root / "original-state"
            with patch.dict(os.environ, {"FEATURE_LOCK_FD": str(handle.fileno())}), \
                    patch.object(developer, "check_original_effects", return_value=observation), \
                    patch.object(developer, "ask") as author, \
                    patch.object(developer, "checkout") as checkout:
                value = developer.develop("evalarc", {}, self.root / "workspace",
                                          state, "2026-10-06")
            self.assertEqual(value, terminal)
            author.assert_not_called()
            checkout.assert_not_called()
            self.assertFalse(state.exists())

    def test_closed_today_returns_before_any_new_work(self):
        terminal = {"status": "externally-finalized", "completed_on": "2026-10-06"}
        observation = {"state": "clear", "closed_original_effects": [{"terminal": terminal}]}
        with patch.object(developer, "check_original_effects", return_value=observation), \
                patch.object(developer, "ask") as author, \
                patch.object(developer, "checkout") as checkout:
            self.assertEqual(developer.develop("evalarc", {}, self.root / "workspace",
                             self.root / "state", "2026-10-06"), terminal)
        author.assert_not_called()
        checkout.assert_not_called()

    def test_missing_lease_and_uncertain_consumption_stop_without_new_work(self):
        proof = {"state": "close-original", "task_id": "original",
                 "resolution_sha256": "a" * 64}
        observation = {"state": "close-original", "active_task_id": "original",
                       "external_finalization": proof}
        self.response("clear")
        with patch.dict(os.environ):
            os.environ.pop("FEATURE_LOCK_FD", None)
            with self.assertRaises(ValueError):
                close_original(self.root, REPOSITORY, observation)
        with (self.root / "daily.lock").open("a+") as handle, \
                patch.dict(os.environ, {"FEATURE_LOCK_FD": str(handle.fileno())}), \
                patch.object(developer, "check_original_effects", return_value=observation), \
                patch.object(developer, "ask") as author, \
                patch.object(developer, "checkout") as checkout:
            with self.assertRaises(ValueError):
                developer.develop("evalarc", {}, self.root / "workspace",
                                  self.root / "state", "2026-10-06")
        author.assert_not_called()
        checkout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
