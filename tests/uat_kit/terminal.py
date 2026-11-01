"""uat_kit: thin, synchronous helpers on Microsoft tui-test (ConPTY on Windows, a pty elsewhere). No project imports.

tui-test runs the program in a real pseudo-terminal and renders its output in a terminal emulator; its mouse sends real
SGR reports and its resize is a real window-size change. This module adds what a test suite wants on top: a blocking
API (tui-test is async), an isolated environment, polling waits that fail with a screen dump, pointer motion before
each click (a real terminal reports motion before every click), and a kill by PID that leaves no process behind.

Usage::

    from uat_kit import Terminal
    with Terminal(["myapp", "--config", "{dir}/c.toml"], 120, 30, env={"MYAPP_CACHE": "{dir}/cache"}) as t:
        t.wait_for_text("Stations", 30)       # polls the screen; on timeout fails with a screen dump
        t.send_keys("down", "enter", "q")      # named keys, or literal text
        t.click(10, 5); t.click_text("ETHB")   # motion, press, release at 0-based cells
        t.resize(60, 16); t.settle(); print(t.screen_text()); t.wait_exit()

``{dir}`` in an argument or an ``env`` value is a scratch directory that is removed on close. The environment is
isolated: config and cache variables point into that directory and a dead proxy makes every network request fail.
Pass ``env`` to add variables and ``env_remove`` to drop some. Windows: set ``PYTHONUTF8=1`` in the test process too,
or printing box-drawing characters raises UnicodeEncodeError.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

try:
    from tui_test import TuiTest
except ImportError:  # pragma: no cover - reported as a skip reason
    TuiTest = None  # type: ignore[assignment,misc]

IS_WINDOWS = sys.platform == "win32"
ESC = "\x1b"
DEFAULT_TIMEOUT = 20.0

KEYS = {
    "enter": "\r",
    "escape": ESC,
    "esc": ESC,
    "tab": "\t",
    "shift+tab": f"{ESC}[Z",
    "up": f"{ESC}[A",
    "down": f"{ESC}[B",
    "right": f"{ESC}[C",
    "left": f"{ESC}[D",
    "home": f"{ESC}[H",
    "end": f"{ESC}[F",
    "pageup": f"{ESC}[5~",
    "pagedown": f"{ESC}[6~",
    "space": " ",
}


def unavailable_reason() -> str | None:
    """Why the real-terminal layer cannot run here, or None when it can."""
    if TuiTest is None:
        return "tui-test is not installed (pip install -e '.[dev]')"
    return None


@dataclass(frozen=True)
class Screen:
    """A snapshot of the emulated screen, exactly ``rows`` lines with trailing blanks trimmed."""

    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def __contains__(self, item: str) -> bool:
        return item in self.text

    def find(self, needle: str) -> tuple[int, int] | None:
        """(column, row), both 0-based, of the first place ``needle`` appears, or None."""
        for row, line in enumerate(self.lines):
            col = line.find(needle)
            if col >= 0:
                return col, row
        return None

    def rows_with(self, needle: str) -> list[int]:
        return [row for row, line in enumerate(self.lines) if needle in line]


class Terminal:
    """The program running in a pseudo-terminal of ``cols`` x ``rows`` cells."""

    def __init__(
        self,
        argv: list[str],
        cols: int = 120,
        rows: int = 30,
        *,
        env: dict[str, str] | None = None,
        env_remove: tuple[str, ...] = (),
        cwd: Path | None = None,
        isolate: bool = True,
    ) -> None:
        reason = unavailable_reason()
        if reason:
            raise RuntimeError(reason)
        self.cols, self.rows = cols, rows
        self._dir = Path(tempfile.mkdtemp(prefix="uat-kit-"))
        self.dir = self._dir  # a scratch directory the caller may use for config files; removed on close
        self._closed = False
        self._pids: set[int] = set()
        self._loop = asyncio.new_event_loop()
        _LIVE.add(self)
        argv = [a.replace("{dir}", str(self._dir)) for a in argv]
        child_env = self._env(env or {}, env_remove, isolate)
        before = {p.pid for p in psutil.Process().children(recursive=True)}
        self._cm = TuiTest.ephemeral()
        self._t: Any = self._run(self._cm.__aenter__())
        try:
            self._run(self._t.run(argv[0], *argv[1:], cols=cols, rows=rows, cwd=str(cwd or self._dir), env=child_env))
        except BaseException:
            self.close()
            raise
        self._pids = {p.pid for p in psutil.Process().children(recursive=True)} - before

    def _run(self, coro: Any) -> Any:
        return self._loop.run_until_complete(coro)

    def _env(self, extra: dict[str, str], remove: tuple[str, ...], isolate: bool) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTEST") and k not in remove}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", PYTHONUNBUFFERED="1", PYTHONUTF8="1")
        if isolate:
            dead = "http://127.0.0.1:9"  # nothing listens on the discard port: a request fails at once
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[name] = dead
            env.update(
                NO_PROXY="",
                no_proxy="",
                XDG_CONFIG_HOME=str(self._dir / "xdg-config"),
                XDG_CACHE_HOME=str(self._dir / "xdg-cache"),
                XDG_DATA_HOME=str(self._dir / "xdg-data"),
            )
            if IS_WINDOWS:
                env.update(APPDATA=str(self._dir / "appdata"), LOCALAPPDATA=str(self._dir / "localappdata"))
        for name, value in extra.items():
            env[name] = value.replace("{dir}", str(self._dir))
        return env

    # -- process ----------------------------------------------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        """The PID of the program (the first process the terminal started), if it could be identified."""
        return min(self._pids) if self._pids else None

    def alive(self) -> bool:
        return self.exit_code() is None

    def exit_code(self) -> int | None:
        return self._run(self._t.state()).exited  # None while the program runs

    def wait_exit(self, timeout: float = DEFAULT_TIMEOUT) -> int | None:
        """Wait for the program to end on its own; its exit code, or None on timeout."""
        try:
            self._run(self._t.wait_exit(timeout=int(timeout * 1000)))
        except Exception:
            return None
        return self.exit_code()

    def kill(self) -> None:
        """End the program and everything it started, by PID."""
        procs = []
        for pid in self._pids:
            with contextlib.suppress(psutil.NoSuchProcess):
                parent = psutil.Process(pid)
                procs += [*parent.children(recursive=True), parent]
        for p in procs:
            with contextlib.suppress(psutil.NoSuchProcess):
                p.kill()
        psutil.wait_procs(procs, timeout=5)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.kill()
        with contextlib.suppress(Exception):
            self._run(self._t.kill())
        with contextlib.suppress(Exception):
            self._run(self._cm.__aexit__(None, None, None))
        self.kill()
        self._loop.close()
        _LIVE.discard(self)
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> Terminal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- input ------------------------------------------------------------------------------------------------------

    def send_raw(self, data: str) -> None:
        self._run(self._t.write(data))

    def send_keys(self, *keys: str) -> None:
        """Each argument is a named key (``"enter"``, ``"escape"``, ``"down"``) or literal text typed as is."""
        for key in keys:
            self.send_raw(KEYS.get(key.lower(), key))
            time.sleep(0.08)  # a person is never faster; lets the app handle one key before the next

    def click(self, col: int, row: int) -> None:
        """Move the pointer in, press and release the left button, as a real terminal reports a click."""
        for c in (max(col - 4, 0), max(col - 1, 0), col):
            self._run(self._t.mouse.move(c, row))
            time.sleep(0.03)
        self._run(self._t.mouse.click(col, row))
        time.sleep(0.12)

    def click_text(self, needle: str) -> tuple[int, int]:
        """Click in the middle of the first place ``needle`` is on screen; returns the cell."""
        found = self.screen().find(needle)
        if found is None:
            raise AssertionError(f"{needle!r} is not on screen:\n{self.screen_text()}")
        col, row = found[0] + len(needle) // 2, found[1]
        self.click(col, row)
        return col, row

    def focus(self, gained: bool = True) -> None:
        self.send_raw(f"{ESC}[{'I' if gained else 'O'}")

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self._run(self._t.resize(cols, rows))

    # -- output -----------------------------------------------------------------------------------------------------

    def state(self) -> Any:
        """tui-test's terminal state: modes, mouse_mode, cursor, title."""
        return self._run(self._t.state())

    def screen(self) -> Screen:
        lines = [line.rstrip() for line in self._run(self._t.text()).split("\n")]
        return Screen(tuple((lines + [""] * self.rows)[: self.rows]))

    def screen_text(self) -> str:
        return self.screen().text

    def wait_for(self, predicate: Callable[[Screen], bool], timeout: float = DEFAULT_TIMEOUT, what: str = "") -> Screen:
        """Poll until ``predicate(screen)`` is true; on timeout fail with the screen as it was."""
        deadline = time.monotonic() + timeout
        while True:
            snap = self.screen()
            if predicate(snap):
                return snap
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        label = what or getattr(predicate, "__name__", "condition")
        state = "still running" if self.alive() else "the program has exited"
        raise AssertionError(f"timed out after {timeout:g} s waiting for {label} ({state}). Screen:\n{snap.text}")

    def wait_for_text(self, text: str, timeout: float = DEFAULT_TIMEOUT) -> Screen:
        return self.wait_for(lambda s: text in s, timeout, f"{text!r} on screen")

    def settle(self, quiet: float = 0.4, timeout: float = 10.0) -> Screen:
        """Wait until the screen has not changed for ``quiet`` seconds (the program is idle), then return it."""
        deadline = time.monotonic() + timeout
        last = self.screen()
        stamp = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.05)
            now = self.screen()
            if now != last:
                last, stamp = now, time.monotonic()
            elif time.monotonic() - stamp >= quiet:
                break
        return last

    def hold(self, seconds: float) -> None:
        """Let real time pass (for a stability check, where the point is that time passes)."""
        time.sleep(seconds)


_LIVE: set[Terminal] = set()


def _reap() -> None:
    for term in list(_LIVE):
        term.close()


atexit.register(_reap)
