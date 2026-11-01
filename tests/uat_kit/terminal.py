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
import json
import os
import re
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
    from tui_test.types import TraceOptions
except ImportError:  # pragma: no cover - reported as a skip reason
    TuiTest = None  # type: ignore[assignment,misc]
    TraceOptions = None  # type: ignore[assignment,misc]

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
    "backspace": "\x7f",
    "delete": f"{ESC}[3~",
    "insert": f"{ESC}[2~",
    "ctrl+c": "\x03",
    "ctrl+l": "\x0c",
    "ctrl+p": "\x10",
    "ctrl+q": "\x11",
    "ctrl+s": "\x13",
    "ctrl+z": "\x1a",
    "f1": f"{ESC}OP",
    "f2": f"{ESC}OQ",
    "f3": f"{ESC}OR",
    "f4": f"{ESC}OS",
    "f5": f"{ESC}[15~",
    "f6": f"{ESC}[17~",
    "f7": f"{ESC}[18~",
    "f8": f"{ESC}[19~",
    "f9": f"{ESC}[20~",
    "f10": f"{ESC}[21~",
    "f11": f"{ESC}[23~",
    "f12": f"{ESC}[24~",
}


def unavailable_reason() -> str | None:
    """Why the real-terminal layer cannot run here, or None when it can."""
    if TuiTest is None:
        return "tui-test is not installed (pip install -e '.[dev]')"
    return None


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def sgr_depth(raw: str) -> str:
    """The colour depth a byte stream asks for, from its SGR sequences: ``mono`` (none, or only bold/dim/reverse),
    ``16`` (30-37, 40-47, 90-97, 100-107), ``256`` (38;5;n or 48;5;n) or ``truecolor`` (38;2;r;g;b or 48;2;...)."""
    depth = 0
    for match in _SGR.finditer(raw):
        params = [p for p in re.split(r"[;:]", match.group(1)) if p != ""]
        i = 0
        while i < len(params):
            n = int(params[i])
            if n in (38, 48, 58) and i + 1 < len(params):
                depth = max(depth, 3 if params[i + 1] == "2" else 2)
                i += 2 + (3 if params[i + 1] == "2" else 1)
                continue
            if 30 <= n <= 37 or 40 <= n <= 47 or 90 <= n <= 97 or 100 <= n <= 107:
                depth = max(depth, 1)
            i += 1
    return ("mono", "16", "256", "truecolor")[depth]


def is_chromatic(hex_colour: str, spread: int = 24) -> bool:
    """True for a colour with real hue: ``#rrggbb`` whose channels differ by more than ``spread`` (greys are not)."""
    if hex_colour.isdigit():  # a palette index: the ANSI hues, or the 6x6x6 cube
        n = int(hex_colour)
        return 1 <= n <= 6 or 9 <= n <= 14 or 16 <= n <= 231
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        return False
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return False
    return max(r, g, b) - min(r, g, b) > spread


