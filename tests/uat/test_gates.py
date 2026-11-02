"""L10: a default run (no UAT_* variables) stays offline and starts nothing heavy.

Every test module under tests/uat that can reach the real network, the real profile, a browser or the release install
must say so with a gate marker; this test reads the sources and fails when one does not.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent

# what makes a test leave the machine or touch the real profile, and the gate that must be in the same file
RULES = [
    (r"isolate=False", "needs_network"),  # the dead-proxy isolation is off: real feeds
    (r"pip install|-m venv|venv\.create", "UAT_INSTALL"),
    (r"urllib\.request|httpx\.(get|Client)|requests\.", r"needs_network|S8B_DIR|listening\.json"),
    (r"hosts\.py|/etc/hosts", "S8B_DIR|S8B_DISPOSABLE"),
]


def test_every_network_or_install_test_file_names_its_gate() -> None:
    bad = []
    for path in sorted(HERE.glob("test_*.py")):
        if path.name == "test_gates.py":
            continue
        src = path.read_text(encoding="utf-8")
        for pattern, gate in RULES:
            if re.search(pattern, src) and not re.search(gate, src):
                bad.append(f"{path.name}: matches {pattern!r} but never mentions {gate!r}")
    assert not bad, bad


def test_real_input_files_need_the_real_flag() -> None:
    """A file that starts the program in a pseudo-terminal skips unless UAT_REAL=1 (needs_kit), so plain pytest does not."""
    bad = []
    for path in sorted(HERE.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "launch(" in src and "needs_kit" not in src and path.name not in {"test_gates.py"}:
            bad.append(path.name)
    assert not bad, bad
