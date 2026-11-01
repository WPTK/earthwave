"""Thin adapter from the earthwave UAT tests to the vendored `uat_kit` (WPTK/wxbrief e01891b, on tui-test 0.1.1).

The kit (tests/uat_kit/) does the pseudo-terminal work: ConPTY on Windows, a pty elsewhere, real SGR mouse reports with
pointer motion before each click, a real window-size change on resize, exit code from the terminal state, kill by PID.
This module keeps the small API the earthwave tests were written against (`press`, `click`, `text`, `wait_for` ...)
so the tests read the same, and adds the two things the kit does not give: every screen seen (for the traceback check)
and the private modes left on after exit.

Cells are 0-based. Set PYTHONUTF8=1 on Windows when printing screen text.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

import psutil
from uat_kit import Terminal, unavailable_reason

AVAILABLE = unavailable_reason() is None


class Session:
    def __init__(
        self, argv: list[str], *, cwd: str, env: dict[str, str], size: tuple[int, int] = (120, 30), isolate: bool = True
    ):
        self.term = Terminal(argv, size[0], size[1], env=env, cwd=Path(cwd), isolate=isolate)
        self.cols, self.rows = size
        self.pid = self.term.pid
        self._seen: list[str] = []

    # -- input ------------------------------------------------------------------------------------------------------

    def press(self, *keys: str, pause: float = 0.3) -> None:
        self.term.send_keys(*keys)
        time.sleep(pause)

    def click(self, x: int, y: int, button: int = 0, pause: float = 0.5) -> None:
        self.term.click(x, y)  # motion, press, release (SGR) at 0-based cells
        time.sleep(pause)

    def resize(self, cols: int, rows: int) -> None:
        self.term.resize(cols, rows)
        self.cols, self.rows = cols, rows

    # -- output -----------------------------------------------------------------------------------------------------

    def text(self) -> str:
        text = self.term.screen_text()
        if not self._seen or self._seen[-1] != text:
            self._seen.append(text)
        return text

    @property
    def raw(self) -> str:
        """Every distinct screen this session looked at (the kit does not expose the raw byte stream)."""
        self.text()
        return "\n".join(self._seen)

    def lines(self) -> list[str]:
        return list(self.term.screen().lines)

    def find(self, needle: str) -> tuple[int, int] | None:
        return self.term.screen().find(needle)

    def _wait(self, test, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if test(self.text()):
                return True
            time.sleep(0.1)
        return False

    def wait_for(self, needle: str | re.Pattern | str, timeout: float = 20.0) -> bool:
        if isinstance(needle, str):
            return self._wait(lambda t: needle in t, timeout)
        return self._wait(lambda t: needle.search(t) is not None, timeout)

    def wait_gone(self, needle: str, timeout: float = 10.0) -> bool:
        return self._wait(lambda t: needle not in t, timeout)

    # -- process ----------------------------------------------------------------------------------------------------

    def alive(self) -> bool:
        return self.term.alive()

    def wait_exit(self, timeout: float = 10.0) -> bool:
        return self.term.wait_exit(timeout) is not None

    def exit_code(self) -> int | None:
        return self.term.exit_code()

    def modes_left_on(self) -> list[str]:
        """Terminal modes that differ from a fresh terminal's, as the kit's state reports them after the program ended:
        alternate screen, application keys, bracketed paste, focus events, mouse tracking, hidden cursor."""
        st = self.term.state()
        defaults = {"cursor_visible": True, "wraparound": True}
        left = [k for k, v in st.modes.items() if v != defaults.get(k, False)]
        if st.mouse_mode != "none":
            left.append(f"mouse:{st.mouse_mode}")
        return sorted(left)

    def descendants(self) -> list[int]:
        try:
            return [p.pid for p in psutil.Process(self.pid).children(recursive=True)] if self.pid else []
        except psutil.NoSuchProcess:
            return []

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.term.close()


def clean_env(config_dir: Path, data_dir: Path, **extra: str) -> dict[str, str]:
    """Extra variables for the program. The kit already isolates XDG/APPDATA and kills the network with a dead proxy;
    NO_COLOR is never set unless a test asks for it."""
    return dict(extra.items())
