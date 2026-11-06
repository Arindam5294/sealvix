import os, time, json
os.environ.setdefault("SANDBOX_RUNTIME", "local")
os.environ.setdefault("ALLOW_UNSAFE_LOCAL", "1")
import pytest
from app import jobs, sandbox, stack
from app.jobs import Toolbox, Job

HERE = os.path.dirname(__file__)
DEMOS = os.path.join(HERE, "..", "demo-repos")


def test_detect():
    assert stack.detect(os.path.join(DEMOS, "node-dates")).name == "node"
    assert stack.detect(os.path.join(DEMOS, "py-cart")).name == "python"


def test_test_file_rule():
    for p in ["tests/test_x.py", "test/dates.test.js", "src/a.spec.ts", "conftest.py", "pkg/__tests__/a.js"]:
        assert Toolbox.is_test_file(p), p
    for p in ["src/dates.js", "cart/__init__.py", "contest.py"]:
        assert not Toolbox.is_test_file(p), p


def test_signals():
    out = "[poison] exfil blocked: <urlopen error [Errno -3] Temporary failure in name resolution>\n" \
          "write /etc/passwd blocked: [Errno 30] Read-only file system\nfork: [Errno 11] Resource temporarily unavailable"
    s = sandbox.detect_signals(out, 0, False)
    assert {"network_blocked", "readonly_fs", "pids_cap"} <= set(s)
    assert "wall_clock_cap" in sandbox.detect_signals("", 124, True)


def test_github_url_filter():
    assert jobs.GITHUB_RE.match("https://github.com/a/b")
    assert jobs.GITHUB_RE.match("https://github.com/a/b.git")
    for bad in ["http://github.com/a/b", "https://github.com/a/b/../../x", "file:///etc", "https://evil.com/a/b",
                "https://github.com/a/b;rm -rf /", "--upload-pack=x"]:
        assert not jobs.GITHUB_RE.match(bad), bad


def test_docker_args_locked_down():
    a = sandbox._docker_args("n", "img", "/tmp/w", "true", network=False)
    j = " ".join(a)
    for flag in ["--rm", "--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit",
                 "--memory", "--cpus", "--user 1000:1000"]:
        assert flag in j, flag
    assert "VULTR" not in j and "KEY" not in j


def _wait(job, t=180):
    end = time.time() + t
    while time.time() < end:
        if job.events and job.events[-1]["type"] == "done":
            return
        time.sleep(0.3)
    raise AssertionError("timeout")


@pytest.mark.parametrize("demo", ["py-cart", "node-dates"])
def test_end_to_end_local(demo, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VULTR_INFERENCE_KEY", raising=False)  # no key: agent is skipped
    job = jobs.create_job("demo:" + demo, None)
    _wait(job)
    assert job.status == "awaiting_agent", (job.status, job.error, [s["output"][-500:] for s in job.steps])
    base = [s for s in job.steps if s["step"] == "baseline"][0]
    assert base["exit_code"] != 0
    assert job.receipt and len(job.receipt["receipt_sha256"]) == 64
    assert os.path.exists(tmp_path / "receipts" / f"{job.id}.json")
    assert not os.path.exists(job.workdir)


def test_toolbox_confinement(tmp_path):
    job = Job(id="t", source="demo:x", branch=None)
    os.makedirs(job.workdir, exist_ok=True)
    try:
        open(os.path.join(job.workdir, "a.py"), "w").write("x=1\n")
        os.makedirs(os.path.join(job.workdir, "tests"), exist_ok=True)
        open(os.path.join(job.workdir, "tests", "test_a.py"), "w").write("")
        tb = Toolbox(job, stack.Stack("python", "", "", []))
        assert tb.write_file("a.py", "x=2\n") == "ok"
        for bad in ["../escape.py", "/etc/passwd", "tests/test_a.py", "new.py", ".git/config"]:
            with pytest.raises(PermissionError):
                tb.write_file(bad, "x")
        with pytest.raises(PermissionError):
            tb.read_file("../../../../etc/passwd")
    finally:
        import shutil; shutil.rmtree(job.workdir, ignore_errors=True)