def sgr_chromatic(raw: str) -> int:
    """How many SGR colour settings in the stream have hue: truecolor triples that are not grey, 256-colour cube
    entries, and the coloured 16-colour codes (31-36, 41-46, 91-96, 101-106). Textual under NO_COLOR still sends greys."""
    count = 0
    for match in _SGR.finditer(raw):
        params = [p for p in re.split(r"[;:]", match.group(1)) if p != ""]
        i = 0
        while i < len(params):
            n = int(params[i])
            if n in (38, 48) and i + 1 < len(params):
                if params[i + 1] == "2" and i + 4 < len(params):
                    r, g, b = (int(x) for x in params[i + 2 : i + 5])
                    count += max(r, g, b) - min(r, g, b) > 24
                    i += 5
                else:
                    count += 16 <= int(params[i + 2] if i + 2 < len(params) else 0) <= 231
                    i += 3
                continue
            count += 31 <= n <= 36 or 41 <= n <= 46 or 91 <= n <= 96 or 101 <= n <= 106
            i += 1
    return count


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
        trace: bool = True,
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
        if os.name != "nt":  # trap: a program that reads COLUMNS/LINES (Textual does) sees 80x24 unless they match
            child_env.setdefault("COLUMNS", str(cols))
            child_env.setdefault("LINES", str(rows))
        before = {p.pid for p in psutil.Process().children(recursive=True)}
        self._trace_dir = self._dir / "_trace"
        self._raw = ""
        # a trace holds an asciicast (session.cast) of everything the program wrote, from the first byte; tui-test
        # writes it when the session closes, so the raw stream is readable after close(), not while the program runs
        self._cm = (
            TuiTest.ephemeral(trace=TraceOptions(mode="on", directory=str(self._trace_dir)))
            if trace
            else TuiTest.ephemeral()
        )
        self._t: Any = self._run(self._cm.__aenter__())
        try:
            self._run(self._t.run(argv[0], *argv[1:], cols=cols, rows=rows, cwd=str(cwd or self._dir), env=child_env))
        except BaseException:
            self.close()
            raise
        self._pids = {p.pid for p in psutil.Process().children(recursive=True)} - before
        if os.name != "nt":
            # trap: tui-test 0.1.1 on Linux and macOS starts the child at 80x24 whatever cols/rows say (the app showed
            # "Terminal too small: 80x24"); an explicit resize applies the size and the app redraws
            self.resize(cols, rows)

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
        self._raw = self._read_trace()
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
            time.sleep(0.1)  # a person is never faster; lets the app handle one key before the next
        time.sleep(
            0.3
        )  # and the last key is drawn before the caller looks (Textual repaints a few frames later; a lone
        # Escape is held back by the parser until it is sure no sequence follows, so a following key must not race it)

    def _approach(self, col: int, row: int) -> None:
        """Pointer motion into the cell, as a real terminal reports it before any press (any-motion mode, 1003)."""
        for c in (max(col - 4, 0), max(col - 1, 0), col):
            self._run(self._t.mouse.move(c, row))
            time.sleep(0.03)

    def click(
        self,
        col: int,
        row: int,
        *,
        button: str = "left",
        clicks: int = 1,
        shift: bool = False,
        ctrl: bool = False,
        alt: bool = False,
    ) -> None:
        """Move the pointer in, press and release a button, as a real terminal reports a click. ``clicks`` 2 or 3 are
        a double and a triple click (press/release pairs sent back to back, inside any application's click chain
        window). ``shift``/``ctrl``/``alt`` set the modifier bits of the report, as a terminal that passes them does."""
        self._approach(col, row)
        self._run(self._t.mouse.click(col, row, button=button, clicks=clicks, shift=shift, ctrl=ctrl, alt=alt))
        time.sleep(0.12)

    def right_click(self, col: int, row: int, **mods: bool) -> None:
        self.click(col, row, button="right", **mods)

    def middle_click(self, col: int, row: int, **mods: bool) -> None:
        self.click(col, row, button="middle", **mods)

    def double_click(self, col: int, row: int, **mods: bool) -> None:
        self.click(col, row, clicks=2, **mods)

    def triple_click(self, col: int, row: int, **mods: bool) -> None:
        self.click(col, row, clicks=3, **mods)

    def hover(self, col: int, row: int) -> None:
        """Motion only, ending on the cell, no button."""
        self._approach(col, row)
        time.sleep(0.1)

    def wheel(self, direction: str, amount: int = 1, at: tuple[int, int] | None = None) -> None:
        """Wheel ``direction`` (``up`` ``down`` ``left`` ``right``) ``amount`` notches with the pointer over ``at``."""
        if at is None:
            self._run(self._t.mouse.scroll(direction, amount=amount))  # tui-test reports these at cell (1, 1)
        else:
            # tui-test cannot place a wheel report (it always says column 1, row 1), so with a position the SGR report is
            # written as a terminal writes it: button 64 up, 65 down, 66 left, 67 right, 1-based cell.
            self._approach(*at)
            code = {"up": 64, "down": 65, "left": 66, "right": 67}[direction]
            for _ in range(amount):
                self.send_raw(f"\x1b[<{code};{at[0] + 1};{at[1] + 1}M")
                time.sleep(0.03)
        time.sleep(0.12)

    def drag(self, start: tuple[int, int], end: tuple[int, int], *, button: str = "left", steps: int = 4) -> None:
        """Press on ``start``, move through ``steps`` cells towards ``end`` with the button held, release on ``end``.
        ``end`` may lie outside the window (negative or past the last column): the release then reports there, as a
        terminal that keeps tracking outside the window does."""
        self._approach(*start)
        self._run(self._t.mouse.down(*start, button=button))
        time.sleep(0.05)
        for i in range(1, steps + 1):
            x = max(start[0] + round((end[0] - start[0]) * i / steps), 0)
            y = max(start[1] + round((end[1] - start[1]) * i / steps), 0)
            # tui-test's move() always reports "no button" (35); a terminal reports motion with a button held as
            # 32 + button, so that report is written directly.
            self.send_raw(f"\x1b[<{32 + {'left': 0, 'middle': 1, 'right': 2}[button]};{x + 1};{y + 1}M")
            time.sleep(0.04)
        self._run(
            self._t.mouse.up(max(end[0], 0), max(end[1], 0), button=button)
        )  # cells are never negative on the wire
        time.sleep(0.12)

    def mouse_down(self, col: int, row: int, *, button: str = "left") -> None:
        self._approach(col, row)
        self._run(self._t.mouse.down(col, row, button=button))

    def mouse_up(self, col: int, row: int, *, button: str = "left") -> None:
        self._run(self._t.mouse.up(col, row, button=button))
        time.sleep(0.12)

    def legacy_mouse(self, col: int, row: int, *, encoding: str = "x10", button: int = 0, release: bool = True) -> str:
        """Send a press (and release) in a legacy encoding, byte for byte as a terminal that has mouse mode 1000 but
        not 1006 would: ``x10``/``normal`` (``ESC [ M`` then three bytes, 32-based) or ``urxvt`` (``ESC [ b ; x ; y M``,
        mode 1015). ``button`` is 0 left, 1 middle, 2 right, 64 wheel up, 65 wheel down. Cells are 0-based; X10 cannot
        address past column 222. Returns the bytes sent. Real-input only in the sense that the bytes are the terminal's
        own format; the terminal itself did not generate them (this kit cannot make tui-test speak the old encodings)."""
        x, y = col + 1, row + 1

        def one(code: int) -> str:
            if encoding in ("x10", "normal", "1000"):
                if x > 95 or y > 95:
                    # X10 can address 223 cells, but past 95 the coordinate byte is above 0x7F, which cannot be written
                    # through tui-test (text is UTF-8 encoded: 0x85 would go out as C2 85). Not a real report then.
                    raise ValueError("X10 mouse encoding is limited to the first 95 columns/rows by this kit")
                return f"{ESC}[M{chr(32 + code)}{chr(32 + x)}{chr(32 + y)}"
            if encoding in ("urxvt", "1015"):
                return f"{ESC}[{32 + code};{x};{y}M"
            raise ValueError(f"unknown mouse encoding {encoding!r}")

        sent = one(button)
        if release and button < 64:
            sent += one(3)  # X10 has no per-button release: button code 3 means 'released'
        self.send_raw(sent)  # no SGR motion first: a terminal that speaks only the old encoding sends only the old one
        time.sleep(0.15)
        return sent

    def arrow(self, name: str, *, application: bool = True) -> None:
        """An arrow key as a terminal in application-cursor mode (``ESC O A``) sends it, or normal mode (``ESC [ A``)."""
        letter = {"up": "A", "down": "B", "right": "C", "left": "D"}[name]
        self.send_raw(f"{ESC}{'O' if application else '['}{letter}")
        time.sleep(0.12)

    def paste(self, text: str, *, bracketed: bool = True) -> None:
        """Paste ``text`` as a terminal does: wrapped in bracketed-paste markers, or raw (the characters as typed)."""
        self.send_raw(f"{ESC}[200~{text}{ESC}[201~" if bracketed else text)
        time.sleep(0.2)

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

    def modes_left_on(self) -> list[str]:
        """Terminal modes that differ from a fresh terminal's, from the emulator's state: alternate screen,
        application keys, bracketed paste, focus events, mouse tracking, hidden cursor. Meaningful after the program
        ended (``wait_exit``) and before ``close``."""
        st = self.state()
        defaults = {"cursor_visible": True, "wraparound": True}
        left = [k for k, v in st.modes.items() if v != defaults.get(k, False)]
        if st.mouse_mode != "none":
            left.append(f"mouse:{st.mouse_mode}")
        return sorted(left)

    def cells(self) -> list[list[Any]]:
        """Every cell of the screen as ``rows`` lists of ``cols`` tui-test Cells: char, fg, bg (``#rrggbb`` or
        ``default``), bold, dim, italic, inverse, underline, strike, link."""
        flat = self._run(self._t.cells(0, 0, self.cols, self.rows))
        return [flat[r * self.cols : (r + 1) * self.cols] for r in range(self.rows)]

    def colours_on_screen(self) -> set[str]:
        """The distinct non-default foreground and background colours currently drawn on non-blank cells."""
        found: set[str] = set()
        for row in self.cells():
            for cell in row:
                if cell.char.strip() or cell.bg != "default":
                    found.update(str(c) for c in (cell.fg, cell.bg) if str(c) != "default")
        return found

    def chromatic_on_screen(self) -> set[str]:
        """``colours_on_screen`` without the greys: colours with a hue, which a NO_COLOR run must not have."""
        return {c for c in self.colours_on_screen() if is_chromatic(c)}

    def _read_trace(self) -> str:
        text = []
        for cast in sorted(self._trace_dir.glob("**/session.cast")):
            with contextlib.suppress(OSError, ValueError):
                for line in cast.read_text(encoding="utf-8").splitlines()[1:]:
                    with contextlib.suppress(ValueError):
                        ev = json.loads(line)
                        if ev[1] == "o":
                            text.append(ev[2])
        return "".join(text)

    def end(self) -> str:
        """Close the session (the program is ended by PID) and return the raw stream."""
        self.close()
        return self._raw

    def raw_stream(self) -> str:
        """Every byte the program wrote to the terminal, from the first byte: startup queries, escape sequences, any
        DCS image data (an asciicast from the tui-test trace). tui-test writes it when the session closes, so this is
        empty while the program runs; call ``end()`` or leave the ``with`` block first."""
        return self._raw if self._closed else self._read_trace()

    def colour_depth(self) -> str:
        """``mono`` / ``16`` / ``256`` / ``truecolor``, from the SGR codes in the raw stream."""
        return sgr_depth(self.raw_stream())

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
