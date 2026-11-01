"""Real-input UAT on the built release exe: ConPTY (tests/uat_kit, tui-test), real key bytes and SGR mouse press/release.

Why: pilot runs the source in Textual's headless driver, so the real input parser, the Windows driver and the
released binary are never exercised. Here the exe runs under a pseudo-console, bytes go in exactly as a terminal
sends them, and the screen is rebuilt by the terminal emulator from what the exe wrote. Assertions are on screen text.

How it runs:
  - Needs `pip install -e ".[dev]"` (tui-test is a dev dependency), Windows,
    and the downloaded release asset: `gh release download vX.Y.Z -p "earthwave-*-windows-x86_64.exe" -p SHA256SUMS`
    into an empty directory, then `$env:EARTHWAVE_EXE = "<path to the exe>"; pytest tests/uat/test_realinput.py`.
  - Without EARTHWAVE_EXE (or off Windows) every test skips with that reason, so CI stays usable.
  - If SHA256SUMS sits next to the exe the exe is verified against it first and its hash is printed.
  - ConPTY answers no sixel query, so the map here is the braille path. Sixel stays a separate real-WT check.
  - `--replay` is used for data: it serves a recording, never touches the network, and keeps the run out of the real
    history database. (platformdirs on Windows ignores the environment, so the exe cannot be pointed at an empty
    config or data directory; `--no-config` and `--replay` are what keep a run away from the user's files.)
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

from uat import pty_driver as pty

EXE = os.environ.get("EARTHWAVE_EXE")
REPLAY = Path(__file__).parents[1] / "fixtures" / "replay"
USGS = REPLAY / "usgs_short.jsonl"
KAM = REPLAY / "kamchatka_watch_to_warning.jsonl"

pytestmark = pytest.mark.skipif(
    not (EXE and Path(EXE).is_file() and sys.platform == "win32" and pty.AVAILABLE),
    reason="real-input run needs Windows, tui-test, and EARTHWAVE_EXE pointing at the release exe",
)

FOOTER = [("Filters", "f"), ("Sort", "s"), ("Search", "/"), ("Map", "m"), ("Tsunami", "t"), ("Gauges", "o"),
          ("History", "h"), ("Ack all", "a"), ("Refresh", "r"), ("Help", "?"), ("Quit", "q")]  # fmt: skip


def test_exe_matches_published_checksum():
    sums = Path(EXE).with_name("SHA256SUMS")
    digest = hashlib.sha256(Path(EXE).read_bytes()).hexdigest()
    print(f"\nexe {Path(EXE).name} sha256 {digest}")
    if not sums.is_file():
        pytest.skip("no SHA256SUMS next to the exe")
    listed = {line.split()[1].lstrip("*"): line.split()[0] for line in sums.read_text().splitlines() if line.strip()}
    assert listed.get(Path(EXE).name) == digest


@contextmanager
def launch(*args: str, size=(130, 32), wt: bool = True, isolate: bool = True, **env) -> Iterator[pty.Session]:
    """The exe in a pseudo-console, in an empty directory, with the process always ended by PID afterwards."""
    with tempfile.TemporaryDirectory(prefix="ew-uat-") as tmp:
        root = Path(tmp)
        (root / "cfg").mkdir()
        (root / "data").mkdir()
        extra = {"WT_SESSION": "uat"} if wt else {}  # earthwave picks unicode glyphs only inside Windows Terminal
        session = pty.Session(
            [EXE, "--no-config", "--log", str(root / "log.txt"), *args],
            cwd=str(root),
            env=pty.clean_env(root / "cfg", root / "data", **extra, **env),
            size=size,
            isolate=isolate,
        )
        try:
            yield session
        finally:
            session.close()


def leftover_modes(s: pty.Session) -> list[str]:
    """Terminal modes the app left on. Focus events (1004) are Textual's/ConPTY's own: a bare Textual app that does
    nothing shows the same, so it is not earthwave's to reset."""
    return [m for m in s.modes_left_on() if m != "focus_events"]


def footer_row(s: pty.Session) -> str:
    return s.lines()[-1]


def footer_cell(s: pty.Session, label: str) -> tuple[int, int]:
    row = len(s.lines()) - 1
    col = footer_row(s).find(label)
    assert col >= 0, f"footer has no {label!r}: {footer_row(s)!r}"
    return col + 1, row


def selected(s: pty.Session) -> str | None:
    """The list row under the cursor (the row whose first column is the cursor bar), without trailing blanks."""
    for ln in s.lines():
        if ln.startswith("▌") and re.match(r"^▌\s*\d+[mhd]\s+\d\.\d", ln):
            return ln[:72].rstrip()
    return None


