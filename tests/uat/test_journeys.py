"""User journeys through the real program in a real pseudo-terminal (tests/uat_kit), with real key and mouse bytes.

The target is the release exe when EARTHWAVE_EXE is set, the source tree otherwise (see ``uat.target``). Data comes from
recordings served by ``--replay``; faults are crafted recordings (``uat.replays``), so the app's real network stack runs
unmodified. Every journey ends with the generic screen rules (``uat.rules``): a person looking at the screen would see
nothing broken. Frames are saved to ``$UAT_OUT`` when set so a person can read them.

Journey ids (J1..) are the ones in ``tests/uat/traceability.toml``.
"""

from __future__ import annotations

import re
import stat
import time
from pathlib import Path

import pytest

from uat import rules
from uat.known import known
from uat.replays import Rec, all_quiet, warning_with_expiry
from uat.target import (
    BASE,
    IS_WINDOWS,
    KAM,
    USGS,
    evidence,
    footer_cell,
    ident,
    launch,
    list_rows,
    log_text,
    needs_kit,
    needs_posix,
    ready,
    selected_row,
    status_line,
)

pytestmark = needs_kit

NO_MODES = ("focus_events",)  # Textual's own; a bare Textual app leaves it too


def clean(term, name: str, test: str) -> str:
    """Save the frame, check the generic rules and return the text."""
    text = evidence(term, test, name)
    found = rules.violations(text, term.cols, term.rows)
    assert not found, f"{name}: {found}\n{text}"
    return text


def leftovers(term) -> list[str]:
    return [m for m in term.modes_left_on() if m not in NO_MODES]


def quit_cleanly(term) -> None:
    term.send_keys("q")
    assert term.wait_exit(10) is not None, "q did not end the process"
    assert "Traceback" not in term.raw_stream()
    assert leftovers(term) == [], f"terminal modes left on after exit: {leftovers(term)}"


def test_j1_first_run_dashboard_and_clean_exit() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        text = clean(t, "dashboard", "j1")
        count = re.search(r"(\d+) shown of (\d+)", text)
        assert count and int(count[1]) == len(list_rows(t.screen().lines))
        quit_cleanly(t)


def test_j5_navigate_open_detail_and_back() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        first = ident(selected_row(t.screen().lines)[1])
        t.send_keys("j", "j", "k")
        assert ident(selected_row(t.screen().lines)[1]) != first
        t.send_keys("end")
        last = ident(selected_row(t.screen().lines)[1])
        t.send_keys("home")
        assert ident(selected_row(t.screen().lines)[1]) == first and last != first
        t.send_keys("enter")
        t.wait_for_text("Event page", 10)
        clean(t, "detail", "j5")
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        assert ident(selected_row(t.screen().lines)[1]) == first, "returning from the detail lost the selection"
        quit_cleanly(t)


def test_j5b_clicking_a_row_selects_that_row_and_the_wheel_scrolls() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        rows = list_rows(t.screen().lines)
        for target in (3, 5, 1):
            row_y, text = rows[target]
            t.click(10, row_y)
            picked = selected_row(t.screen().lines)
            assert picked and ident(picked[1]) == ident(text), f"clicked {text!r}, cursor is on {picked!r}"
        t.wheel("down", 3, at=(10, rows[2][0]))
        t.wheel("up", 3, at=(10, rows[2][0]))
        clean(t, "after-wheel", "j5b")
        assert t.alive()
        quit_cleanly(t)


def test_j6_filters_apply_and_cancel() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        before = status_line(t.screen_text())
        t.send_keys("f")
        t.wait_for_text("Minimum magnitude", 10)
        clean(t, "filters", "j6")
        t.send_keys("escape")
        t.wait_for(lambda s: "Minimum magnitude" not in s.text, 10, "filter dialog closed")
        assert status_line(t.screen_text()).split("│")[:2] == before.split("│")[:2], (
            "cancel changed the list or the filters"
        )
        t.send_keys("f")
        t.wait_for_text("Minimum magnitude", 10)
        t.send_keys("backspace", "backspace", "backspace", "backspace", "6", "enter")
        t.wait_for(lambda s: "M6+" in s.text, 15, "M6+ in the status bar")
        t.settle()
        text = clean(t, "after-apply", "j6")
        rows = list_rows(t.screen().lines)
        assert rows and all(float(re.search(r"\s(\d\.\d)\s", r)[1]) >= 6.0 for _, r in rows), text
        quit_cleanly(t)


