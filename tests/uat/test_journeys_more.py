"""Journeys J11, J14, J15, J18, J19, J22, J23, J24 (the rest of J1-J24 are in test_journeys.py and test_install.py).

Same rules: the real program in a real pseudo-terminal, real key bytes, data from ``--replay`` recordings, the generic
screen rules after every step. Nothing under earthwave/ is touched; history is seeded with the app's own
``History`` class into the file the app will open.
"""

from __future__ import annotations

import difflib
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import pytest

from uat import rules
from uat.known import known, unfiled
from uat.replays import Rec
from uat.target import (
    BASE,
    KAM,
    USGS,
    evidence,
    ident,
    launch,
    list_rows,
    log_text,
    needs_kit,
    needs_network,
    needs_posix,
    needs_state,
    program,
    ready,
    selected_row,
    status_line,
)

pytestmark = needs_kit

NO_MODES = ("focus_events",)
LONG = os.environ.get("UAT_LONG") == "1"
LONG_MINUTES = float(os.environ.get("UAT_J19_MINUTES", "1"))


def clean(term, name: str, test: str) -> str:
    text = evidence(term, test, name)
    found = rules.violations(text, term.cols, term.rows)
    assert not found, f"{name}: {found}\n{text}"
    return text


def quit_cleanly(term) -> None:
    term.send_keys("q")
    assert term.wait_exit(10) is not None, "q did not end the process"
    assert "Traceback" not in term.raw_stream()
    left = [m for m in term.modes_left_on() if m not in NO_MODES]
    assert left == [], f"terminal modes left on after exit: {left}"


# -- seeded history -------------------------------------------------------------------------------------------------


