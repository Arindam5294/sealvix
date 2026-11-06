"""Sandbox runner: every command runs in a fresh, throwaway gVisor container.

Containment (enforced on every step):
  - gVisor runtime (runsc): the container talks to a user-space kernel, not the host kernel
  - --network none for test/patch steps (install step may use the network, never with secrets)
  - non-root user, read-only root filesystem, writable /work (job copy) and tmpfs /tmp only
  - CPU, memory, pids caps and a wall-clock limit
  - --cap-drop ALL, no-new-privileges
  - empty environment: no host env vars, no API keys
  - --rm: the container is destroyed after the step

The LLM and any tokens live only on the host. Nothing secret is ever mounted or passed in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict

RUNTIME = os.environ.get("SANDBOX_RUNTIME", "runsc")  # runsc | runc | local (dev only, NOT isolated)
CPUS = os.environ.get("SANDBOX_CPUS", "1")
MEMORY = os.environ.get("SANDBOX_MEMORY", "1g")
PIDS = os.environ.get("SANDBOX_PIDS", "256")
STEP_TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "120"))
MAX_OUTPUT = 200_000

IMAGES = {
    "node": os.environ.get("SANDBOX_NODE_IMAGE", "node:22-bookworm-slim"),
    "python": os.environ.get("SANDBOX_PYTHON_IMAGE", "python:3.12-slim"),
}

# Output fragments that prove a containment control fired.
SIGNALS = [
    ("network_blocked", ["Network is unreachable", "Could not resolve host", "Temporary failure in name resolution",
                         "getaddrinfo ENOTFOUND", "getaddrinfo EAI_AGAIN", "ENETUNREACH", "Name or service not known",
                         "bad address", "Failed to establish a new connection",
                         "urlopen error", "[Errno -3]", "[Errno -2]", "[Errno 101]"]),
    ("readonly_fs", ["Read-only file system", "EROFS"]),
    ("pids_cap", ["Resource temporarily unavailable", "fork: retry", "EAGAIN", "Cannot fork", "can't fork"]),
    ("permission_denied", ["Permission denied", "EACCES", "Operation not permitted", "EPERM"]),
]


@dataclass
class StepResult:
    step: str
    command: str
    exit_code: int
    duration_s: float
    output: str
    timed_out: bool = False
    oom_killed: bool = False
    network: str = "none"
    container: str = ""
    destroyed: bool = True
    signals: list[str] = field(default_factory=list)
    runtime: str = RUNTIME

    def to_dict(self) -> dict:
        return asdict(self)


def detect_signals(output: str, exit_code: int, timed_out: bool) -> list[str]:
    found = []
    for name, needles in SIGNALS:
        if any(n in output for n in needles):
            found.append(name)
    if exit_code == 137 and not timed_out:
        found.append("memory_cap")
    if timed_out:
        found.append("wall_clock_cap")
    return found


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _docker_args(name: str, image: str, workdir_host: str, cmd: str, network: bool) -> list[str]:
    uid = os.environ.get("SANDBOX_UID", "1000")
    args = [
        "docker", "run", "--rm", "--name", name,
        "--runtime", RUNTIME,
        "--network", "bridge" if network else "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=256m",
        "--tmpfs", "/home/sandbox:rw,size=256m",
        "-v", f"{workdir_host}:/work:rw",
        "-w", "/work",
        "--user", f"{uid}:{uid}",
        "--cpus", CPUS,
        "--memory", MEMORY, "--memory-swap", MEMORY,
        "--pids-limit", PIDS,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--env", "HOME=/home/sandbox",
        "--env", "npm_config_cache=/tmp/.npm",
        "--env", "PIP_CACHE_DIR=/tmp/.pip",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "CI=1",
        image, "sh", "-c", cmd,
    ]
    if RUNTIME == "runc":
        args.remove("--runtime"); args.remove("runc")
    return args


def run_step(step: str, stack: str, workdir_host: str, cmd: str, network: bool = False,
             timeout: int | None = None) -> StepResult:
    """Run one command in a brand-new sandbox container and destroy it."""
    timeout = timeout or STEP_TIMEOUT
    name = f"svx-{step}-{uuid.uuid4().hex[:8]}"
    start = time.time()

    if RUNTIME == "local":
        # Developer convenience only. NO isolation. Refuses unless explicitly allowed.
        if os.environ.get("ALLOW_UNSAFE_LOCAL") != "1":
            raise RuntimeError("SANDBOX_RUNTIME=local is unsafe; set ALLOW_UNSAFE_LOCAL=1 to use it for dev")
        argv = ["sh", "-c", cmd]
        cwd = workdir_host
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "CI": "1"}
    else:
        if not docker_available():
            raise RuntimeError("docker not found on host")
        argv = _docker_args(name, IMAGES[stack], workdir_host, cmd, network)
        cwd = None
        env = None

    timed_out = False
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
        code = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        code = 124
        out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        out += ((e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        out += f"\n[sandbox] wall-clock limit of {timeout}s hit, container killed\n"
        if RUNTIME != "local":
            subprocess.run(["docker", "kill", name], capture_output=True)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT // 2] + "\n[... output truncated ...]\n" + out[-MAX_OUTPUT // 2:]

    destroyed = False  # local dev mode has no container to destroy
    if RUNTIME != "local":
        chk = subprocess.run(["docker", "ps", "-a", "-q", "-f", f"name={name}"], capture_output=True, text=True)
        destroyed = chk.stdout.strip() == ""

    return StepResult(
        step=step, command=cmd, exit_code=code, duration_s=round(time.time() - start, 2), output=out,
        timed_out=timed_out, oom_killed=(code == 137 and not timed_out),
        network="bridge" if network else "none", container=name, destroyed=destroyed,
        signals=detect_signals(out, code, timed_out),
    )


def containment_profile() -> dict:
    """What the UI shows in the containment panel."""
    if RUNTIME == "local":
        return {k: "OFF (local dev mode, not isolated)" for k in (
            "runtime", "network_during_tests", "root_filesystem", "user", "cpus", "memory", "pids_limit",
            "wall_clock_s", "capabilities", "secrets_inside", "lifecycle")} | {"isolated": False}
    return {
        "runtime": RUNTIME,
        "isolated": RUNTIME != "local",
        "network_during_tests": "none",
        "root_filesystem": "read-only",
        "user": "non-root (uid " + os.environ.get("SANDBOX_UID", "1000") + ")",
        "cpus": CPUS, "memory": MEMORY, "pids_limit": PIDS,
        "wall_clock_s": STEP_TIMEOUT,
        "capabilities": "all dropped",
        "secrets_inside": "none (empty env, LLM calls on host)",
        "lifecycle": "fresh container per step, destroyed after",
    }
