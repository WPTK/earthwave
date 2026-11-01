"""Self-test of the gesture helpers with a tiny program that has nothing to do with any project: it logs, as text, the
exact bytes each input call puts on its stdin. What a gesture sends is the contract; what an app does with it is
tested elsewhere."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

from uat_kit import Terminal, unavailable_reason

LOGGER = r"""
import sys, os
log = open(sys.argv[1], "a", encoding="utf-8", buffering=1)
if sys.platform == "win32":
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetConsoleMode(k32.GetStdHandle(-10), 0x290)  # VIRTUAL_TERMINAL_INPUT | MOUSE_INPUT | EXTENDED_FLAGS; no line mode, no echo
else:
    import tty
    tty.setraw(0)
print("\x1b[?1000h\x1b[?1003h\x1b[?1006hready", flush=True)  # only once input is set up: tests send at once
while True:
    if sys.platform == "win32":
        buf = ctypes.create_string_buffer(4096)
        got = ctypes.c_ulong(0)
        k32.ReadFile(k32.GetStdHandle(-10), buf, 4096, ctypes.byref(got), None)  # raw bytes: no ^Z handling, no text mode
        data = buf.raw[: got.value].decode("utf-8", "replace")
    else:
        data = os.read(0, 4096).decode("utf-8", "replace")
    if data == "q":
        break
    log.write(data.replace("\x1b", "<ESC>"))
    log.flush()
sys.exit(0)
"""

pytestmark = pytest.mark.skipif(unavailable_reason() is not None, reason=str(unavailable_reason()))


def run(tmp_path: Path):
    log = tmp_path / "bytes.log"
    term = Terminal([sys.executable, "-c", LOGGER, str(log)], 120, 30)
    term.wait_for_text("ready", 20)
    return term, log


def read_log(log: Path) -> str:
    time.sleep(0.4)
    return log.read_text(encoding="utf-8") if log.exists() else ""


def sgr(log_text: str) -> list[tuple[int, int, int, str]]:
    """(code, col, row, M or m) for every SGR report, however the chunks fell."""
    return [(int(a), int(b), int(c), d) for a, b, c, d in re.findall(r"<ESC>\[<(\d+);(\d+);(\d+)([Mm])", log_text)]


def test_buttons_modifiers_and_chains(tmp_path: Path) -> None:
    term, log = run(tmp_path)
    with term:
        term.click(10, 5)
        term.right_click(11, 6)
        term.middle_click(12, 7)
        term.click(13, 8, shift=True)
        term.click(14, 9, ctrl=True)
        term.click(15, 10, alt=True)
        term.double_click(16, 11)
        term.triple_click(17, 12)
        reports = sgr(read_log(log))
    presses = [(c, x, y) for c, x, y, s in reports if s == "M" and c < 32]
    assert (0, 11, 6) in presses  # left
    assert (2, 12, 7) in presses  # right
    assert (1, 13, 8) in presses  # middle
    assert (4, 14, 9) in presses  # shift + left
    assert (16, 15, 10) in presses  # ctrl + left
    assert (8, 16, 11) in presses  # alt + left
    assert presses.count((0, 17, 12)) == 2  # double click: two press/release pairs
    assert presses.count((0, 18, 13)) == 3  # triple click
    releases = [(c, x, y) for c, x, y, s in reports if s == "m"]
    assert (0, 11, 6) in releases and (2, 12, 7) in releases
    motion = [(x, y) for c, x, y, s in reports if c == 35]  # motion with no button, ahead of the press
    assert (11, 6) in motion, "pointer motion must arrive before the press"


def test_wheel_hover_drag(tmp_path: Path) -> None:
    term, log = run(tmp_path)
    with term:
        term.wheel("up", 2, at=(30, 8))
        term.wheel("down", 1, at=(31, 9))
        term.hover(40, 3)
        term.drag((20, 4), (26, 4))
        term.drag((50, 5), (60, 12))  # release elsewhere
        reports = sgr(read_log(log))
    ups = [r for r in reports if r[0] == 64]
    downs = [r for r in reports if r[0] == 65]
    assert len(ups) == 2 and ups[0][1:3] == (31, 9)
    assert len(downs) == 1 and downs[0][1:3] == (32, 10)
    assert (35, 41, 4, "M") in reports  # hover ends on the cell, no button
    held = [r for r in reports if r[0] == 32]  # motion with the left button held
    assert len(held) >= 8, f"a drag must report motion with the button held: {reports}"
    assert (0, 27, 5, "m") in reports  # released on the end cell
    assert (0, 61, 13, "m") in reports  # released away from where it started


def test_legacy_encodings_are_the_old_bytes(tmp_path: Path) -> None:
    term, log = run(tmp_path)
    with term:
        sent_x10 = term.legacy_mouse(10, 5)
        sent_urxvt = term.legacy_mouse(10, 5, encoding="urxvt")
        term.legacy_mouse(9, 4, button=64)
        text = read_log(log)
        with pytest.raises(ValueError, match="222"):
            term.legacy_mouse(250, 3)
    assert sent_x10 == "\x1b[M" + chr(32) + chr(32 + 11) + chr(32 + 6) + "\x1b[M" + chr(32 + 3) + chr(32 + 11) + chr(32 + 6)
    assert sent_urxvt == "\x1b[32;11;6M\x1b[35;11;6M"
    assert "<ESC>[M" in text and "<ESC>[32;11;6M" in text


def test_application_cursor_arrows_paste_and_function_keys(tmp_path: Path) -> None:
    term, log = run(tmp_path)
    with term:
        term.arrow("up")
        term.arrow("left", application=False)
        term.paste("hello\nworld")
        term.paste("raw", bracketed=False)
        term.send_keys("f1", "f5", "f12", "ctrl+z", "backspace", "delete")
        text = read_log(log)
    assert "<ESC>OA" in text and "<ESC>[D" in text
    assert "<ESC>[200~hello" in text and "world<ESC>[201~" in text
    assert "raw" in text
    for seq in ("<ESC>OP", "<ESC>[15~", "<ESC>[24~", "\x1a", "\x7f", "<ESC>[3~"):
        assert seq in text, seq
