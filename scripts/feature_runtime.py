"""Run candidate code without host credentials, network or Docker socket access."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
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


def archive(root: Path, commit: str, destination: Path) -> None:
    """Export tracked files; exclude Git credentials and unrelated local files."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as stream:
        subprocess.run(["git", "archive", commit], cwd=root, stdout=stream, check=True)
        stream.seek(0)
        subprocess.run(["tar", "-x", "-C", str(destination)], stdin=stream, check=True)
    if any(p.is_symlink() for p in destination.rglob("*")):
        raise ValueError("The feature execution snapshot cannot contain symlinks")


def image_for(root: Path, base: str, config: dict, state: Path) -> str:
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
        with (state / "dependencies-build.log").open("a") as log:
            subprocess.run(["docker", "build", "-t", tag, str(context)],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    return command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()


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


def execute(snapshot: Path, image: str, commands: list[list[str]], state: Path,
            label: str, *, gpu: bool = False, generated: list[str] | None = None,
            timeout: int = 1200) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    output = state / label
    output.mkdir(exist_ok=True)
    artifacts = output / "artifacts"
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir()
    container = "ai-feature-" + uuid.uuid4().hex
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="feature-spec-") as tmp:
        config = Path(tmp)
        config.chmod(0o755)
        (config / "harness.py").write_text(HARNESS)
        write_json(config / "spec.json", {"commands": commands, "gpu": gpu,
                                        "generated": generated or []})
        for p in config.iterdir():
            p.chmod(0o644)
        argv = ["docker", "run", "--rm", "--name", container, "--network", "none",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "512", "--memory", "24g", "--cpus", str(min(os.cpu_count() or 1, 4)),
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--mount", f"type=bind,src={snapshot},dst=/candidate,readonly",
                "--mount", f"type=bind,src={config / 'harness.py'},dst=/harness.py,readonly",
                "--mount", f"type=bind,src={config / 'spec.json'},dst=/spec.json,readonly",
                "--mount", f"type=bind,src={artifacts},dst=/results",
                "--env", "PYTHONUNBUFFERED=1",
                "--entrypoint", "python"]
        if gpu:
            argv += ["--gpus", "device=0", "--tmpfs",
                     f"/workspace:uid={os.getuid()},gid={os.getgid()},size=4g"]
        argv += [image, "/harness.py"]
        def cleanup():
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)

        def interrupted(signum, _frame):
            cleanup()
            raise SystemExit(128 + signum)

        previous = {sig: signal.signal(sig, interrupted)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with (output / "output.log").open("w") as log:
                process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            cleanup()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    log_bytes = (output / "output.log").read_bytes()
    steps_file = artifacts / "commands.json"
    steps = []
    if steps_file.is_file() and not steps_file.is_symlink() and steps_file.stat().st_size < 100000:
        steps = json.loads(steps_file.read_text())
    record = {"exit_code": code, "commands": commands, "image": image,
              "executed_commands": steps,
              "gpu": gpu, "seconds": round(time.monotonic() - started, 2),
              "log": str(output / "output.log"),
              "log_sha256": hashlib.sha256(log_bytes).hexdigest()}
    write_json(output / "receipt.json", record)
    return record
