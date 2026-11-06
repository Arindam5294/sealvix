"""Skip the end-to-end tests when the tools they drive are not installed.

They clone demo repos with git (and run node for node-dates). Inside a
minimal sandbox, such as Sealvix testing its own repo, those tools are
absent on purpose, so the tests skip instead of failing.
"""
import shutil

import pytest

NEEDS_GIT = {
    "test_py_cart_fixed_green",
    "test_node_dates_fixed_green",
    "test_write_to_test_file_refused",
    "test_fallback_on_429",
    "test_test_run_cap_and_baseline_return",
    "test_end_to_end_local",
}


def pytest_collection_modifyitems(config, items):
    has_git = shutil.which("git") is not None
    has_node = shutil.which("node") is not None
    for item in items:
        name = item.originalname or item.name
        if name in NEEDS_GIT and not has_git:
            item.add_marker(pytest.mark.skip(reason="git not installed"))
        elif "node" in item.name and not has_node:
            item.add_marker(pytest.mark.skip(reason="node not installed"))
