"""SQLite history: round trips, seen_at, retention, restart replay (quakes only, never alerts), the history screen."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

from conftest import T0, make_quake

from earthwave.app import EarthwaveApp, PollDone
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch, TsunamiAlert
from earthwave.poll import PollResult
from earthwave.sources import levels
from earthwave.state.events import QuakeNew
from earthwave.state.history import History, day_bounds, decode_quake, encode_quake
from earthwave.state.store import Store
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.history import HistoryScreen, stats_text
from earthwave.ui.widgets.quake_list import QuakeList


def test_quake_round_trip_keeps_every_field():
    quake = make_quake(
        "us1", 6.1, tsunami_flag=True, pager_alert="orange", felt=12, cdi=4.5, also_ids=("ak1", "pt2"), mmi=7.1
    )
    assert decode_quake(encode_quake(quake)) == quake
    bare = make_quake("us2", updated=None)
    assert decode_quake(encode_quake(bare)) == bare


class Eastern(tzinfo):
    """US Eastern around the 2026 spring change (DST began 2026-03-08 at 02:00); needs no zone database in CI."""

    def utcoffset(self, dt):
        return timedelta(hours=-4) if dt.replace(tzinfo=None) >= datetime(2026, 3, 8, 2) else timedelta(hours=-5)

    def dst(self, dt):
        return timedelta(hours=1) if self.utcoffset(dt) == timedelta(hours=-4) else timedelta(0)


def test_day_bounds_zone_utc_and_dst():
    start, end = day_bounds(date(2026, 10, 31), UTC)
    assert (start, end) == (datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    start, end = day_bounds(date(2026, 3, 8), Eastern())  # the day the clocks spring forward
    assert (start, end) == (datetime(2026, 3, 8, 5, tzinfo=UTC), datetime(2026, 3, 9, 4, tzinfo=UTC))
    assert end - start == timedelta(hours=23)
    local = day_bounds(date(2026, 10, 31))  # this machine's zone, whatever it is: midnight to midnight
    assert local[0].astimezone().hour == 0 and local[1].astimezone().hour == 0 and local[1] > local[0]


def test_decode_tolerates_other_versions():
    data = json.loads(encode_quake(make_quake("v2")))
    data["new_field"] = 1
    del data["felt"], data["also_ids"]
    quake = decode_quake(json.dumps(data))
    assert quake.id == "v2" and quake.felt is None and quake.also_ids == ()


def test_schema_version_is_recorded(tmp_path, clock):
    History(tmp_path / "h.sqlite", 90, clock).open()
    assert sqlite3.connect(tmp_path / "h.sqlite").execute("PRAGMA user_version").fetchone() == (1,)


def warn_alert(alert_id: str, issued: datetime, expires: datetime) -> TsunamiAlert:
    return TsunamiAlert(
        alert_id, alert_id, "tsunamigov", levels.WARNING, "Tsunami Warning", issued=issued, expires=expires
    )


async def test_record_seen_and_read_back(tmp_path, clock):
    history = History(tmp_path / "h.sqlite", 90, clock)
    history.open()
    history.start()
    new, old = make_quake("new", 5.0, minutes_ago=10), make_quake("old", 4.0, minutes_ago=20)
    history.record([new, old], {"new"}, [warn_alert("a1", T0, T0 + timedelta(hours=1))], T0)
    history.mark_seen({"new"})  # the user has acknowledged it
    await history.close()
    reopened = History(tmp_path / "h.sqlite", 90, clock)
    reopened.open()
    quakes, unseen = reopened.load_since(T0 - timedelta(days=1))
    assert {q.id for q in quakes} == {"new", "old"} and unseen == set()  # the ack was saved
    day, alerts = reopened.day(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [q.id for q in day] == ["new", "old"] and alerts == {levels.WARNING: 1}  # newest first


async def test_unseen_survives_without_an_ack(tmp_path, clock):
    history = History(tmp_path / "h.sqlite", 90, clock)
    history.open()
    history.start()
    history.record([make_quake("new")], {"new"}, [], T0)
    await history.close()
    again = History(tmp_path / "h.sqlite", 90, clock)
    again.open()
    assert again.load_since(T0 - timedelta(days=1))[1] == {"new"}


async def test_retention_trims_old_rows_on_write_and_on_open(tmp_path, clock):
    path = tmp_path / "h.sqlite"
    history = History(path, 90, clock)
    history.open()
    history.start()
    old_alert = warn_alert("oldalert", T0 - timedelta(days=95), T0 - timedelta(days=95))
    kept = make_quake("kept", time=T0 - timedelta(days=89))
    history.record([make_quake("ancient", time=T0 - timedelta(days=91)), kept], set(), [old_alert], T0)
    await history.close()
    assert sqlite3.connect(path).execute("SELECT id FROM quakes").fetchall() == [("kept",)]
    assert sqlite3.connect(path).execute("SELECT count(*) FROM alerts").fetchone() == (0,)
    clock.advance(timedelta(days=2).total_seconds())  # "kept" is now 91 days old
    later = History(path, 90, clock)
    later.open()
    assert later.load_since(T0 - timedelta(days=200))[0] == []


def test_stats_text_is_words():
    assert stats_text([], {}) == "No saved quakes for this day"
    text = stats_text([make_quake("a", 5.2), make_quake("b", 3.0)], {"INFORMATION": 2, "WARNING": 1, "WATCH": 1})
    assert text == "2 quakes, largest M5.2, 1 at M5 or more; tsunami alerts: 1 Warning, 1 Watch, 2 Information"
    assert stats_text([make_quake("a", 2.0)], {}).endswith("no tsunami alerts")


# --- restart: quit, relaunch --offline -------------------------------------------------------------------------


def _app(path, clock, **kwargs) -> EarthwaveApp:
    config = with_overrides(Config(), 2.5, 24, None, "unicode")
    config = replace(config, view=replace(config.view, time_zone="UTC"))  # day windows independent of this machine
    history = History(path, config.history.retain_days, clock)
    history.open()
    store = Store(config.alerts, config.home)
    return EarthwaveApp(config, store=store, clock=clock, ticks=False, run_scheduler=False, history=history, **kwargs)


def _poll(app: EarthwaveApp, quakes=(), alerts=(), events=()) -> None:
    batch = Batch("usgs", app.clock.now(), quakes=tuple(quakes), alerts=tuple(alerts))
    app.store.merge(batch, app.clock)
    app.post_message(PollDone(PollResult("usgs", 0, tuple(events), batch)))


async def test_relaunch_offline_keeps_yesterday_and_acks_but_never_alerts(tmp_path):
    path = tmp_path / "h.sqlite"
    clock = ReplayClock(T0, real=lambda: 0.0)
    seen, unseen = make_quake("seen", 4.0, minutes_ago=30), make_quake("unseen", 6.2, minutes_ago=20)
    live = warn_alert("live", T0 - timedelta(minutes=10), T0 + timedelta(hours=1))
    app = _app(path, clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.store.warm = True
        _poll(app, [seen, unseen], [live], [QuakeNew(seen), QuakeNew(unseen)])
        await pilot.pause()
        assert app.tracker.unseen() == 2 and app.store.active_alerts(clock)  # the warning was live when saved
        app.tracker.ack(seen.id)  # the user looked at one of them, then quit
    later = ReplayClock(T0 + timedelta(hours=14), real=lambda: 0.0)  # 02:00 UTC next day, still in the 24 h window
    app2 = _app(path, later, offline=True)
    async with app2.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not app2.scheduler_running
        assert {q.id for q in app2.store.all()} == {"seen", "unseen"}
        assert app2.tracker.is_unseen("unseen") and not app2.tracker.is_unseen("seen")  # the ack survived
        assert app2.store.alerts() == () and app2.store.active_alerts(later) == []  # an alert is never replayed
        assert app2.tracker.tsunami_unacked() == 0
        await pilot.press("h")
        await pilot.pause()
        screen = app2.screen
        assert isinstance(screen, HistoryScreen)
        await app2.workers.wait_for_complete()
        assert screen.query_one(QuakeList).rows == []  # today (a day later) is empty ...
        await pilot.press("left")
        await app2.workers.wait_for_complete()
        assert [q.id for q in screen.query_one(QuakeList).rows] == ["unseen", "seen"]  # ... yesterday is there
        assert "1 Warning" in str(screen.query_one("#history_stats").render())
        for _ in range(3):  # cannot go past today
            await pilot.press("right")
        assert screen.day == screen.today()
        await pilot.press("escape")
        assert app2.screen is app2.dashboard


async def test_history_screen_enter_opens_detail_and_back(tmp_path):
    clock = ReplayClock(T0, real=lambda: 0.0)
    app = _app(tmp_path / "h.sqlite", clock, offline=True)
    app.history.start()
    app.history.record([make_quake("q1", 5.5, minutes_ago=1)], set(), [], T0)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        for _ in range(50):  # the writer task commits in a thread
            screen.refresh_day()
            await app.workers.wait_for_complete()
            if screen.query_one(QuakeList).rows:
                break
            await pilot.pause(0.05)
        assert screen.query_one(QuakeList).selected.id == "q1"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, HistoryScreen)


async def test_h_says_why_there_is_no_history_and_stays_on_the_dashboard():
    app = EarthwaveApp(
        Config(), clock=ReplayClock(T0, real=lambda: 0.0), run_scheduler=False, ticks=False, history_error="disk full"
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app._notifications)
        await pilot.press("h")  # the binding is the only thing that can add this notification
        await pilot.pause()
        assert app.screen is app.dashboard
        assert [n.message for n in app._notifications][before:] == ["disk full"]


async def test_open_failure_is_announced_at_mount_and_offline_says_there_is_nothing_to_show():
    app = EarthwaveApp(
        Config(), clock=ReplayClock(T0, real=lambda: 0.0), ticks=False, offline=True, history_error="OperationalError"
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        text = " | ".join(n.message for n in app._notifications)
        assert "History unavailable" in text and "OperationalError" in text
        assert "no saved history to show" in text and "showing saved history" not in text


async def test_unreadable_history_at_startup_is_closed_and_reported(tmp_path, clock):
    path = tmp_path / "h.sqlite"
    app = _app(path, clock)
    sqlite3.connect(path).execute("DROP TABLE quakes").connection.commit()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.history is None and "OperationalError" in (app.history_error or "")
        assert app.screen is app.dashboard


async def test_a_failed_read_shows_one_line_and_does_not_crash(tmp_path, clock):
    app = _app(tmp_path / "h.sqlite", clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        sqlite3.connect(app.history.path).execute("DROP TABLE quakes").connection.commit()
        await pilot.press("h")
        await app.workers.wait_for_complete()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        assert "could not be read" in str(screen.query_one("#history_stats").render())
        await pilot.press("escape")
        assert app.screen is app.dashboard


async def test_changing_the_day_resets_cursor_and_scroll(tmp_path, clock):
    app = _app(tmp_path / "h.sqlite", clock)
    rows = [make_quake(f"q{i:02d}", 4.0, minutes_ago=i) for i in range(40)]
    rows += [make_quake("old", 4.0, time=T0 - timedelta(days=1, hours=1))]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app.history.start()
        app.history.record(rows, set(), [], T0)
        await app.history.close()
        app.history.start()
        await pilot.press("h")
        await app.workers.wait_for_complete()
        quake_list = app.screen.query_one(QuakeList)
        for _ in range(35):
            quake_list.action_cursor_down()
        await pilot.pause()
        assert quake_list.cursor == 35 and quake_list.scroll_offset.y > 0
        await pilot.press("left")
        await app.workers.wait_for_complete()
        assert [q.id for q in quake_list.rows] == ["old"]
        assert quake_list.cursor == 0 and quake_list.scroll_offset.y == 0


# --- alert counts follow the store's merged view --------------------------------------------------------------


def _ntwc(alert_id: str, source: str, level: str = levels.WARNING, **kwargs) -> TsunamiAlert:
    return TsunamiAlert(
        alert_id,
        alert_id,
        source,
        level,
        "Tsunami Warning",
        areas=("Alaska",),
        issued=T0 - timedelta(minutes=30),
        expires=T0 + timedelta(hours=3),
        quake_ref="q1",
        **kwargs,
    )


async def test_one_product_from_two_sources_updated_then_cancelled_counts_once_each(tmp_path, clock):
    app = _app(tmp_path / "h.sqlite", clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.store.warm = True
        quake = make_quake("q1", 7.1, minutes_ago=40)
        _poll(app, [quake], [_ntwc("tg-1", "tsunamigov")])
        _poll(app, alerts=[_ntwc("nws-1", "nws")])  # the same warning, second source
        _poll(app, alerts=[_ntwc("tg-1", "tsunamigov", updated=T0)])  # and an update of it
        cancel = TsunamiAlert(
            "tg-2", "tg-2", "tsunamigov", levels.CANCELLATION, "Cancelled", references=("tg-1",), quake_ref="q1",
            issued=T0,
        )  # fmt: skip
        _poll(app, alerts=[cancel])
        await pilot.pause()
        await app.history.close()
        _, counts = app.history.__class__(app.history.path, 90, clock).day(
            T0 - timedelta(days=1), T0 + timedelta(days=1)
        )
    assert counts == {levels.WARNING: 1, levels.CANCELLATION: 1}


# --- cursor and scroll stay on the same quake when a refresh inserts rows above ---------------------------------


async def test_refresh_inserting_above_keeps_cursor_and_scroll_on_the_same_quake():
    clock = ReplayClock(T0, real=lambda: 0.0)
    config = with_overrides(Config(), 0.0, 24, None, "unicode")
    store = Store(config.alerts, config.home)
    rows = [make_quake(f"q{i:02d}", 4.0, minutes_ago=10 + i) for i in range(40)]
    store.merge(Batch("usgs", T0, quakes=tuple(rows), backfill=True), clock)
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        for _ in range(30):
            quake_list.action_cursor_down()
        await pilot.pause()
        assert quake_list.scroll_offset.y > 0
        cursor_id = quake_list.selected.id
        top_id = quake_list.rows[quake_list.scroll_offset.y].id
        for k in range(3):  # three refreshes, each putting a fresh quake at the very top
            store.merge(Batch("usgs", T0, quakes=(make_quake(f"n{k}", 4.0, minutes_ago=1),), backfill=True), clock)
            app.dashboard.refresh_view()
            await pilot.pause()
            assert quake_list.selected.id == cursor_id
            assert quake_list.rows[quake_list.scroll_offset.y].id == top_id
            assert quake_list.scroll_offset.y <= quake_list.cursor < quake_list.scroll_offset.y + quake_list.size.height


async def test_sort_change_keeps_the_cursor_quake_on_screen():
    clock = ReplayClock(T0, real=lambda: 0.0)
    config = with_overrides(Config(), 0.0, 24, None, "unicode")
    store = Store(config.alerts, config.home)
    rows = [make_quake(f"q{i:02d}", 5.0 - i * 0.05, minutes_ago=10 + i) for i in range(40)]
    rows[5] = make_quake("low", 0.5, minutes_ago=15)  # fifth by time, last by magnitude
    store.merge(Batch("usgs", T0, quakes=tuple(rows), backfill=True), clock)
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        for _ in range(5):
            quake_list.action_cursor_down()
        assert quake_list.selected.id == "low" and quake_list.scroll_offset.y == 0
        app.dashboard.action_sort()  # by magnitude: the cursor quake moves to the very bottom
        await pilot.pause()
        top = quake_list.scroll_offset.y
        assert quake_list.selected.id == "low" and quake_list.cursor == 39
        assert top <= quake_list.cursor < top + quake_list.size.height


# --- command line ---------------------------------------------------------------------------------------------


def _run_cli(monkeypatch, tmp_path, argv):
    from earthwave import __main__ as cli

    seen = {}

    class FakeApp:
        def __init__(self, config, **kwargs):
            seen.update(kwargs)

        def run(self):
            pass

    monkeypatch.setattr(cli, "EarthwaveApp", FakeApp)
    monkeypatch.setattr(cli, "default_path", lambda: tmp_path / "h.sqlite")
    code = cli.run(cli.build_parser().parse_args(["--no-config", "--log", str(tmp_path / "log"), *argv]))
    return code, seen


def test_replay_leaves_history_off(monkeypatch, tmp_path):
    replay = Path(__file__).parent / "fixtures" / "replay" / "usgs_short.jsonl"
    code, seen = _run_cli(monkeypatch, tmp_path, ["--replay", str(replay)])
    assert code == 0 and seen["history"] is None and not (tmp_path / "h.sqlite").exists()


def test_a_normal_launch_opens_history(monkeypatch, tmp_path):
    code, seen = _run_cli(monkeypatch, tmp_path, ["--offline"])
    assert code == 0 and isinstance(seen["history"], History) and seen["offline"] is True


def test_offline_cannot_combine_with_record_or_replay(monkeypatch, tmp_path):
    for flag in ("--record", "--replay"):
        code, seen = _run_cli(monkeypatch, tmp_path, ["--offline", flag, str(tmp_path / "x.jsonl")])
        assert code == 2 and not seen
