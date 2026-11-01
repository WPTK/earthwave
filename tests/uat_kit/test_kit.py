"""Self-test of the kit with a tiny program that has nothing to do with any project: it echoes the bytes it receives."""

from __future__ import annotations

import sys

import pytest

from uat_kit import Terminal, unavailable_reason

ECHO = r"""
import sys, os
print("[?1000h[?1003h[?1006h" + "ready", flush=True)  # what a TUI sends to turn mouse reports on
if sys.platform == "win32":
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetConsoleMode(k32.GetStdHandle(-10), 0x200)  # ENABLE_VIRTUAL_TERMINAL_INPUT, no line mode, no echo
    read = lambda: os.read(0, 1).decode()
else:
    import termios, tty
    tty.setraw(0)
    read = lambda: os.read(0, 1).decode()
buf = ""
while True:
    c = read()
    if c == "q":
        break
    buf += c
    print("GOT " + repr(buf[-14:]), flush=True)
sys.exit(3)
"""

pytestmark = pytest.mark.skipif(unavailable_reason() is not None, reason=str(unavailable_reason()))


def test_keys_mouse_resize_exit_and_kill() -> None:
    with Terminal([sys.executable, "-c", ECHO], 80, 24) as term:
        term.wait_for_text("ready", 20)
        term.send_keys("a", "b")
        term.wait_for_text("GOT 'ab'", 10)
        term.click(4, 2)  # motion, press, release as SGR reports
        screen = term.wait_for(lambda s: "[<0;5;3m" in s.text.replace("\x1b", ""), 10, "the release report")
        assert "[<35;5;3M" in screen.text.replace("\x1b", "")  # pointer motion arrives before the press
        term.resize(60, 16)
        assert len(term.screen().lines) == 16
        pid = term.pid
        assert pid is not None
        term.send_keys("q")
        assert term.wait_exit(10) == 3
    import psutil

    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_wait_for_fails_with_a_screen_dump_and_kills_the_child() -> None:
    with Terminal([sys.executable, "-c", ECHO], 80, 24) as term:
        term.wait_for_text("ready", 20)
        with pytest.raises(AssertionError, match=r"(?s)timed out after 0.5 s.*ready"):
            term.wait_for_text("never", 0.5)