class Seeded:
    """The app's saved-history file, written with the app's own History class (the schema is the app's, not ours)."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(
            "XDG_DATA_HOME", str(tmp / "xdg-data")
        )  # platformdirs on Linux and macOS; Windows ignores it
        from earthwave.state.history import History, default_path

        self.path = default_path()
        self._history = History
        self.env = {"XDG_DATA_HOME": str(tmp / "xdg-data")}

    def write(self, quakes: list) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        from earthwave.clock import SystemClock
        from earthwave.state.history import encode_quake

        history = self._history(self.path, 90, SystemClock())
        history.open()
        db = history._db
        stamp = datetime.now().astimezone().isoformat()
        db.executemany(
            "INSERT INTO quakes (id, time, data, seen_at) VALUES (?, ?, ?, ?)",
            [(q.id, q.time.isoformat(), encode_quake(q), stamp) for q in quakes],
        )
        db.commit()
        history.close_now()


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Seeded:
    return Seeded(tmp_path, monkeypatch)


def quake(qid: str, when: datetime, mag: float, place: str):
    from earthwave.models import Earthquake

    when = when.astimezone()
    return Earthquake(
        id=qid, time=when, mag=mag, lat=10.0, lon=20.0, depth_km=10.0, place=place, updated=when, url="", detail_url=""
    )


def local_noon(days_ago: int) -> datetime:
    day = datetime.now().astimezone().date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)


STATS = re.compile(r"(\d+) quakes, largest M(\d+\.\d), (\d+) at M5 or more")


def history_state(term) -> tuple[int, float, int, int]:
    """(count, largest, big, rows drawn) of the day on screen."""
    text = term.screen_text()
    m = STATS.search(text)
    assert m, f"no stats line on the history screen:\n{text}"
    return int(m[1]), float(m[2]), int(m[3]), len(list_rows(term.screen().lines))


# -- J11 ------------------------------------------------------------------------------------------------------------


@needs_state
def test_j11_history_days_stats_and_an_old_quake(seeded: Seeded) -> None:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = max(now - timedelta(minutes=3), midnight + timedelta(minutes=1))
    seeded.write(
        [
            quake("t1", today, 5.4, "Today Strait"),
            quake("t2", today - timedelta(seconds=30), 3.1, "Today Ridge"),
            quake("y1", local_noon(1), 4.8, "Yesterday Basin"),
            quake("y2", local_noon(1) + timedelta(minutes=5), 5.6, "Yesterday Trench"),
            quake("d3", local_noon(3), 6.1, "Three Days Ago Rise"),
            quake("old", local_noon(40), 5.0, "Forty Days Ago Fault"),
        ]
    )
    with launch("--offline", env=seeded.env, isolate=True) as t:
        t.wait_for(lambda s: "shown of" in s.text, 30, "dashboard from saved history")
        t.send_keys("h")
        t.wait_for_text("Previous day", 10)
        t.settle()
        clean(t, "today", "j11")
        count, largest, big, drawn = history_state(t)
        assert (count, largest, big) == (2, 5.4, 1) and drawn == count, (count, largest, big, drawn)
        t.send_keys("left")
        t.settle()
        count, largest, big, drawn = history_state(t)
        assert (count, largest, big) == (2, 5.6, 1) and drawn == count, "yesterday: " + str(
            (count, largest, big, drawn)
        )
        clean(t, "yesterday", "j11")
        t.send_keys("left", "left")
        t.settle()
        count, largest, big, drawn = history_state(t)
        assert (count, largest, big) == (1, 6.1, 1) and drawn == count, "three days ago"
        t.send_keys("left")
        t.settle()
        assert "No saved quakes for this day" in t.screen_text(), "a day with nothing saved says so"
        t.send_keys(*["left"] * 36)  # day 40 back
        t.settle(1.0)
        count, largest, big, drawn = history_state(t)
        assert (count, drawn) == (1, 1), "the 40-day-old quake is listed"
        t.send_keys("enter")
        t.wait_for_text("Event page", 10)
        text = clean(t, "old-detail", "j11")
        assert "Forty Days Ago Fault" in text and "removed" not in text.lower(), text  # #173
        t.send_keys("escape")
        t.wait_for_text("Previous day", 10)
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        quit_cleanly(t)


# -- J14 ------------------------------------------------------------------------------------------------------------


def offline_checks(t, name: str) -> str:
    t.settle(2.0)
    text = clean(t, name, "j14")
    status = status_line(text)
    assert "offline" in status.lower(), f"--offline but the status bar does not say so: {status!r}"
    assert "loading" not in status.lower(), f"every feed is 'loading' for the whole offline session (#175): {status!r}"
    assert "No tsunami alerts in force" not in text, "an all-clear with no tsunami data fetched (#63)"
    return text


@needs_state
def test_j14_offline_with_saved_history(seeded: Seeded) -> None:
    now = datetime.now().astimezone()
    seeded.write(
        [
            quake("o1", now - timedelta(minutes=20), 5.2, "Saved Coast"),
            quake("o2", now - timedelta(hours=3), 4.0, "Older Bay"),
        ]
    )
    with launch("--offline", env=seeded.env) as t:
        t.wait_for(lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)), 30, "saved quakes listed")
        text = offline_checks(t, "with-history")
        assert "Saved Coast" in text
        quit_cleanly(t)


@needs_state
def test_j14_offline_with_an_empty_cache(seeded: Seeded) -> None:
    seeded.write([])
    with launch("--offline", env=seeded.env) as t:
        t.wait_for(lambda s: "shown of" in s.text, 30, "dashboard")
        t.wait_for(lambda s: "not being fetched" in s.text, 10, "the empty list saying nothing is fetched")
        offline_checks(t, "empty-cache")
        quit_cleanly(t)


# -- J15 ------------------------------------------------------------------------------------------------------------


def test_j15_a_feed_that_stops_is_marked_stale_and_nothing_contradicts(tmp_path: Path) -> None:
    rec = Rec.usgs().status("2.5_day.geojson", 500, "unavailable", after="first")
    path = rec.save(tmp_path / "frozen.jsonl")
    with launch("--replay", str(path), "--speed", "120", *BASE) as t:
        t.wait_for(lambda s: "shown of" in s.text, 45, "dashboard")
        t.wait_for(lambda s: "stale" in s.text.lower(), 60, "the word stale once the feed has been silent too long")
        t.settle(1.0)
        text = clean(t, "stale", "j15")
        assert "feeds ok" not in text, text
        assert re.search(r"[1-9]\d* shown of", text), "the old quakes are still listed, marked by the stale feed"
        t.send_keys("t")
        t.settle(1.0)
        clean(t, "stale-tsunami", "j15")
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        quit_cleanly(t)


# -- J18 ------------------------------------------------------------------------------------------------------------


@needs_state
@needs_network
def test_j18_record_then_replay_at_speeds_1_and_5(tmp_path: Path, seeded: Seeded) -> None:
    rec = tmp_path / "session.jsonl"
    env = {**seeded.env, "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_CACHE_HOME": str(tmp_path / "xdg-cache")}
    with launch("--record", str(rec), env=env, isolate=False) as t:
        ready(t, timeout=90)
        live = [ident(r) for _, r in list_rows(t.screen().lines)[:5]]
        t.settle(2.0)
        quit_cleanly(t)
    assert rec.is_file() and rec.stat().st_size > 0, "--record wrote nothing"
    for speed in ("1", "5"):
        with launch("--replay", str(rec), "--speed", speed) as t:
            ready(t, timeout=60)
            clean(t, f"replay-x{speed}", "j18")
            replayed = [ident(r) for _, r in list_rows(t.screen().lines)[:5]]
            assert replayed == live, f"the replay at x{speed} shows different quakes than were recorded"
            quit_cleanly(t)


def test_j18_replay_shows_the_readme_demo_story() -> None:
    """The README image caption: a tsunami watch, then a warning, then the quake in the list and on the map."""
    with launch("--replay", str(KAM), "--speed", "20") as t:
        t.wait_for(lambda s: "WATCH" in s.text.upper() and "TSUNAMI" in s.text, 60, "the watch banner")
        t.wait_for(lambda s: "WARNING" in s.text.upper() and "TSUNAMI" in s.text, 60, "the warning banner")
        t.wait_for(lambda s: bool(list_rows(s.lines)), 30, "the Kamchatka quake in the list")
        clean(t, "demo", "j18")
        quit_cleanly(t)


@unfiled(
    "a --replay session shows nothing on screen that says the data is a recording, not live (the README caption does)"
)
def test_j18_replay_says_it_is_not_live() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        text = evidence(t, "j18", "replay-label").lower()
        assert "replay" in text or "recorded" in text or "not live" in text, text


# -- J19 ------------------------------------------------------------------------------------------------------------

long_only = pytest.mark.skipif(
    not LONG, reason="long session: UAT_LONG=1 (the UAT workflow's long job); UAT_J19_MINUTES sets the length"
)


def rss_mb(term) -> float:
    total = 0
    for pid in term._pids:
        try:
            proc = psutil.Process(pid)
            for p in [proc, *proc.children(recursive=True)]:
                total += p.memory_info().rss
        except psutil.NoSuchProcess:
            pass
    return total / 1e6


def long_session(t, minutes: float, name: str, *, similarity: float) -> None:
    ready(t, timeout=90)
    t.send_keys("j", "j", "j")
    t.settle()
    held = ident(selected_row(t.screen().lines)[1])
    samples: list[float] = []
    end = time.monotonic() + minutes * 60
    tick = 0
    while time.monotonic() < end:
        time.sleep(15)
        tick += 1
        assert t.alive(), f"the app ended by itself after {tick} ticks"
        if tick % 4 == 0:  # screen switches, then back to the same place
            for keys, back in ((("t",), "escape"), (("o",), "escape"), (("h",), "escape"), (("?",), "?")):
                t.send_keys(*keys)
                t.settle(0.5)
                clean(t, f"{name}-switch", "j19")
                t.send_keys(back)
                t.wait_for_text("shown of", 15)
        text = clean(t, f"{name}-{tick:03d}", "j19")
        now = ident(selected_row(t.screen().lines)[1]) if selected_row(t.screen().lines) else None
        assert now is not None, f"no selected row after {tick} ticks\n{text}"
        assert difflib.SequenceMatcher(None, held, now).ratio() >= similarity, f"selection moved: {held!r} -> {now!r}"
        samples.append(rss_mb(t))
    assert len(samples) >= 4, "too short to judge a trend"
    q = len(samples) // 4
    first, last = sum(samples[:q]) / q, sum(samples[-q:]) / q
    evidence_note = f"rss MB first quarter {first:.1f}, last quarter {last:.1f}, all {[round(s) for s in samples]}"
    print(evidence_note)
    assert last - first < max(40.0, 0.3 * first), f"memory grew: {evidence_note}"
    quit_cleanly(t)


@long_only
def test_j19_long_session_on_a_replay() -> None:
    with launch("--replay", str(USGS), "--speed", "5", *BASE) as t:
        long_session(t, LONG_MINUTES, "replay", similarity=1.0)


@long_only
@needs_state
@needs_network
def test_j19_long_session_live(tmp_path: Path, seeded: Seeded) -> None:
    env = {**seeded.env, "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_CACHE_HOME": str(tmp_path / "xdg-cache")}
    with launch(env=env, isolate=False) as t:
        long_session(t, LONG_MINUTES, "live", similarity=0.8)


# -- J22 ------------------------------------------------------------------------------------------------------------


def test_j22_bell_with_notify_and_the_screen_stays_clean() -> None:
    with launch("--replay", str(KAM), "--speed", "20", "--notify") as t:
        t.wait_for(lambda s: "TSUNAMI" in s.text, 60, "a tsunami banner")
        t.settle(2.0)
        clean(t, "notify", "j22")
        quit_cleanly(t)
    assert "\x07" in t.raw_stream(), "--notify with a tsunami alert rang no bell"


@needs_posix
def test_j22_desktop_notice_goes_to_notify_send_and_does_not_touch_the_screen(tmp_path: Path) -> None:
    """The Windows toast and macOS osascript cannot be observed on a runner (no notification centre to read, and the
    toast needs an interactive desktop session): J22 asserts the notify-send path and, on every OS, the bell."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "notify.log"
    fake = bin_dir / "notify-send"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\necho NOISE-ON-STDOUT\necho NOISE-ON-STDERR >&2\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    with launch("--replay", str(KAM), "--speed", "20", "--notify", env={"PATH": path}) as t:
        t.wait_for(lambda s: "TSUNAMI" in s.text, 60, "a tsunami banner")
        t.wait_for(lambda s: log.is_file(), 30, "a notification handed to notify-send")
        t.settle(2.0)
        text = clean(t, "notify-send", "j22")
        assert "NOISE-ON" not in text, "output of the notifier reached the screen (#50)"
        assert "earthwave" in log.read_text(), log.read_text()
        quit_cleanly(t)