def test_j7_search_narrows_and_clears() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        all_rows = len(list_rows(t.screen().lines))
        t.send_keys("/")
        t.wait_for_text("search place", 10)
        t.send_keys("a", "enter")
        t.settle()
        some = len(list_rows(t.screen().lines))
        assert 0 < some <= all_rows
        t.send_keys("/")
        t.send_keys("z", "z", "z", "z", "q", "enter")  # nothing matches; the q is text, not quit
        t.settle()
        text = clean(t, "no-match", "j7")
        assert t.alive() and ("No quakes match" in text or len(list_rows(t.screen().lines)) == 0)
        t.send_keys("/")
        t.send_keys("escape")
        quit_cleanly(t)


def test_j8_sort_cycles() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        assert "sort:time" in t.screen_text()
        t.send_keys("s")
        t.wait_for(lambda s: "sort:mag" in s.text, 10, "sort:mag")
        first = [ident(r) for _, r in list_rows(t.screen().lines)]
        mags = [float(re.search(r"\s(\d\.\d)\s", r)[1]) for _, r in list_rows(t.screen().lines)]
        assert mags == sorted(mags, reverse=True), first
        clean(t, "sorted", "j8")
        quit_cleanly(t)


def test_j9_map_hide_show_and_zoom() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        assert "Map ·" in t.screen_text()
        t.send_keys("m")
        t.wait_for(lambda s: "Map ·" not in s.text, 10, "map hidden")
        t.send_keys("m")
        t.wait_for_text("Map ·", 10)
        t.send_keys("z")
        t.wait_for_text("Map · wide", 10)
        clean(t, "map-wide", "j9")
        t.send_keys("escape")
        quit_cleanly(t)


def test_j10_help_lists_the_keys_and_closes() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.send_keys("?")
        t.wait_for_text("Keys", 10)
        text = clean(t, "help", "j10")
        for key_text in ("search place", "show or hide map"):
            assert key_text in text
        t.send_keys("?")
        t.wait_for(lambda s: "Esc or ? to close" not in s.text, 10, "help closed")
        quit_cleanly(t)


@pytest.mark.skipif(
    not IS_WINDOWS,
    reason="resize on a POSIX pty drew a cropped screen in CI (run 41357766307); not yet confirmed with a second driver, so not called a finding",
)
def test_j12_resize_small_and_back() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.resize(50, 12)
        t.wait_for(lambda s: "too small" in s.text.lower(), 10, "too-small message")
        clean(t, "small", "j12")
        t.resize(130, 32)
        t.wait_for(lambda s: "shown of" in s.text, 10, "dashboard back")
        t.settle()
        clean(t, "restored", "j12")
        assert t.alive()
        quit_cleanly(t)


def test_j13_tsunami_watch_to_warning_in_the_ui() -> None:
    with launch("--replay", str(KAM), "--speed", "20") as t:
        t.wait_for(lambda s: "TSUNAMI" in s.text, 60, "a tsunami banner")
        t.settle()
        clean(t, "banner", "j13")
        t.send_keys("t")
        t.wait_for_text("LEVEL", 10)
        text = clean(t, "tsunami-screen", "j13")
        assert re.search(r"WATCH|WARNING", text)
        t.send_keys("a", "escape")
        t.wait_for_text("shown of", 10)
        assert t.alive()
        quit_cleanly(t)


