"""Job lifecycle: clone -> install -> baseline tests -> agent fix loop -> verify -> receipt."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import sandbox, stack as stackmod

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
DEMO_DIR = os.environ.get("DEMO_DIR", os.path.join(os.path.dirname(__file__), "..", "demo-repos"))
GITHUB_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?$")
MAX_REPO_MB = int(os.environ.get("MAX_REPO_MB", "50"))
JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
_ACTIVE = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))


@dataclass
class Job:
    id: str
    source: str
    branch: str | None
    status: str = "queued"  # queued|running|green|already_green|failed|awaiting_agent|error
    created: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    stack: str | None = None
    commit: str | None = None
    diff: str = ""
    receipt: dict | None = None
    error: str | None = None

    @property
    def workdir(self) -> str:
        return os.path.join(DATA_DIR, "work", self.id)

    def emit(self, type_: str, **data):
        self.events.append({"i": len(self.events), "t": round(time.time() - self.created, 2), "type": type_, **data})

    def public(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "source", "branch", "status", "created", "stack", "commit", "diff", "receipt", "error")} | {
            "steps": [{k: v for k, v in s.items() if k != "output"} for s in self.steps]}


def demo_names() -> list[str]:
    if not os.path.isdir(DEMO_DIR):
        return []
    return sorted(d for d in os.listdir(DEMO_DIR) if os.path.isdir(os.path.join(DEMO_DIR, d)))


def _sh(argv, cwd=None, timeout=120):
    env = {"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0", "HOME": "/tmp"}
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _dir_mb(path: str) -> float:
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(dp, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total / 1e6


def _fetch_source(job: Job):
    os.makedirs(os.path.dirname(job.workdir), exist_ok=True)
    if job.source.startswith("demo:"):
        name = job.source[5:]
        if name not in demo_names():
            raise ValueError(f"unknown demo repo {name}")
        shutil.copytree(os.path.join(DEMO_DIR, name), job.workdir, symlinks=False)
        if not os.path.isdir(os.path.join(job.workdir, ".git")):
            _sh(["git", "init", "-q"], cwd=job.workdir)
            _sh(["git", "add", "-A"], cwd=job.workdir)
            _sh(["git", "-c", "user.email=sealvix@local", "-c", "user.name=sealvix", "commit", "-qm", "demo"], cwd=job.workdir)
    else:
        if not GITHUB_RE.match(job.source):
            raise ValueError("only public https://github.com/<owner>/<repo> URLs are accepted")
        argv = ["git", "clone", "--depth", "1", "--no-tags", "--single-branch"]
        if job.branch:
            argv += ["--branch", job.branch]
        # Clone happens on the host with no credentials; nothing from the repo is executed here.
        r = _sh(argv + ["--", job.source, job.workdir], timeout=90)
        if r.returncode != 0:
            raise ValueError("clone failed: " + r.stderr.strip()[-300:])
        if _dir_mb(job.workdir) > MAX_REPO_MB:
            raise ValueError(f"repo larger than {MAX_REPO_MB} MB")
    job.commit = _sh(["git", "rev-parse", "HEAD"], cwd=job.workdir).stdout.strip() or None
    # Sandbox user (uid 1000) must own the job copy; host keeps the pristine git history.
    if os.geteuid() == 0:
        uid = int(os.environ.get("SANDBOX_UID", "1000"))
        for dp, dns, fns in os.walk(job.workdir):
            for n in dns + fns:
                os.lchown(os.path.join(dp, n), uid, uid)
        os.lchown(job.workdir, uid, uid)


def _record(job: Job, res: sandbox.StepResult) -> sandbox.StepResult:
    d = res.to_dict()
    d["output_sha256"] = hashlib.sha256(res.output.encode()).hexdigest()
    job.steps.append(d)
    job.emit("step", step=res.step, exit_code=res.exit_code, duration_s=res.duration_s, network=res.network,
             container=res.container, destroyed=res.destroyed, signals=res.signals, timed_out=res.timed_out,
             output=res.output[-20_000:])
    for s in res.signals:
        job.emit("containment", step=res.step, control=s)
    if res.destroyed:
        job.emit("destroyed", step=res.step, container=res.container)
    return res


class Toolbox:
    """What the agent may do. All file access is confined to the job copy; tests are read-only to the agent."""

    def __init__(self, job: Job, st: stackmod.Stack):
        self.job, self.st = job, st
        self.root = os.path.realpath(job.workdir)
        self.test_runs = 0

    def _safe(self, rel: str) -> str:
        p = os.path.realpath(os.path.join(self.root, rel))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise PermissionError("path escapes the repo")
        if "/.git" in p[len(self.root):] or "/.deps" in p[len(self.root):] or "/node_modules" in p[len(self.root):]:
            raise PermissionError("path is off-limits")
        return p

    @staticmethod
    def is_test_file(rel: str) -> bool:
        r = rel.lower()
        return bool(re.search(r"(^|/)(tests?|__tests__|spec)(/|$)", r) or re.search(r"(\.|_)(test|spec)\.", r)
                    or re.search(r"(^|/)test_[^/]+\.py$", r) or r.endswith("conftest.py"))

    def list_files(self) -> list[str]:
        return stackmod.list_files(self.root)

    def read_file(self, rel: str, max_bytes: int = 60_000) -> str:
        p = self._safe(rel)
        with open(p, "r", errors="replace") as f:
            return f.read(max_bytes)

    def write_file(self, rel: str, content: str) -> str:
        if self.is_test_file(rel):
            raise PermissionError("tests are read-only: fix the code, not the test")
        p = self._safe(rel)
        if not os.path.exists(p):
            raise PermissionError("only existing source files may be edited")
        with open(p, "w") as f:
            f.write(content)
        self.job.emit("edit", path=rel)
        return "ok"

    def run_tests(self) -> sandbox.StepResult:
        self.test_runs += 1
        return _record(self.job, sandbox.run_step(f"verify-{self.test_runs}", self.st.name, self.job.workdir,
                                                  self.st.test, network=False))


def _diff(job: Job) -> str:
    return _sh(["git", "-c", "safe.directory=*", "-c", "core.fileMode=false", "diff", "--", ".", ":(exclude).deps", ":(exclude)node_modules"],
               cwd=job.workdir).stdout


def _receipt(job: Job, baseline: sandbox.StepResult, final: sandbox.StepResult | None) -> dict:
    body = {
        "job": job.id, "source": job.source, "commit": job.commit, "stack": job.stack,
        "diff_sha256": hashlib.sha256(job.diff.encode()).hexdigest(),
        "baseline": {"exit_code": baseline.exit_code, "output_sha256": hashlib.sha256(baseline.output.encode()).hexdigest()},
        "final": None if final is None else {"exit_code": final.exit_code,
                                             "output_sha256": hashlib.sha256(final.output.encode()).hexdigest()},
        "containers": [{"name": s["container"], "network": s["network"], "destroyed": s["destroyed"],
                        "signals": s["signals"]} for s in job.steps],
        "containment": sandbox.containment_profile(),
        "status": job.status,
        "finished_at": int(time.time()),
    }
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["receipt_sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    return body


def _save(job: Job):
    os.makedirs(os.path.join(DATA_DIR, "receipts"), exist_ok=True)
    with open(os.path.join(DATA_DIR, "receipts", job.id + ".json"), "w") as f:
        json.dump(job.public(), f, indent=2)


def run_job(job: Job):
    with _ACTIVE:
        job.status = "running"
        job.emit("status", status="running")
        baseline = final = None
        try:
            job.emit("phase", phase="clone", detail="fetching repo on host (no credentials, nothing executed)")
            _fetch_source(job)
            st = stackmod.detect(job.workdir)
            job.stack = st.name
            job.emit("phase", phase="detect", stack=st.name, install=st.install, test=st.test)

            job.emit("phase", phase="install", detail="fresh sandbox, network on for packages, no secrets")
            inst = _record(job, sandbox.run_step("install", st.name, job.workdir, st.install, network=True, timeout=240))
            if inst.exit_code != 0:
                raise RuntimeError("dependency install failed in sandbox")

            job.emit("phase", phase="baseline", detail="fresh sandbox, network none")
            baseline = _record(job, sandbox.run_step("baseline", st.name, job.workdir, st.test, network=False))
            if baseline.exit_code == 0:
                job.status = "already_green"
                final = baseline
            else:
                job.emit("phase", phase="agent", detail="diagnose -> patch -> verify")
                from . import agent  # model loop lives on the host only
                tb = Toolbox(job, st)
                final = agent.fix(job, tb, baseline)
                if final is None:
                    job.status = "awaiting_agent"
                else:
                    job.status = "green" if final.exit_code == 0 else "failed"
            job.diff = _diff(job)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)[:500]
            job.emit("error", message=job.error)
        finally:
            if baseline is not None:
                job.receipt = _receipt(job, baseline, final)
                job.emit("receipt", receipt=job.receipt)
            job.emit("done", status=job.status, diff=job.diff)
            _save(job)
            shutil.rmtree(job.workdir, ignore_errors=True)


def create_job(source: str, branch: str | None) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], source=source.strip(), branch=(branch or None))
    with _LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job
