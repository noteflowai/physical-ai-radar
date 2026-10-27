"""Run candidate code without host credentials, network or Docker socket access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

try:
    from .agent_pipeline import command, write_json
except ImportError:
    from agent_pipeline import command, write_json

BASE_IMAGE = "noteflowai/feature-tools:20260927"
GPU_IMAGE = "poc/base-infer@sha256:f447c10e987803ab1ce25ac69221022b35f055a33f4de4d18a564691aee5af6e"
# Feature images are built per repository and base commit and were never removed (about 3.6 GB
# every one to two days). Images built here carry these labels; retention only ever looks at
# labelled images of the same repository, never at unlabelled ones, feature-tools or other images.
LABEL_REPO, LABEL_KEY, LABEL_CREATED = ("noteflow.feature.repo", "noteflow.feature.key",
                                        "noteflow.feature.created")
KEEP_IMAGES = 2
# A worker resolves its image once and runs it in several stages (`docker run --rm`, so no
# container references it in between); an image younger than this is never removed, so a
# concurrent build for the same repository cannot pull it from under a running task.
RETENTION_GRACE = timedelta(hours=24)
FEATURE_TAG = re.compile(r"^noteflowai/feature-([0-9a-f]{20}):latest$")


def archive(root: Path, commit: str, destination: Path) -> None:
    """Export tracked files; exclude Git credentials and unrelated local files."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "archive", commit], cwd=root, stdout=stream, check=True)
        stream.seek(0)
        subprocess.run(["tar", "-x", "-C", str(destination)], stdin=stream, check=True)
    if any(p.is_symlink() for p in destination.rglob("*")):
        raise ValueError("The feature execution snapshot cannot contain symlinks")