def test_j16_a_quiet_day_says_so(tmp_path: Path) -> None:
    path = all_quiet().save(tmp_path / "quiet.jsonl")
    with launch("--replay", str(path), "--speed", "60") as t:  # the startup backfill needs a few poll intervals
        t.wait_for(lambda s: "No quakes match" in s.text, 60, "the empty-list text once the steady feed answered")
        t.settle(1.0)
        text = clean(t, "quiet", "j16")
        assert "No tsunami alerts in force" in text and "Waiting for USGS" not in text
        assert "TSUNAMI" not in status_line(text)
        quit_cleanly(t)


def test_j17_a_feed_that_errors_is_shown_and_does_not_crash(tmp_path: Path) -> None:
    rec = Rec.kamchatka().status("api.weather.gov/alerts/active", 500, "internal error")
    rec.body("jma.go.jp", lambda _b: "{not json")
    path = rec.save(tmp_path / "faults.jsonl")
    with launch("--replay", str(path)) as t:
        t.wait_for(lambda s: "shown of" in s.text, 45, "dashboard")
        t.settle(2.0)
        text = clean(t, "faults", "j17")
        assert "feeds ok" not in text, "every feed is claimed fine while two answer with errors\n" + text
        assert t.alive()
        quit_cleanly(t)


@known(245, "a tsunami Warning whose expiry is earlier than its issue time gets no alert")
def test_j20_warning_with_expiry_before_issue_time(tmp_path: Path) -> None:
    path = warning_with_expiry("2025-07-29T11:51:07-00:00").save(tmp_path / "odd.jsonl")
    with launch("--replay", str(path)) as t:
        t.wait_for(lambda s: "shown of" in s.text, 45, "dashboard")
        t.settle(2.0)
        text = evidence(t, "j20", "odd-expiry")
        assert "TSUNAMI WARNING" in text, "a Warning was published and the screen says nothing:\n" + text


def test_j20_control_warning_with_a_sane_expiry_alerts(tmp_path: Path) -> None:
    path = warning_with_expiry("2025-07-30T03:51:07-00:00").save(tmp_path / "sane.jsonl")
    with launch("--replay", str(path)) as t:
        t.wait_for(lambda s: "TSUNAMI WARNING" in s.text, 45, "the Warning banner")
        clean(t, "sane-expiry", "j20")


@needs_posix
@known(246, "b with a console browser leaves the app dead (written on Windows, not yet run on POSIX)", strict=False)
def test_j4_browser_key_with_a_console_browser(tmp_path: Path) -> None:
    fake = tmp_path / "console-browser"
    fake.write_text("#!/bin/sh\nprintf '\\033[?1049h\\033[2J'\nsleep 2\nprintf '\\033[?1049l'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with launch("--replay", str(USGS), *BASE, env={"BROWSER": str(fake)}) as t:
        ready(t)
        t.send_keys("enter")
        t.wait_for_text("Event page", 10)
        t.send_keys("b")
        time.sleep(3.0)
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        assert t.alive(), "the app died after b"
        t.send_keys("j")
        quit_cleanly(t)


def test_j21_no_traceback_in_the_log_after_a_session() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        for k in ("j", "enter", "escape", "t", "escape", "o", "escape", "?", "?", "s", "m", "m", "r"):
            t.send_keys(k)
            time.sleep(0.2)
        t.settle()
        clean(t, "end", "j21")
        log = log_text(t)
        assert "Traceback" not in log, log
        quit_cleanly(t)


def test_footer_hints_are_clickable_where_they_say(tmp_path: Path) -> None:
    """A user who clicks a footer cell gets what the cell says (the footer is the only on-screen key list)."""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        for label, expect in (("Help", "Keys"), ("Filters", "Minimum magnitude")):
            col, row = footer_cell(t, label)
            t.click(col, row)
            t.wait_for_text(expect, 10)
            t.send_keys("escape")
            t.wait_for(lambda s, e=expect: e not in s.text or "Esc or ? to close" not in s.text, 10, "closed")
        assert t.alive()
        quit_cleanly(t)
