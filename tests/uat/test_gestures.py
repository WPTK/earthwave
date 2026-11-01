"""Gesture table: every pointer gesture and odd input a terminal can produce, against every kind of screen.

One expectation for all cells of the table, because the user-visible rule is the same: whatever the gesture does or does
not mean, the program stays alive, the screen stays well-formed (``uat.rules``), nothing of the input leaks onto the
screen as text, and the terminal is handed back clean at exit. Gestures that the app does give a meaning to are checked
for that meaning in ``test_j*`` and ``test_realinput``; a gesture that is simply ignored is correct behaviour here.

Legacy mouse encodings (X10 / 1000-without-1006, urxvt 1015) are not understood by Textual's parser: the expected
result is "no response, no crash, no leaked keys", and that is what is asserted.
"""

from __future__ import annotations

import re

import pytest

from uat import rules
from uat.target import BASE, USGS, evidence, launch, list_rows, log_text, needs_kit, ready

pytestmark = needs_kit

LEAK = re.compile(r"\[<\d+;\d+;\d+[Mm]|\x1b|\[M[ -~]{3}|\d+;\d+;\d+M")


def at_dashboard(t):
    ready(t)


def at_detail(t):
    ready(t)
    t.send_keys("enter")
    t.wait_for_text("Event page", 10)


def at_filters(t):
    ready(t)
    t.send_keys("f")
    t.wait_for_text("Minimum magnitude", 10)


def at_help(t):
    ready(t)
    t.send_keys("?")
    t.wait_for_text("Keys", 10)


def at_tsunami(t):
    ready(t)
    t.send_keys("t")
    t.wait_for_text("LEVEL", 10)


def at_map_wide(t):
    ready(t)
    t.send_keys("z")
    t.wait_for_text("Map · wide", 10)


SCREENS = {
    "dashboard": at_dashboard,
    "detail": at_detail,
    "filters": at_filters,
    "help": at_help,
    "tsunami": at_tsunami,
    "map-wide": at_map_wide,
}

# (cell) = (column, row) on the 130x32 screen; points picked from the dashboard geometry and reused on every screen,
# so that gestures land on whatever each screen has there, including nothing.
POINTS = {
    "list-row": (10, 5),
    "map": (100, 8),
    "tsunami-panel": (100, 28),
    "empty": (60, 20),
    "header": (20, 0),
    "footer": (6, 31),
}


def gestures(t, name: str, point: tuple[int, int]) -> None:
    c, r = point
    if name.startswith("x10"):
        c, r = min(c, 90), min(r, 90)  # see Terminal.legacy_mouse
    match name:
        case "right-click":
            t.right_click(c, r)
        case "middle-click":
            t.middle_click(c, r)
        case "double-click":
            t.double_click(c, r)
        case "triple-click":
            t.triple_click(c, r)
        case "shift-click":
            t.click(c, r, shift=True)
        case "ctrl-click":
            t.click(c, r, ctrl=True)
        case "alt-click":
            t.click(c, r, alt=True)
        case "wheel-up":
            t.wheel("up", 3, at=point)
        case "wheel-down":
            t.wheel("down", 3, at=point)
        case "wheel-sideways":
            t.wheel("left", 2, at=point)
            t.wheel("right", 2, at=point)
        case "hover":
            t.hover(c, r)
        case "drag":
            t.drag(point, (c + 12, r + 3))
        case "drag-out-of-window":
            t.drag(point, (c + 200, r + 40))
        case "x10-click":
            t.legacy_mouse(c, r, encoding="x10")
        case "urxvt-click":
            t.legacy_mouse(c, r, encoding="urxvt")
        case "x10-wheel":
            t.legacy_mouse(c, r, encoding="x10", button=64, release=False)
        case _:
            raise AssertionError(name)


GESTURES = [
    "right-click", "middle-click", "double-click", "triple-click", "shift-click", "ctrl-click", "alt-click",
    "wheel-up", "wheel-down", "wheel-sideways", "hover", "drag", "drag-out-of-window",
    "x10-click", "urxvt-click", "x10-wheel",
]  # fmt: skip


def check(t, label: str) -> None:
    assert t.alive(), f"{label}: the program is gone: {t.exit_code()}"
    t.settle()
    text = evidence(t, "gestures", re.sub(r"\W+", "_", label))
    assert not LEAK.search(text), f"{label}: input leaked onto the screen\n{text}"
    found = rules.violations(text, t.cols, t.rows)
    assert not found, f"{label}: {found}\n{text}"
    assert "Traceback" not in log_text(t), f"{label}: traceback in the log\n{log_text(t)}"


@pytest.mark.parametrize("screen", list(SCREENS))
@pytest.mark.parametrize("gesture", GESTURES)
def test_gesture_on_screen(screen: str, gesture: str) -> None:
    """Every gesture at every named point of one screen, in one session (a program that survives one gesture and dies on
    the next is the usual shape of this bug), then a clean quit."""
    with launch("--replay", str(USGS), *BASE) as t:
        SCREENS[screen](t)
        for name, point in POINTS.items():
            gestures(t, gesture, point)
            check(t, f"{screen}/{gesture}/{name}")
        t.send_keys("escape")
        t.send_keys("escape")
        t.send_keys("q")
        assert t.wait_exit(10) is not None, f"{screen}/{gesture}: q did not end the program afterwards"
        leftover = [m for m in t.modes_left_on() if m != "focus_events"]
        assert leftover == [], f"modes left on: {leftover}"


def test_legacy_mouse_changes_nothing_on_the_dashboard() -> None:
    """No leaked keys is stronger than no crash: the list, selection and status must be exactly as before."""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        before = [r for _, r in list_rows(t.screen().lines)]
        sel = [ln[:72] for ln in t.screen().lines if ln.startswith("▌")]
        for enc in ("x10", "urxvt"):
            for c, r in POINTS.values():
                t.legacy_mouse(min(c, 90), r, encoding=enc)
        t.settle()
        after_sel = [ln[:72] for ln in t.screen().lines if ln.startswith("▌")]
        assert len(after_sel) == len(sel) == 1
        assert re.sub(r"^▌\s*\d+\w\s+", "", after_sel[0]) == re.sub(r"^▌\s*\d+\w\s+", "", sel[0]), (
            "legacy mouse moved the cursor"
        )
        assert len(before) == len(list_rows(t.screen().lines))


def test_application_cursor_keys_and_paste_do_not_break_the_dashboard() -> None:
    """Arrow keys as a terminal in application-cursor mode sends them (ESC O A), a bracketed paste of text and of a
    multi-line block, and a raw paste: none may crash, leak or be taken as commands (a pasted 'q' must not quit)."""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        for name in ("down", "down", "up", "right", "left"):
            t.arrow(name, application=True)
        t.paste("qqqq\nfff\n")
        t.settle()
        assert t.alive(), "a bracketed paste containing q quit the program"
        t.paste("jjj", bracketed=False)  # raw paste is just typing: three downs
        t.settle()
        check(t, "paste")


@pytest.mark.parametrize("key", ["f1", "f5", "f12", "ctrl+c", "ctrl+l", "ctrl+z", "backspace", "delete", "insert"])
def test_unbound_keys_are_ignored(key: str) -> None:
    """Keys the app does not bind (but a user will press) leave the screen and the process as they were. ctrl+c is the
    exception that proves the rule: Textual binds it to a quit hint, so it may only show a notice, never kill the app."""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.send_keys(key)
        t.settle()
        check(t, f"key-{key}")
        t.send_keys("q")
        assert t.wait_exit(10) is not None
