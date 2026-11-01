"""The program under test and how to start it in a real terminal, for every real-input UAT file.

The target is the published release binary when ``EARTHWAVE_EXE`` names one (the plan's meaning of "against the
published artifact"), and the source tree (``python -m earthwave``) otherwise, so the same tests run on a laptop and in
CI. ``TARGET`` says which, and goes into every skip reason and result file.

State isolation. The kit points XDG_* at a scratch directory and ``HOME`` is added here for macOS. platformdirs on
Windows ignores the environment, so on Windows config and history live in the real profile: tests that need either
declare ``needs_state`` and run only where ``UAT_DISPOSABLE=1`` says the machine is thrown away afterwards (a CI runner,
Windows Sandbox). Everything else uses ``--no-config`` and ``--replay``/``--offline`` and touches nothing.

Environment: ``UAT_NETWORK=1`` allows tests that need the real feeds. ``UAT_ORIGIN_DIR`` is the S8b fake origin's work
directory (hosts file redirected, only ever set on a disposable runner). ``UAT_SIZE`` overrides the default 130x32.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from uat_kit import Terminal, unavailable_reason

EXE = os.environ.get("EARTHWAVE_EXE") or None
TARGET = "exe" if EXE else "source"
IS_WINDOWS = sys.platform == "win32"
DISPOSABLE = os.environ.get("UAT_DISPOSABLE") == "1"
NETWORK = os.environ.get("UAT_NETWORK") == "1"
ORIGIN_DIR = os.environ.get("UAT_ORIGIN_DIR") or None
REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
REPLAY = FIXTURES / "replay"
USGS = REPLAY / "usgs_short.jsonl"
KAM = REPLAY / "kamchatka_watch_to_warning.jsonl"
DEFAULT_SIZE = tuple(int(v) for v in os.environ.get("UAT_SIZE", "130x32").split("x"))
BASE = ["--hours", "720", "--min-mag", "2.5"]  # what the usgs_short recording needs to show its quakes

KIT = unavailable_reason()
needs_kit = pytest.mark.skipif(KIT is not None, reason=str(KIT))
needs_state = pytest.mark.skipif(
    IS_WINDOWS and not DISPOSABLE,
    reason="config and history live in the real profile on Windows (platformdirs ignores the environment, #230); "
    "set UAT_DISPOSABLE=1 on a runner or in Sandbox",
)
needs_network = pytest.mark.skipif(not NETWORK, reason="needs the real feeds: UAT_NETWORK=1")
needs_origin = pytest.mark.skipif(not ORIGIN_DIR, reason="needs the S8b fake origin on a disposable runner: UAT_ORIGIN_DIR")
needs_posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only behaviour")
needs_windows = pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only behaviour")


def program() -> list[str]:
    """argv of the program, without options."""
    if EXE:
        return [EXE]
    return [sys.executable, "-m", "earthwave"]


def sha256_of_exe() -> str | None:
    return hashlib.sha256(Path(EXE).read_bytes()).hexdigest() if EXE and Path(EXE).is_file() else None


def isolated_env(**extra: str) -> dict[str, str]:
    """HOME for macOS (platformdirs reads ~/Library there); the kit's isolation covers XDG and APPDATA."""
    env = {"HOME": "{dir}/home"} if not IS_WINDOWS else {}
    env.update(extra)
    return env


@contextmanager
def launch(
    *args: str,
    size: tuple[int, int] | None = None,
    env: dict[str, str] | None = None,
    env_remove: tuple[str, ...] = ("NO_COLOR",),
    no_config: bool = True,
    isolate: bool = True,
    log: bool = True,
) -> Iterator[Terminal]:
    """The program in a pseudo-terminal in an empty directory, ended by PID on exit.

    ``--no-config`` is added unless ``no_config=False``; a ``--log`` file in the scratch directory unless ``log=False``.
    ``env_remove`` defaults to dropping NO_COLOR: Gilead's shell sets it and a test that wants it passes it in ``env``.
    ``WT_SESSION`` is set so the app picks its Windows Terminal glyphs; tests of other environments remove it.
    """
    cols, rows = size or DEFAULT_SIZE
    argv = [*program()]
    if no_config:
        argv.append("--no-config")
    if log:
        argv += ["--log", "{dir}/earthwave.log"]
    argv += list(args)
    base = {"WT_SESSION": "uat"} if IS_WINDOWS else {}
    base.update(isolated_env())
    base.update(env or {})
    with Terminal(argv, cols, rows, env=base, env_remove=env_remove, isolate=isolate) as term:
        yield term


def log_text(term: Terminal) -> str:
    path = term.dir / "earthwave.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def ready(term: Terminal, timeout: float = 45.0) -> None:
    """Wait until the dashboard shows at least one quake row and the footer."""
    term.wait_for(lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)) and " Quit" in s.text, timeout, "dashboard with quakes")
    term.wait_for(lambda s: len(list_rows(s.lines)) >= 1, 10, "list rows drawn")
    term.settle(0.5)


ROW = re.compile(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d")
SELECTED = re.compile(r"^[▌>]\s*\d+[smhd]\s+\d\.\d")


def list_rows(lines) -> list[tuple[int, str]]:
    """(screen row, text) for every quake row of the dashboard list (left 72 columns)."""
    return [(i, ln[:72].rstrip()) for i, ln in enumerate(lines) if ROW.match(ln[:72])]


def selected_row(lines) -> tuple[int, str] | None:
    for i, ln in enumerate(lines):
        if SELECTED.match(ln[:72]):
            return i, ln[:72].rstrip()
    return None


def ident(row: str | None) -> str | None:
    """A list row without its cursor bar and age (the age ticks): magnitude, depth and place."""
    return None if row is None else re.sub(r"^\S?\s*\d+[smhd]\s+", "", row)


def status_line(text: str) -> str:
    return next((ln for ln in text.splitlines() if " shown of " in ln), "")


def footer_cell(term: Terminal, label: str) -> tuple[int, int]:
    lines = term.screen().lines
    row = len(lines) - 1
    col = lines[row].find(label)
    assert col >= 0, f"footer has no {label!r}: {lines[row]!r}"
    return col + 1, row


OUT = Path(os.environ["UAT_OUT"]) if os.environ.get("UAT_OUT") else None


def evidence(term: Terminal, test: str, name: str) -> str:
    """The screen as text, written to ``$UAT_OUT/<test>/<name>.txt`` when UAT_OUT is set (CI uploads the folder), and
    returned. Frames are for a person to read as well as for assertions."""
    text = term.screen_text()
    if OUT is not None:
        folder = OUT / TARGET / test
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.txt").write_text(f"{term.cols}x{term.rows}\n{text}\n", encoding="utf-8")
    return text


@contextmanager
def scratch() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ew-uat-") as tmp:
        yield Path(tmp)
