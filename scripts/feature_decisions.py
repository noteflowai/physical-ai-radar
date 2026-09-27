"""Controller-owned, optional local Kev records for an independently reviewed plan."""
from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import uuid

try:
    from .agent_pipeline import write_json
except ImportError:
    from agent_pipeline import write_json

ROOT = Path.home() / ".local/share/kev-local"
LOCAL_STATE = Path.home() / ".local/state/kev-local"
RUN = "jaredpalmer/kev-4b@139fdd94f1b6a6ad80cc15e08fcb99cac885a101"
BASE = "Qwen/Qwen3.5-4B-Base"
BASE_REVISION = "1001bb4d826a52d1f399e183466143f4da7b741b"
SCHEMA = "kev-local-batch-v1"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode()


def validate_spec(spec) -> None:
    if not isinstance(spec, dict) or set(spec) != {"purpose", "cases"}:
        raise ValueError("decision_probe requires purpose and cases only")
    if not isinstance(spec["purpose"], str) or not 10 <= len(spec["purpose"]) <= 500:
        raise ValueError("decision_probe purpose must explain the feature in 10..500 characters")
    cases = spec["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 4:
        raise ValueError("decision_probe needs 1..4 cases")
    identifiers = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "request"}:
            raise ValueError("Each decision case needs id and request only")
        identifier = case["id"]
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier)
                or identifier in identifiers):
            raise ValueError("Decision case IDs must be unique simple identifiers")
        identifiers.add(identifier)
        request = case["request"]
        if (not isinstance(request, dict) or set(request) != {"model", "state", "questions"}
                or request["model"] != "kev-latest"):
            raise ValueError("Decision request needs model=kev-latest, state and questions only")
        questions = request["questions"]
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 4:
            raise ValueError("Use 1..4 decision questions per case")
        for identifier, question in questions.items():
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier):
                raise ValueError("Question IDs must be simple identifiers")
            if (not isinstance(question, dict) or not {"type", "instructions"} <= set(question)
                    or set(question) - {"type", "instructions", "criteria"}
                    or not isinstance(question["instructions"], str)
                    or not 1 <= len(question["instructions"]) <= 1000):
                raise ValueError("Each question needs a type and bounded text instructions")
            kind, criteria = question["type"], question.get("criteria")
            if kind == "choice":
                if (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 8
                        or not all(isinstance(k, str) and 1 <= len(k) <= 80 for k in criteria)):
                    raise ValueError("Choice needs 1..8 named options")
            elif kind == "score":
                if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8:
                    raise ValueError("Score needs 1..8 ordered criteria")
            elif kind == "noul":
                if criteria is not None and (not isinstance(criteria, dict)
                                              or set(criteria) - {"false", "true"}):
                    raise ValueError("Noul criteria may name false and true only")
            else:
                raise ValueError("Unsupported decision type")
        if len(canonical(request)) > 8192:
            raise ValueError("Each decision request is limited to 8192 UTF-8 bytes")
    if len(canonical(spec)) > 12288:
        raise ValueError("decision_probe exceeds 12288 UTF-8 bytes")


def installation(root: Path = ROOT) -> dict:
    """Inspect installation integrity without starting a model or reading its key."""
    expected = {"kev_local.py", "relay.py", "runtime.json", "provenance.json"}
    seen = set()
    for line in (root / "deployment-files.sha256").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        path = root / relative
        if (not re.fullmatch(r"[a-f0-9]{64}", digest) or Path(relative).name != relative
                or path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000):
            raise ValueError("Invalid local Kev installation manifest")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Local Kev deployment checksum mismatch")
        seen.add(relative)
    if not expected <= seen:
        raise ValueError("Incomplete local Kev installation manifest")
    runtime = json.loads((root / "runtime.json").read_text())
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", runtime["image_id"]):
        raise ValueError("Local Kev needs an immutable image ID")
    source = (root / "kev_local.py").read_text()
    if ('BATCH_SCHEMA = "kev-local-batch-v1"' not in source
            or 'JOB_LABEL = "ai.local.job"' not in source):
        raise ValueError("Local Kev needs the reviewed batch protocol")
    return {"model": RUN, "image_id": runtime["image_id"]}


def capability(root: Path = ROOT) -> dict:
    try:
        info = installation(root)
    except (OSError, ValueError, KeyError):
        return {"available": False, "reason": "Pinned local Kev batch runtime is not installed or verified"}
    return {**info, "available": True, "kind": "optional_controller_decision_probe",
            "limits": "1..4 cases, 1..4 questions each, 1..8 options; total 12 KiB; 600-second GPU session",
            "scope": "Kev is an independent Jev-style model. Inputs are supplied synthetic examples, not held-out labels. "
                     "Recorded responses prove execution only; semantic quality, calibration and thresholds need separate evaluation."}


