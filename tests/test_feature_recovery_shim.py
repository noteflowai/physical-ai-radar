import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import develop_repos as developer
from scripts.feature_recovery import check_original_effects

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


if __name__ == "__main__":
    unittest.main()