def ident(row: str | None) -> str | None:
    """A list row without its age (which ticks) and cursor bar: magnitude, depth and place."""
    return None if row is None else re.sub(r"^\S?\s*\d+[mhd]\s+", "", row)


def list_rows(s: pty.Session, width: int = 72) -> list[tuple[int, str]]:
    pattern = r"^\S?\s*\d+[mhd]\s+\d\.\d"
    return [(i, ln[:width].rstrip()) for i, ln in enumerate(s.lines()) if re.match(pattern, ln[:width])]


def ready(s: pty.Session) -> None:
    assert s.wait_for(re.compile(r"[1-9]\d* shown of"), 40), "dashboard never showed quakes:\n" + s.text()
    assert s.wait_for(" Quit", 5)
    assert s._wait(lambda t: len(list_rows(s)) >= 1, 10), "no list rows drawn:\n" + s.text()
    s.term.settle(0.5)  # the first frames draw the status bar before the list is complete


def test_first_run_dashboard_and_clean_exit():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        assert "Traceback" not in s.text()
        count = re.search(r"(\d+) shown of (\d+)", s.text())
        assert count and int(count[1]) == len(list_rows(s)) <= int(count[2]), "status count disagrees with the list"
        assert footer_row(s).split()[:2] == ["f", "Filters"]
        s.press("q")
        assert s.wait_exit(10), "q did not end the process"
        assert s.descendants() == []
        assert "Traceback" not in s.raw and "Error" not in s.raw
        assert leftover_modes(s) == [], f"modes left on after exit: {leftover_modes(s)}"


@pytest.mark.parametrize("label,key", FOOTER, ids=[k for _, k in FOOTER])
def test_every_footer_key_by_bytes(label, key):
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        before = s.text()
        s.press(key, pause=0.8)
        after = s.text()
        if key == "f":
            assert "Minimum magnitude" in after and "Apply" in after
            s.press("escape")
            assert s.wait_gone("Minimum magnitude")
        elif key == "s":
            assert "sort:mag" in after and "sort:time" in before
        elif key == "/":
            assert "search place" in after or "Enter apply, Esc cancel" in after
            s.press("escape")
            assert s.wait_gone("Enter apply, Esc cancel")
        elif key == "m":
            assert "Map ·" in before and "Map ·" not in after
            s.press("m", pause=0.8)
            assert "Map ·" in s.text()
        elif key == "t":
            assert "LEVEL" in after and "Back" in footer_row(s)
            s.press("escape")
            assert s.wait_for("shown of")
        elif key == "o":
            assert "AGE  MAG" not in after and "Back" in footer_row(s)
            s.press("escape")
            assert s.wait_for("shown of")
        elif key == "h":
            assert "history" in after.lower() and "AGE  MAG" in after  # replay has no history: a toast says so
        elif key == "a":
            pass  # ack-all only recolours rows that are unseen; test_inputs checks the tracker. Here: it must not crash.
        elif key == "r":
            assert "Refreshing" in after
        elif key == "?":
            assert "Keys" in after and "full-screen map" in after
            s.press("?")
            assert s.wait_gone("Esc or ? to close")
        elif key == "q":
            assert s.wait_exit(10)
            return
        assert s.alive()
        assert "Traceback" not in s.raw


def test_navigation_keys_move_the_selection():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        seen = []
        for key in ("j", "down", "j", "k", "up", "end", "home", "pagedown", "pageup"):
            s.press(key)
            seen.append((key, selected(s)))
        names = [ident(m) for _, m in seen]
        assert all(names), seen
        assert names[0] != names[1] != names[2], seen  # j, down, j each move down one row
        assert names[2] != names[3] != names[4], seen  # k, up each move up one row
        assert names[4] == names[0], seen  # j, down, j, k, up nets to one row down
        s.press("end")
        last = selected(s)
        s.press("home")
        assert selected(s) != last
        s.press("enter", pause=1.0)
        assert "Magnitude" in s.text() and "Event page" in s.text(), s.text()
        s.press("escape")
        assert s.wait_for("shown of")
        assert s.alive()


def test_mouse_click_on_a_list_row_selects_it_and_it_sticks_through_refresh():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        rows = list_rows(s)
        assert len(rows) == 7, s.text()
        for target in (3, 5, 1):
            row_y, text = rows[target]
            s.click(10, row_y)
            picked = selected(s)
            assert picked and ident(picked) == ident(text), f"clicked {text!r}, cursor is on {picked!r}"
            # still the same after the next refresh ticks (the app redraws every half second)
            import time

            time.sleep(2.5)
            now = selected(s)
            assert ident(now) == ident(picked), f"selection lost: {picked!r} -> {now!r}"
        assert s.alive() and "Traceback" not in s.raw


