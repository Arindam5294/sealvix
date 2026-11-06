"""Detect a repo's stack and the commands to install and test it."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class Stack:
    name: str            # node | python
    install: str         # runs with network (no secrets)
    test: str            # runs with --network none
    files: list[str]     # source files worth showing the model


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache", ".deps"}
SRC_EXT = {".js", ".mjs", ".cjs", ".ts", ".py", ".json", ".toml", ".cfg", ".txt", ".md"}


def list_files(root: str, limit: int = 200) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if os.path.splitext(f)[1] in SRC_EXT:
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
                if len(out) >= limit:
                    return sorted(out)
    return sorted(out)


def detect(root: str) -> Stack:
    files = list_files(root)
    pkg = os.path.join(root, "package.json")
    if os.path.exists(pkg):
        try:
            scripts = json.load(open(pkg)).get("scripts", {})
        except Exception:
            scripts = {}
        has_lock = os.path.exists(os.path.join(root, "package-lock.json"))
        install = ("npm ci" if has_lock else "npm install") + " --ignore-scripts --no-audit --no-fund"
        test = "npm test --silent" if "test" in scripts else "node --test"
        return Stack("node", install, test, files)

    py_markers = ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini"]
    if any(os.path.exists(os.path.join(root, m)) for m in py_markers) or any(f.endswith(".py") for f in files):
        # Install into a job-local dir so the read-only root FS is never touched.
        parts = ["pip install --no-input --disable-pip-version-check -q --target .deps pytest"]
        if os.path.exists(os.path.join(root, "requirements.txt")):
            parts.append("pip install --no-input --disable-pip-version-check -q --target .deps -r requirements.txt")
        install = " && ".join(parts)
        test = "PYTHONPATH=.deps:. python -m pytest -q -s -p no:cacheprovider"
        return Stack("python", install, test, files)

    raise ValueError("Unsupported repo: need package.json (Node) or Python files")