# -- J23 ------------------------------------------------------------------------------------------------------------


@known(235, "there is no --diagnose command for bug reports")
def test_j23_diagnose_prints_a_bug_report() -> None:
    result = subprocess.run(
        [*program(), "--diagnose"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
    )
    assert result.returncode == 0, result.stderr[-300:]
    assert result.stdout.strip(), "--diagnose printed nothing"


def hostile_usgs(body: str) -> str:
    import json

    if not body.strip():  # a 304 record has no body
        return body
    data = json.loads(body)
    feats = data["features"]
    if len(feats) >= 4:
        feats[0]["properties"]["mag"] = None
        feats[1]["geometry"]["coordinates"] = ["x", None, "deep"]
        feats[2]["properties"]["place"] = None
        feats[2]["properties"]["time"] = "soon"
        del feats[3]["properties"]
    feats.append(
        {"type": "Feature", "id": "bad", "properties": {"mag": 1e308, "time": 10**18, "place": "\x1b[2J\x07" * 50}}
    )
    return json.dumps(data)


def test_j23_hostile_feed_content_does_not_crash_the_app(tmp_path: Path) -> None:
    """The delta says: a crafted payload through the proxy is the only way to a crash. If this passes, nothing here
    produced a crash, which the report records as a test gap."""
    rec = Rec.usgs()
    for feed in ("4.5_week", "all_day", "2.5_day"):
        rec.body(feed, hostile_usgs)
    path = rec.save(tmp_path / "hostile.jsonl")
    with launch("--replay", str(path), *BASE) as t:
        t.wait_for(lambda s: "shown of" in s.text, 45, "dashboard")
        t.settle(2.0)
        assert t.alive(), "a crafted feed ended the app"
        evidence(t, "j23", "hostile")
        assert "\x07" not in t.screen_text()
        log = log_text(t)
        assert "Traceback" not in t.raw_stream()
        quit_cleanly(t)
    assert "Traceback" not in log or "Unhandled" not in log, log[-800:]


@unfiled("the program never tells the user where its log is (--log is silent; the default path appears nowhere)")
def test_j23_the_log_path_is_printed(tmp_path: Path) -> None:
    logfile = tmp_path / "x.log"
    with launch("--replay", str(USGS), *BASE, "--debug", log=False) as t:
        ready(t)
        quit_cleanly(t)
    out = t.raw_stream()
    assert "log" in out.lower().replace("logging", "") and ("earthwave.log" in out or str(logfile) in out), (
        "no log path in the output"
    )


# -- J24 ------------------------------------------------------------------------------------------------------------

HELP_KEYS = [
    # key sent, what the help line says it does, screen test, key(s) back
    ("enter", "quake detail", lambda s: "Event page" in s, "escape"),
    ("f", "filters", lambda s: "Minimum magnitude" in s, "escape"),
    ("s", "cycle sort", lambda s: re.search(r"sort:(?!time)\w+", s) is not None, None),
    ("/", "search place", lambda s: "Enter apply, Esc cancel" in s, "escape"),
    ("M", "full-screen map", lambda s: "AGE  MAG" not in s, "M"),
    ("t", "tsunami alerts", lambda s: "LEVEL" in s, "escape"),
    ("o", "tide gauges near quake", lambda s: "Back" in s.splitlines()[-1], "escape"),
    ("h", "history by day", lambda s: "No saved history" in s, None),  # under --replay nothing is saved; J11 opens it
    ("?", "this help", lambda s: "Esc or ? to close" in s, "?"),
]


@pytest.mark.parametrize("key,says,shows,back", HELP_KEYS, ids=[k[0] for k in HELP_KEYS])
def test_j24_each_key_the_help_lists_does_what_it_says(key, says, shows, back) -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.send_keys("?")
        t.wait_for_text("Esc or ? to close", 10)
        assert says in t.screen_text(), f"the help no longer lists {says!r}"
        t.send_keys("?")
        t.wait_for(lambda s: "Esc or ? to close" not in s.text, 10, "help closed")
        (t.send_raw if key in "M/" else t.send_keys)(key)
        t.wait_for(lambda s: shows(s.text), 10, f"{key!r} ({says})")
        clean(t, "key-" + {"?": "help", "/": "search", "M": "fullmap"}.get(key, key), "j24")
        if back:
            (t.send_raw if back == "M" else t.send_keys)(back)
            t.wait_for_text("shown of", 10)
        quit_cleanly(t)


def test_j24_the_map_keys_the_help_lists_work() -> None:
    """m hides and shows the map, z zooms it, Tab focuses it and arrows pan (the list header stays)."""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        before = t.screen_text()
        t.send_keys("m")
        t.settle()
        assert t.screen_text() != before, "m changed nothing"
        t.send_keys("m", "z", "z")
        t.settle()
        clean(t, "map", "j24")
        t.send_keys("tab", "right", "right", "left")
        t.settle()
        clean(t, "map-focus", "j24")
        t.send_keys("tab")
        t.send_keys("a", "r")
        t.settle(1.0)
        clean(t, "ack-refresh", "j24")
        quit_cleanly(t)


def test_j24_detail_keys_the_help_lists_back_and_copy() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.send_keys("enter")
        t.wait_for_text("Event page", 10)
        t.send_keys("y")
        t.settle(1.0)
        clean(t, "copy-url", "j24")
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        quit_cleanly(t)


def test_j24_the_help_does_not_say_how_to_leave_it_with_the_mouse() -> None:
    """Recorded, not asserted: the help says 'Esc or ? to close' and nothing about clicking. Does a click outside close it?"""
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        t.send_keys("?")
        t.wait_for_text("Esc or ? to close", 10)
        t.click(2, 2)
        t.settle(0.8)
        closed = "Esc or ? to close" not in t.screen_text()
        evidence(t, "j24", "help-after-click")
        (Path(os.environ.get("UAT_OUT", ".")) / "j24-help-mouse.txt").parent.mkdir(parents=True, exist_ok=True)
        print(f"click outside the help closes it: {closed}")
        if not closed:
            t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        quit_cleanly(t)
