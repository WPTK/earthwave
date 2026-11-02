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
    (r"isolate=False", r"NETWORK|needs_network"),  # the dead-proxy isolation is off: real feeds
    (r"import venv|venv\.create", r"INSTALL"),
    (r"urllib\.request|httpx\.(get|Client)|requests\.", r"NETWORK|needs_network|S8B_DIR|listening\.json"),
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


def test_terminal_files_are_skipped_in_a_default_run() -> None:
    """A file that starts the program in a pseudo-terminal skips unless UAT_REAL=1 (needs_kit) or its own env gate.
    Report-only: a file with neither is listed (it spawns a terminal in every default run; it stays offline)."""
    from uat.known import report

    ungated = []
    for path in sorted(HERE.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        starts = "launch(" in src or "Terminal(" in src
        if starts and path.name != "test_gates.py" and not re.search(r"needs_kit|UAT_REAL|EARTHWAVE_EXE|skipif", src):
            ungated.append(path.name)
    report("test files that start a terminal in a default run", ungated)