def image_for(root: Path, base: str, config: dict, state: Path, repo: str | None = None) -> str:
    policy = Path(__file__).with_name("feature-runtime.Dockerfile").read_bytes()
    identity = f"{os.getuid()}:{os.getgid()}"
    key = hashlib.sha256(policy + base.encode() + identity.encode()
                         + json.dumps(config["setup"]).encode()).hexdigest()[:20]
    tag = "noteflowai/feature-" + key
    if subprocess.run(["docker", "image", "inspect", tag],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()
    with tempfile.TemporaryDirectory(prefix="feature-image-") as tmp:
        context = Path(tmp)
        (context / "Dockerfile").write_bytes(policy)
        with (state / "tools-build.log").open("a") as log:
            subprocess.run(["docker", "build", "-t", BASE_IMAGE, str(context)],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)
        archive(root, base, context / "source")
        # Only the already published base supplies dependency and build configuration.
        dockerfile = f"FROM {BASE_IMAGE}\nCOPY source/ /workspace/\n"
        for setup in config["setup"]:
            dockerfile += "RUN " + setup + "\n"
        dockerfile += (f"RUN mkdir -p /opt/browsers /opt/feature-data /opt/feature-cache "
                       f"/opt/feature-state && chown -R {identity} "
                       f"/workspace /opt/browsers /home/worker /opt/feature-data "
                       f"/opt/feature-cache /opt/feature-state\nUSER {identity}\n")
        (context / "Dockerfile").write_text(dockerfile)
        labels = {LABEL_KEY: key, LABEL_CREATED: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  **({LABEL_REPO: repo} if repo else {})}
        with (state / "dependencies-build.log").open("a") as log:
            subprocess.run(["docker", "build", "-t", tag,
                            *[a for k, v in labels.items() for a in ("--label", f"{k}={v}")], str(context)],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    image = command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()
    if repo:
        prune_feature_images(repo, image, state)
    return image


def prune_feature_images(repo: str, current: str, state: Path, *, run=subprocess.run,
                         now=lambda: datetime.now(timezone.utc)) -> list[str]:
    """Keep the newest KEEP_IMAGES labelled feature images of one repository; remove older ones.

    Only images whose labels name exactly this repository, whose only tag is a feature tag of
    their own key, that no container (running or stopped) uses and that are older than
    RETENTION_GRACE are removed, by ID and without --force (Docker refuses images still in use).
    Never raises: a failure is logged to image-retention.log and the run continues. Build cache
    is not pruned here: Docker accepts `builder prune --filter label=...` but ignores the label
    (moby daemon/internal/builder-next/builder.go), so it would prune every user's cache.
    """
    log_path = state / "image-retention.log"

    def log(message: str) -> None:
        try:
            with log_path.open("a") as stream:
                stream.write(f"{now().strftime('%Y-%m-%dT%H:%M:%SZ')} {message}\n")
        except OSError:
            pass

    def docker(*args: str):
        return run(["docker", *args], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    removed: list[str] = []
    try:
        listed = docker("image", "ls", "-q", "--no-trunc", "--filter", f"label={LABEL_REPO}={repo}")
        if listed.returncode:
            log(f"list failed for {repo}: {listed.stderr.strip()[:200]}")
            return removed
        ids = sorted(set(listed.stdout.split()))
        if not ids:
            return removed
        inspected = docker("image", "inspect", *ids)
        if inspected.returncode:
            log(f"inspect failed for {repo}: {inspected.stderr.strip()[:200]}")
            return removed
        images = []
        for image in json.loads(inspected.stdout):
            labels = (image.get("Config") or {}).get("Labels") or {}
            tags = image.get("RepoTags") or []
            key = labels.get(LABEL_KEY, "")
            try:
                created = datetime.strptime(labels.get(LABEL_CREATED, ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # not one of ours: never touched
            if labels.get(LABEL_REPO) != repo or tags != [f"noteflowai/feature-{key}:latest"] \
                    or not FEATURE_TAG.fullmatch(tags[0]):
                continue
            images.append((created, image["Id"], tags[0]))
        containers = docker("container", "ls", "-a", "-q", "--no-trunc")
        if containers.returncode:
            log(f"container list failed: {containers.stderr.strip()[:200]}; nothing removed")
            return removed
        used = set()
        if containers.stdout.split():
            refs = docker("container", "inspect", "--format", "{{.Image}}", *containers.stdout.split())
            if refs.returncode:
                log(f"container inspect failed: {refs.stderr.strip()[:200]}; nothing removed")
                return removed
            used = set(refs.stdout.split())
        images.sort(reverse=True)
        kept = [i for i in images if i[1] == current][:1]
        kept += [i for i in images if i[1] != current][:max(0, KEEP_IMAGES - len(kept))]
        for created, image_id, tag in images:
            if (created, image_id, tag) in kept or image_id in used or now() - created < RETENTION_GRACE:
                continue
            result = docker("image", "rm", image_id)
            if result.returncode:
                log(f"keep {tag} {image_id[:19]}: rm failed: {result.stderr.strip()[:200]}")
            else:
                removed.append(image_id)
                log(f"removed {tag} {image_id[:19]} (created {created:%Y-%m-%dT%H:%M:%SZ}, repo {repo})")
    except Exception as error:  # retention must never fail a feature run
        log(f"retention skipped for {repo}: {type(error).__name__}: {str(error)[:200]}")
    return removed


# This script is controller-owned and is mounted read-only into the container.
HARNESS = r'''
import hashlib,json,os,pathlib,shutil,subprocess,sys
source=pathlib.Path("/candidate")
target=pathlib.Path("/workspace")
target.mkdir(parents=True,exist_ok=True)
for p in source.iterdir():
    dest=target/p.name
    if p.is_dir(): shutil.copytree(p,dest,dirs_exist_ok=True)
    else: shutil.copy2(p,dest)
os.chdir(target)
os.environ.update(PYTHONPATH="/workspace/src:/workspace",SITE_DIR="/tmp/feature-site",
    HOME="/tmp/feature-home",
    MPLCONFIGDIR="/tmp/feature-matplotlib",CI="1")
os.environ.setdefault("XDG_CACHE_HOME","/tmp/feature-cache")
pathlib.Path("/tmp/feature-home").mkdir(exist_ok=True)
subprocess.run(["git","init","-q"],check=True)
subprocess.run(["git","config","user.email","feature@example.invalid"],check=True)
subprocess.run(["git","config","user.name","Feature validation"],check=True)
subprocess.run(["git","add","."],check=True,stdout=subprocess.DEVNULL)
subprocess.run(["git","commit","-qm","Candidate validation snapshot"],check=True)
spec=json.load(open("/spec.json"))
if spec["gpu"]:
    import torch
    if not torch.cuda.is_available(): raise RuntimeError("CUDA is unavailable")
    x=torch.arange(4096,device="cuda",dtype=torch.float32).reshape(64,64)
    y=x@x.T
    torch.cuda.synchronize()
    print(json.dumps({"cuda_device":torch.cuda.get_device_name(0),
      "torch":torch.__version__,"cuda_computation_sum":y.sum().item()}),flush=True)
steps=[]
for argv in spec["commands"]:
    print(json.dumps({"command":argv}),flush=True)
    completed=subprocess.run(argv)
    steps.append({"argv":argv,"exit_code":completed.returncode,"executed":True})
    pathlib.Path("/results/commands.json").write_text(json.dumps(steps))
    if completed.returncode: raise SystemExit(completed.returncode)
for name in spec.get("generated",[]):
    path=target/name
    if path.is_file():
        out=pathlib.Path("/results/generated")/name
        out.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,out)
'''


class CleanupUnconfirmed(RuntimeError):
    """Original Docker effect cannot be proven absent; reconciliation is required."""


def _durable_json(path: Path, value: dict, *, exclusive: bool = False) -> None:
    """Persist an atomic record and its directory entry before a Docker effect."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".lifecycle-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise CleanupUnconfirmed("Original lifecycle record is unavailable")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise CleanupUnconfirmed("Original lifecycle record is invalid")
    return value


def require_resolved_execution(state: Path, label: str) -> None:
    """Never bypass an unfinished Docker effect by reusing an older receipt."""
    output = state / label
    marker = output / "lifecycle.json"
    try:
        marker.lstat()
    except FileNotFoundError:
        if (output / "receipt.json").exists():
            raise CleanupUnconfirmed("Legacy receipt requires reconciliation before reuse")
        if output.is_dir() and any(output.glob("execution-*.json")):
            raise CleanupUnconfirmed("Original execution history requires reconciliation")
        return
    record = _read_record(marker)
    if (record.get("state") != "cleanup-confirmed"
            or not isinstance(record.get("execution_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", record["execution_id"])
            or not isinstance(record.get("container_id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["container_id"])
            or record.get("removal_returncode") != 0 or record.get("readback_returncode") != 0
            or record.get("readback_ids") != []):
        raise CleanupUnconfirmed("Original container requires reconciliation")
    # A previous receipt cannot stand in for a later execution that died after
    # cleanup but before its own result was durably written.
    receipt_path = output / "receipt.json"
    if (receipt_path.is_symlink() or not receipt_path.is_file()
            or receipt_path.stat().st_size > 100_000):
        raise CleanupUnconfirmed("Original execution receipt requires reconciliation")
    receipt = json.loads(receipt_path.read_bytes())
    cleanup = receipt.get("cleanup") if isinstance(receipt, dict) else None
    if (not isinstance(cleanup, dict) or cleanup.get("state") != "confirmed"
            or cleanup.get("execution_id") != record["execution_id"]
            or cleanup.get("container_id") != record["container_id"]):
        raise CleanupUnconfirmed("Receipt and original execution identity differ")


def _docker_authority() -> tuple[list[str], dict, dict]:
    """Freeze the local Docker socket; remote authorities require a separate design."""
    env = os.environ.copy()
    context = env.get("DOCKER_CONTEXT")
    host = env.get("DOCKER_HOST") if not context else None
    if not host:
        if not context:
            result = subprocess.run(["docker", "context", "show"], env=env, capture_output=True,
                                    text=True, timeout=10, stdin=subprocess.DEVNULL, check=True)
            context = result.stdout.strip()
        result = subprocess.run(["docker", "context", "inspect", context, "--format",
                                 "{{.Endpoints.docker.Host}}"], env=env, capture_output=True,
                                text=True, timeout=10, stdin=subprocess.DEVNULL, check=True)
        host = result.stdout.strip()
    if not isinstance(host, str) or not host.startswith("unix:///"):
        raise CleanupUnconfirmed("A local Unix Docker authority is required")
    socket = Path(host[len("unix://"):]).resolve(strict=True)
    socket_info = socket.stat()
    if not stat.S_ISSOCK(socket_info.st_mode):
        raise CleanupUnconfirmed("Docker endpoint is not a socket")
    env.pop("DOCKER_HOST", None)
    env.pop("DOCKER_CONTEXT", None)
    docker = ["docker", "--host", f"unix://{socket}"]
    result = subprocess.run([*docker, "info", "--format", "{{.ID}}"], env=env, capture_output=True,
                            text=True, timeout=10, stdin=subprocess.DEVNULL, check=True)
    daemon = result.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", daemon):
        raise CleanupUnconfirmed("Docker daemon identity unavailable")
    return docker, env, {"daemon_id": daemon, "socket": str(socket),
                         "socket_device": socket_info.st_dev, "socket_inode": socket_info.st_ino}


def _check_authority(docker: list[str], env: dict, authority: dict) -> None:
    current = Path(authority["socket"]).stat()
    if (not stat.S_ISSOCK(current.st_mode) or current.st_dev != authority["socket_device"]
            or current.st_ino != authority["socket_inode"]):
        raise CleanupUnconfirmed("Docker socket identity changed")
    result = subprocess.run([*docker, "info", "--format", "{{.ID}}"], env=env, capture_output=True,
                            text=True, timeout=10, stdin=subprocess.DEVNULL, check=True)
    if result.stdout.strip() != authority["daemon_id"]:
        raise CleanupUnconfirmed("Docker daemon identity changed")


def execute(snapshot: Path, image: str, commands: list[list[str]], state: Path,
            label: str, *, gpu: bool = False, generated: list[str] | None = None,
            timeout: int = 1200, workspace_bytes: int | None = None,
            cpus: int = 4, memory_bytes: int | None = None,
            workspace_executable: bool = False) -> dict:
    if type(cpus) is not int or not 1 <= cpus <= 4:
        raise ValueError("CPU limit must be between one and four")
    if memory_bytes is not None and (type(memory_bytes) is not int or memory_bytes < 256 * 1024**2):
        raise ValueError("Invalid memory limit")
    if workspace_bytes is not None and (gpu or type(workspace_bytes) is not int
                                       or not 64 * 1024**2 <= workspace_bytes <= 1024**3):
        raise ValueError("Bounded CPU workspace must be 64 MiB to 1 GiB")
    if (type(workspace_executable) is not bool
            or workspace_executable and (gpu or workspace_bytes is None)):
        raise ValueError("Executable workspaces require bounded CPU isolation")
    if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", label):
        raise ValueError("A bounded execution label is required")
    state.mkdir(parents=True, exist_ok=True)
    output = state / label
    output.mkdir(exist_ok=True)
    if state.is_symlink() or output.is_symlink():
        raise ValueError("Original execution directory required")
    with (output / ".lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_resolved_execution(state, label)
        control = state / "started.json"
        bound = None
        if control.exists() or control.is_symlink():
            bound = _read_record(control)
            if (not re.fullmatch(r"[a-f0-9]{64}", bound.get("check_id", ""))
                    or state.name != bound["check_id"]
                    or not re.fullmatch(r"[a-f0-9]{40}", bound.get("provider_commit", ""))
                    or bound.get("image") != image):
                raise CleanupUnconfirmed("Original check/provider identity differs")
        docker, docker_env, authority = _docker_authority()
        run_id = uuid.uuid4().hex
        container = "ai-feature-" + run_id
        identity = {"schema_version": 1, "state": "started", "execution_id": run_id,
                    "container_name": container, "container_id": None,
                    "check_id": bound.get("check_id") if bound else None,
                    "provider_commit": bound.get("provider_commit") if bound else None,
                    "provider_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "authority": authority, "image": image,
                    "commands_sha256": hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest(),
                    "snapshot": str(snapshot.resolve()), "label": label}
        history = output / ("execution-" + run_id + ".json")
        _durable_json(history, identity, exclusive=True)
        _durable_json(output / "lifecycle.json", identity)
        artifacts = output / "artifacts"
        if artifacts.exists():
            shutil.rmtree(artifacts)
        artifacts.mkdir()
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="feature-spec-") as tmp:
            config = Path(tmp)
            config.chmod(0o755)
            (config / "harness.py").write_text(HARNESS)
            write_json(config / "spec.json", {"commands": commands, "gpu": gpu,
                                            "generated": generated or []})
            for p in config.iterdir():
                p.chmod(0o644)
            argv = [*docker, "create", "--init", "--name", container, "--network", "none",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--pids-limit", "512", "--memory", str(memory_bytes) if memory_bytes else "24g",
                    "--cpus", str(min(os.cpu_count() or 1, cpus)),
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", f"type=bind,src={snapshot},dst=/candidate,readonly",
                    "--mount", f"type=bind,src={config / 'harness.py'},dst=/harness.py,readonly",
                    "--mount", f"type=bind,src={config / 'spec.json'},dst=/spec.json,readonly",
                    "--mount", f"type=bind,src={artifacts},dst=/results",
                    "--env", "PYTHONUNBUFFERED=1", "--entrypoint", "python"]
            if gpu:
                argv += ["--gpus", "device=0", "--tmpfs",
                         f"/workspace:uid={os.getuid()},gid={os.getgid()},size=4g"]
            elif workspace_bytes is not None:
                argv += ["--read-only", "--log-driver", "none",
                         "--tmpfs", f"/workspace:uid={os.getuid()},gid={os.getgid()},size={workspace_bytes}"
                         + (",exec" if workspace_executable else ""),
                         "--tmpfs", f"/tmp:uid={os.getuid()},gid={os.getgid()},size=134217728",
                         "--env", "XDG_STATE_HOME=/tmp/feature-state",
                         "--env", "XDG_DATA_HOME=/tmp/feature-data",
                         "--env", "XDG_CACHE_HOME=/tmp/feature-cache"]
            argv += [image, "/harness.py"]
            class Interrupted(Exception):
                def __init__(self, signum): self.signum = signum
            def interrupted(signum, _frame):
                raise Interrupted(signum)
            previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
            container_id = None
            interrupted_signal = None
            code = None

            def mark_unconfirmed():
                # Best effort only: a storage failure must leave the original started
                # record in place and must never turn an uncertain cleanup into success.
                uncertain = {**identity, "state": "cleanup-unconfirmed"}
                try:
                    _durable_json(history, uncertain)
                    _durable_json(output / "lifecycle.json", uncertain)
                except OSError:
                    pass

            try:
                _check_authority(docker, docker_env, authority)
                created = subprocess.run(argv, env=docker_env, stdin=subprocess.DEVNULL, capture_output=True,
                                         text=True, timeout=30, check=True)
                created_id = created.stdout.strip()
                if not re.fullmatch(r"[a-f0-9]{64}", created_id):
                    raise CleanupUnconfirmed("Container create returned an invalid identity")
                container_id = created_id
                identity["container_id"] = container_id
                _durable_json(history, identity)
                _durable_json(output / "lifecycle.json", identity)
                _check_authority(docker, docker_env, authority)
                with (output / "output.log").open("w") as log:
                    process = subprocess.run([*docker, "start", "--attach", container_id], env=docker_env,
                                             stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, timeout=timeout)
                code = process.returncode
            except subprocess.TimeoutExpired:
                if container_id is None:
                    raise CleanupUnconfirmed("Container creation did not complete")
                code = 124
            except Interrupted as exc:
                interrupted_signal = exc.signum
            finally:
                # Ignore duplicate signals only during bounded cleanup; retain original handlers afterward.
                for sig in previous:
                    signal.signal(sig, signal.SIG_IGN)
                try:
                    if container_id is None:
                        mark_unconfirmed()
                    else:
                        _check_authority(docker, docker_env, authority)
                        removed = subprocess.run([*docker, "rm", "-f", container_id], env=docker_env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                        if removed.returncode != 0:
                            raise CleanupUnconfirmed("Exact container removal was not confirmed")
                        _check_authority(docker, docker_env, authority)
                        listed = subprocess.run([*docker, "container", "ls", "-a", "-q", "--no-trunc",
                            "--filter", f"id={container_id}"], env=docker_env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10, check=True)
                        _check_authority(docker, docker_env, authority)
                        if listed.stdout.strip():
                            raise CleanupUnconfirmed("Exact container remains present")
                        identity.update(state="cleanup-confirmed", removal_returncode=removed.returncode,
                                        readback_returncode=listed.returncode, readback_ids=[])
                        _durable_json(history, identity)
                        _durable_json(output / "lifecycle.json", identity)
                except Exception:
                    mark_unconfirmed()
                    raise
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
            if interrupted_signal is not None:
                raise SystemExit(128 + interrupted_signal)
            if code is None:
                raise CleanupUnconfirmed("Original Docker execution has no terminal result")
        log_bytes = (output / "output.log").read_bytes()
        steps_file = artifacts / "commands.json"
        steps = []
        if steps_file.is_file() and not steps_file.is_symlink() and steps_file.stat().st_size < 100000:
            steps = json.loads(steps_file.read_text())
        record = {"exit_code": code, "commands": commands, "image": image,
                  "executed_commands": steps, "gpu": gpu, "seconds": round(time.monotonic() - started, 2),
                  "log": str(output / "output.log"), "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
                  "cleanup": {"state": "confirmed", "execution_id": run_id, "container_id": container_id}}
        _durable_json(output / "receipt.json", record)
        return record
