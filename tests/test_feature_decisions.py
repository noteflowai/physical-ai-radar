import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import feature_decisions as decisions


def spec():
    return {"purpose": "Exercise a recorded robot policy decision for replay.",
            "cases": [{"id": "near_person", "request": {
                "model": "kev-latest", "state": "Stop if a person is within 1m. Person is 0.5m away.",
                "questions": {"action": {"type": "choice", "instructions": "Choose the policy action",
                                         "criteria": {"stop": "Stop", "go": "Continue"}}}}}]}


IMAGE = "sha256:" + "a" * 64


def result(request):
    return {"schema": decisions.SCHEMA, "created_at": "2026-09-27T10:00:00+00:00",
            "purpose": request["purpose"],
            "input_sha256": hashlib.sha256(decisions.canonical(request)).hexdigest(),
            "startup_seconds": 1,
            "model": {"run": decisions.RUN, "base": decisions.BASE, "device": "cuda",
                      "backend": "torch", "dtype": "bfloat16", "image_id": IMAGE,
                      "base_revision": decisions.BASE_REVISION},
            "records": [{"id": c["id"], "request": c["request"], "wall_ms": 2,
                         "response": {"model": "kev-latest", "latency_ms": 1,
                                      "answers": {"action": {"type": "choice", "choice": "stop",
                                                            "confidence": .8,
                                                            "probabilities": {"stop": .9, "go": .1}}}}}
                        for c in request["cases"]]}


class DecisionTests(unittest.TestCase):
    def test_typed_requests_and_finite_bounded_data(self):
        request = spec()
        request["cases"][0]["request"]["questions"].update({
            "required": {"type": "noul", "instructions": "Is a stop required?"},
            "urgency": {"type": "score", "instructions": "Rate urgency", "criteria": ["low", "high"]}})
        decisions.validate_spec(request)
        for mutation in [
            lambda s: s.update(command="printenv"),
            lambda s: s["cases"][0]["request"].update(model="jev-latest"),
            lambda s: s["cases"].append(copy.deepcopy(s["cases"][0])),
            lambda s: s["cases"][0].update(id="../private"),
            lambda s: s["cases"][0]["request"].update(state=float("nan")),
            lambda s: s["cases"][0]["request"].update(state="x" * 8192),
            lambda s: s["cases"][0]["request"]["questions"]["action"].update(criteria={}),
        ]:
            bad = copy.deepcopy(request)
            mutation(bad)
            with self.subTest(bad=repr(bad)[:100]), self.assertRaises(ValueError):
                decisions.validate_spec(bad)

    def test_unavailable_installation_does_not_require_gpu_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(decisions.subprocess, "run") as run:
            self.assertFalse(decisions.capability(Path(directory))["available"])
            run.assert_not_called()

    def test_manifest_detects_changed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"kev_local.py": 'BATCH_SCHEMA = "kev-local-batch-v1"\n',
                     "runtime.json": json.dumps({"image_id": IMAGE}),
                     "provenance.json": "{}", "relay.py": "# reviewed relay"}
            lines = []
            for name, content in files.items():
                (root / name).write_text(content)
                lines.append(hashlib.sha256(content.encode()).hexdigest() + "  " + name)
            (root / "deployment-files.sha256").write_text("\n".join(lines))
            self.assertTrue(decisions.capability(root)["available"])
            (root / "relay.py").write_text("unreviewed change")
            self.assertFalse(decisions.capability(root)["available"])

    def test_all_inputs_model_and_distributions_must_match(self):
        request, valid = spec(), result(spec())
        decisions.validate_result(request, valid, IMAGE)
        for mutation in [
            lambda r: r.update(input_sha256="0" * 64),
            lambda r: r["model"].update(device="cpu"),
            lambda r: r["records"].clear(),
            lambda r: r["records"][0]["request"].update(state="different state"),
            lambda r: r["records"][0]["response"]["answers"]["action"].update(choice="unknown"),
            lambda r: r["records"][0]["response"]["answers"]["action"].update(
                probabilities={"stop": float("nan"), "go": 1}),
            lambda r: r["records"][0]["response"]["answers"]["action"].update(
                probabilities={"stop": .9, "go": .9}),
            lambda r: r["records"][0].update(wall_ms=True),
        ]:
            bad = copy.deepcopy(valid)
            mutation(bad)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                decisions.validate_result(request, bad, IMAGE)

    @patch.object(decisions, "installation", return_value={"model": decisions.RUN, "image_id": IMAGE})
    def test_retry_reuses_complete_evidence_and_keeps_credentials_out(self, installation):
        request = spec()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"KIRO_API_KEY": "test-private", "AWS_SECRET_ACCESS_KEY": "test-private"}):
            state = Path(directory)
            completed = subprocess.CompletedProcess([], 0, json.dumps(result(request)), "")
            with patch.object(decisions.subprocess, "run", return_value=completed) as run:
                first = decisions.collect(request, state)
                second = decisions.collect(request, state)
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("KIRO_API_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["timeout"], 660)

    @patch.object(decisions, "installation", return_value={"model": decisions.RUN, "image_id": IMAGE})
    def test_failed_or_incomplete_probe_never_becomes_cached_success(self, installation):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for completed in [subprocess.CompletedProcess([], 1, "", "private diagnostic"),
                              subprocess.CompletedProcess([], 0, json.dumps({}), "")]:
                with patch.object(decisions.subprocess, "run", return_value=completed):
                    with self.assertRaises((RuntimeError, ValueError)):
                        decisions.collect(spec(), state)
                self.assertEqual(list(state.rglob("receipt.json")), [])
            self.assertNotIn("private diagnostic", next(state.rglob("failure.json")).read_text())

    @patch.object(decisions, "installation", return_value={"model": decisions.RUN, "image_id": IMAGE})
    def test_changed_approved_input_requires_new_execution(self, installation):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for text in ["Person is 0.5m away", "Person is 5m away"]:
                request = spec()
                request["cases"][0]["request"]["state"] = text
                with patch.object(decisions.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(result(request)), "")) as run:
                    decisions.collect(request, state)
                run.assert_called_once()
            self.assertEqual(len(list(state.rglob("receipt.json"))), 2)


if __name__ == "__main__":
    unittest.main()