def number(value, low=0, high=float("inf")) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_result(spec: dict, result: dict, image_id: str) -> None:
    if (not isinstance(result, dict) or result.get("schema") != SCHEMA
            or result.get("input_sha256") != hashlib.sha256(canonical(spec)).hexdigest()
            or result.get("purpose") != spec["purpose"]
            or result.get("model") != {"run": RUN, "base": BASE, "device": "cuda",
                                      "backend": "torch", "dtype": "bfloat16",
                                      "image_id": image_id, "base_revision": BASE_REVISION}):
        raise ValueError("Decision evidence does not match the approved inputs and pinned CUDA model")
    if not number(result.get("startup_seconds")) or not isinstance(result.get("created_at"), str):
        raise ValueError("Decision evidence lacks timing/provenance")
    records = result.get("records")
    if not isinstance(records, list) or len(records) != len(spec["cases"]):
        raise ValueError("Decision evidence must contain every approved case")
    for case, record in zip(spec["cases"], records):
        if (not isinstance(record, dict) or record.get("id") != case["id"]
                or record.get("request") != case["request"] or not number(record.get("wall_ms"))):
            raise ValueError("Decision record changed an approved input")
        response = record.get("response")
        questions = case["request"]["questions"]
        if not isinstance(response, dict) or not isinstance(response.get("answers"), dict):
            raise ValueError("Decision response and answers must be objects")
        answers = response["answers"]
        if response.get("model") != "kev-latest" or set(answers) != set(questions):
            raise ValueError("Decision answers do not match the requested questions")
        if not number(response.get("latency_ms")):
            raise ValueError("Decision response lacks measured latency")
        for name, question in questions.items():
            answer, kind = answers[name], question["type"]
            if not isinstance(answer, dict) or answer.get("type") != kind:
                raise ValueError("Decision answer type mismatch")
            if kind == "noul":
                if not number(answer.get("noul"), 0, 1):
                    raise ValueError("Invalid Noul probability")
                continue
            keys = set(question["criteria"]) if kind == "choice" else {
                str(i) for i in range(len(question["criteria"]))}
            probabilities = answer.get("probabilities", {})
            if (not isinstance(probabilities, dict) or set(probabilities) != keys
                    or not all(number(p, 0, 1) for p in probabilities.values())
                    or abs(sum(probabilities.values()) - 1) > 0.002
                    or not number(answer.get("confidence"), 0, 1)):
                raise ValueError("Invalid decision distribution")
            if kind == "choice" and answer.get("choice") not in keys:
                raise ValueError("Decision choice is outside the approved options")
            if kind == "score" and not number(answer.get("score"), 0, len(keys) - 1):
                raise ValueError("Decision score is outside the ordered scale")


def cleanup_job(job: str, env: dict) -> None:
    """Remove only containers from this attempt, never another user's local session."""
    records = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=ai.local.service=kev-4b",
         "--filter", "label=ai.local.job=" + job],
        env=env, capture_output=True, text=True, timeout=30, check=True)
    ids = records.stdout.split()
    if not all(re.fullmatch(r"[a-f0-9]{12,64}", identifier) for identifier in ids):
        raise ValueError("Unexpected local decision container identifier")
    if ids:
        subprocess.run(["docker", "rm", "--force", *ids], env=env,
                       capture_output=True, timeout=30, check=True)
    if LOCAL_STATE.is_dir():
        with (LOCAL_STATE / "service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # Another session now owns its metadata.
            active = LOCAL_STATE / "active.json"
            if active.is_file() and json.loads(active.read_text()).get("job_id") == job:
                active.unlink()


def run_probe(argv: list[str], env: dict, timeout: int = 660):
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=120)  # Logs, relay shutdown and container removal.
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=15)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def collect(spec: dict, state: Path, root: Path = ROOT) -> dict:
    """Cache by approved request + runtime; retries reuse validated complete evidence."""
    validate_spec(spec)
    runtime = installation(root)
    identity = hashlib.sha256(canonical({"spec": spec, "runtime": runtime})).hexdigest()
    directory = state / "decision-probes" / identity
    receipt = directory / "receipt.json"
    if receipt.is_file():
        try:
            if receipt.is_symlink() or receipt.stat().st_size > 100000:
                raise ValueError("Invalid cached decision receipt")
            result = json.loads(receipt.read_text())
            validate_result(spec, result, runtime["image_id"])
        except (ValueError, TypeError, KeyError):
            receipt.replace(directory / ("receipt.invalid-" + uuid.uuid4().hex + ".json"))
        else:
            return result
    directory.mkdir(parents=True, exist_ok=True)
    request = directory / "request.json"
    write_json(request, spec)
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "XDG_RUNTIME_DIR") if k in os.environ}
    env["KEV_JOB_ID"] = uuid.uuid4().hex
    try:
        completed = run_probe(["python3", str(root / "kev_local.py"), "batch", str(request),
                               "--seconds", "600"], env)
    except subprocess.TimeoutExpired:
        failure = {"exit_code": 124, "reason": "Local Kev probe exceeded its controller deadline"}
        try:
            cleanup_job(env["KEV_JOB_ID"], env)
        except (OSError, ValueError, subprocess.SubprocessError):
            failure["cleanup"] = "Job cleanup needs local inspection; runtime watchdog remains active"
        write_json(directory / "failure.json", failure)
        raise RuntimeError("Local Kev probe timed out; retain the approved feature and retry") from None
    if completed.returncode:
        # Do not propagate arbitrary runtime diagnostics/credential material to model context.
        write_json(directory / "failure.json", {"exit_code": completed.returncode,
                                                "reason": "Local Kev probe failed; inspect local service logs"})
        raise RuntimeError("Local Kev probe failed or GPU is busy; retain the approved feature and retry")
    if len(completed.stdout.encode()) > 100000:
        raise ValueError("Local Kev result exceeds the evidence budget")
    result = json.loads(completed.stdout)
    validate_result(spec, result, runtime["image_id"])
    write_json(receipt, result)
    return result
