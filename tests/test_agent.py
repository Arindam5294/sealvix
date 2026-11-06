"""Agent loop tests with a scripted, mocked Vultr inference endpoint (no network, no real model)."""
import json
import os
import time

os.environ.setdefault("SANDBOX_RUNTIME", "local")
os.environ.setdefault("ALLOW_UNSAFE_LOCAL", "1")

import httpx
import pytest

from app import agent, jobs

FAKE_KEY = "test-key-never-shown"

PY_CART_FIXED = '''def add_item(name, items=None):
    """Return a new cart with `name` added. Each call without `items` starts an empty cart."""
    if items is None:
        items = []
    items.append(name)
    return items
'''

NODE_DATES_FIXED = '''const DAY = 24 * 60 * 60 * 1000;

// Whole days from a to b, counting both ends (e.g. Mon..Wed = 3 days).
export function daysInclusive(a, b) {
  return Math.round((Date.parse(b) - Date.parse(a)) / DAY) + 1;
}

// Last calendar day of the given month (month is 1-12).
export function lastDayOfMonth(year, month) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}
'''


def _call(name, **args):
    return {"id": f"call_{name}_{time.time_ns()}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedModel:
    """Plays back one tool call per turn; records every request it receives."""

    def __init__(self, script, fail_first=None):
        self.script = list(script)
        self.requests = []
        self.fail_first = fail_first  # e.g. 429 on the primary model's first request

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"model": body["model"], "auth": request.headers.get("authorization"), "body": body})
        if self.fail_first and len(self.requests) == 1:
            return httpx.Response(self.fail_first, json={"error": "busy"})
        step = self.script.pop(0) if self.script else _call("finish", summary="out of script")
        msg = {"role": "assistant", "content": None, "tool_calls": [step]}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "tool_calls"}]})


@pytest.fixture
def model(monkeypatch):
    def install(script, fail_first=None):
        m = ScriptedModel(script, fail_first)
        monkeypatch.setattr(agent, "_TRANSPORT", httpx.MockTransport(m))
        return m
    monkeypatch.setenv("VULTR_INFERENCE_KEY", FAKE_KEY)
    monkeypatch.setenv("VULTR_MODEL", "glm-5.3")
    monkeypatch.setenv("VULTR_FALLBACK_MODEL", "deepseek-v4.1-flash-1017")
    return install


def _run(demo, tmp_path, monkeypatch, t=180):
    monkeypatch.setattr(jobs, "DATA_DIR", str(tmp_path))
    job = jobs.create_job("demo:" + demo, None)
    end = time.time() + t
    while time.time() < end:
        if job.events and job.events[-1]["type"] == "done":
            return job
        time.sleep(0.3)
    raise AssertionError("timeout")


def _agent_msgs(job):
    return [e["message"] for e in job.events if e["type"] == "agent"]


def test_py_cart_fixed_green(model, tmp_path, monkeypatch):
    m = model([
        _call("read_file", path="cart/__init__.py"),
        _call("write_file", path="cart/__init__.py", content=PY_CART_FIXED),
        _call("run_tests"),
        _call("finish", summary="mutable default argument"),
    ])
    job = _run("py-cart", tmp_path, monkeypatch)
    assert job.status == "green", (job.status, job.error, _agent_msgs(job))
    assert "items=None" in job.diff and job.diff.strip()
    assert [s["step"] for s in job.steps] == ["install", "baseline", "verify-1"]
    assert job.receipt["final"]["exit_code"] == 0
    # first request carries the baseline failure and the file list, the key only in the auth header
    first = m.requests[0]
    assert first["model"] == "glm-5.3" and first["auth"] == f"Bearer {FAKE_KEY}"
    user = first["body"]["messages"][1]["content"]
    assert "cart/__init__.py" in user and "UNTRUSTED" in user
    assert {t["function"]["name"] for t in first["body"]["tools"]} == {
        "list_files", "read_file", "write_file", "run_tests", "finish"}
    # the key never reaches the event stream
    assert FAKE_KEY not in json.dumps(job.events)
    assert any("write_file cart/__init__.py" in s for s in _agent_msgs(job))


def test_node_dates_fixed_green(model, tmp_path, monkeypatch):
    model([
        _call("list_files"),
        _call("read_file", path="src/dates.js"),
        _call("write_file", path="src/dates.js", content=NODE_DATES_FIXED),
        _call("run_tests"),
    ])
    job = _run("node-dates", tmp_path, monkeypatch)
    assert job.status == "green", (job.status, job.error, _agent_msgs(job))
    assert "+ 1;" in job.diff and "month, 0" in job.diff


def test_write_to_test_file_refused(model, tmp_path, monkeypatch):
    m = model([
        _call("write_file", path="tests/test_cart.py", content="def test_ok():\n    assert True\n"),
        _call("write_file", path="cart/new_module.py", content="x = 1\n"),
        _call("write_file", path="../escape.py", content="x = 1\n"),
        _call("write_file", path="cart/__init__.py", content=PY_CART_FIXED),
        _call("run_tests"),
    ])
    job = _run("py-cart", tmp_path, monkeypatch)
    refused = [s for s in _agent_msgs(job) if s.startswith("refused:")]
    assert len(refused) == 3, _agent_msgs(job)
    assert "tests are read-only" in refused[0]
    # the refusal went back to the model as the tool result
    tool_msgs = [x for x in m.requests[1]["body"]["messages"] if x["role"] == "tool"]
    assert tool_msgs[-1]["content"].startswith("refused: tests are read-only")
    assert "tests/" not in job.diff and job.status == "green"


def test_fallback_on_429(model, tmp_path, monkeypatch):
    m = model([_call("write_file", path="cart/__init__.py", content=PY_CART_FIXED), _call("run_tests")],
              fail_first=429)
    job = _run("py-cart", tmp_path, monkeypatch)
    assert m.requests[0]["model"] == "glm-5.3" and m.requests[1]["model"] == "deepseek-v4.1-flash-1017"
    assert job.status == "green"


def test_test_run_cap_and_baseline_return(model, tmp_path, monkeypatch):
    # model never fixes anything: 4 run_tests requests, only 3 execute; status failed
    model([_call("run_tests")] * 4 + [_call("finish", summary="gave up")])
    job = _run("py-cart", tmp_path, monkeypatch)
    assert [s["step"] for s in job.steps] == ["install", "baseline", "verify-1", "verify-2", "verify-3"]
    assert job.status == "failed" and job.diff == ""


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("VULTR_INFERENCE_KEY", raising=False)

    class J:
        events = []
        def emit(self, t, **d): self.events.append({"type": t, **d})
    j = J()
    assert agent.fix(j, None, None) is None
    assert "no VULTR_INFERENCE_KEY" in j.events[0]["message"]


def test_trim_keeps_tail():
    s = "A" * 5000 + "TAIL_FAILURE"
    t = agent.trim(s, 1200)
    assert t.endswith("TAIL_FAILURE") and len(t) < 1300