def test_mouse_click_on_the_map_focuses_it_so_the_arrows_pan():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        s.press("z", pause=0.6)
        assert "Map · wide" in s.text()
        pos = s.find("Map · wide")
        s.click(pos[0] + 10, pos[1] + 6)  # inside the map box
        before = s.text()
        s.press("right", pause=0.6)
        s.press("right", pause=0.6)
        after = s.text()
        assert before != after, "arrows after a map click did nothing: the click did not focus the map"
        sel = selected(s)
        s.press("j")
        assert selected(s) == sel, "j moved the list although the map has focus"


@pytest.mark.parametrize("label,key", [f for f in FOOTER if f[1] not in "oh"], ids=lambda v: v)
def test_every_footer_label_clicked_with_the_mouse(label, key):
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        before = s.text()
        s.click(*footer_cell(s, label), pause=0.8)
        after = s.text()
        if key != "a":  # ack-all changes only colours when something is unseen; the in-process test checks the tracker
            assert after != before, f"clicking {label!r} in the footer changed nothing"
        expected = {"f": "Minimum magnitude", "s": "sort:mag", "/": "Enter apply", "t": "LEVEL", "?": "Keys",
                    "r": "Refreshing"}  # fmt: skip
        if key in expected:
            assert expected[key] in after
        if key == "m":
            assert "Map ·" not in after
        if key == "q":
            assert s.wait_exit(10)
        else:
            assert s.alive()


def test_filters_buttons_clicked_with_the_mouse():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        s.press("f", pause=0.8)
        x, y = s.find("Cancel")
        s.click(x + 2, y)
        assert s.wait_gone("Minimum magnitude"), "Cancel button click did not close the filters"
        s.press("f", pause=0.8)
        x, y = s.find("Apply")
        s.click(x + 2, y)
        assert s.wait_gone("Minimum magnitude"), "Apply button click did not close the filters"
        assert s.alive()


def test_tsunami_screen_rows_clicked_with_the_mouse():
    with launch("--replay", str(KAM), "--speed", "20", "--hours", "720", "--min-mag", "2.5") as s:
        assert s.wait_for("TSUNAMI", 40) or s.wait_for("Tsunami", 5), s.text()
        s.press("t", pause=1.0)
        assert s.wait_for("LEVEL", 5)
        rows = [(i, ln) for i, ln in enumerate(s.lines()) if re.match(r"^\s*(WARNING|WATCH|ADVISORY|INFORMATION|CANCEL)", ln)]
        assert rows, "the tsunami table has no rows:\n" + s.text()
        s.text()
        s.click(5, rows[-1][0])
        assert s.alive() and "Traceback" not in s.raw
        assert "Traceback" not in s.text()
        assert s.text().count("WATCH") >= 1


def test_resize_to_too_small_shows_the_message_and_recovers():
    with launch("--replay", str(USGS), "--hours", "720", "--min-mag", "2.5") as s:
        ready(s)
        s.resize(90, 25)
        assert s.wait_for("Terminal too small", 8), s.text()
        assert "90x25" in s.text()
        s.resize(130, 32)
        assert s.wait_for("shown of", 8), s.text()
        s.resize(100, 30)
        assert s.wait_for("shown of", 8) and "Map ·" not in s.text()
        s.press("q")
        assert s.wait_exit(10)


def test_offline_flag_says_so_and_nothing_contradicts():
    with launch("--offline") as s:
        assert s.wait_for("offline", 20), s.text()
        # status bar text depends on the tester's saved history (#230), so only the stable part is asserted
        assert s.wait_for(re.compile(r"offline", re.I), 15), s.text()
        text = s.text()
        assert "feeds ok" not in text.lower() and "live" not in footer_row(s).lower()
        s.press("q")
        assert s.wait_exit(10)


@pytest.mark.skipif(os.environ.get("EARTHWAVE_UAT_NETWORK") != "1", reason="set EARTHWAVE_UAT_NETWORK=1 for the live smoke")
def test_first_run_smoke_on_the_real_network():
    """The one test that uses the real network: a clean launch reaches live quakes with no traceback and quits."""
    with launch(isolate=False) as s:  # isolate=False: no dead proxy, the one run that uses the network
        ready(s)
        assert s.wait_gone("Waiting for USGS", 60), s.text()
        text = s.text()
        assert "Traceback" not in text
        assert "failed:" not in text.splitlines()[-2], text.splitlines()[-2]
        s.press("q")
        assert s.wait_exit(10)
        assert leftover_modes(s) == []
