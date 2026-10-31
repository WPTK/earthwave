"""SQLite history: round trips, seen_at, retention, restart replay (quakes only, never alerts), the history screen."""

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

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


def test_day_bounds_utc_and_local():
    start, end = day_bounds(date(2026, 10, 31), "UTC")
    assert (start, end) == (datetime(2026, 10, 31, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    start, end = day_bounds(date(2026, 10, 31), "local")
    local = start.astimezone()
    assert (local.day, local.hour, local.minute) == (31, 0, 0)
    assert end.astimezone().day == 1 and timedelta(hours=23) <= end - start <= timedelta(hours=25)


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
    history.sync_seen(lambda _id: False)  # the user has acknowledged everything
    await history.close()
    reopened = History(tmp_path / "h.sqlite", 90, clock)
    reopened.open()
    quakes, unseen = reopened.load_since(T0 - timedelta(days=1))
    assert {q.id for q in quakes} == {"new", "old"} and unseen == set()  # the ack was saved
    assert [q.id for q in reopened.quakes_between(T0 - timedelta(hours=1), T0)] == ["new", "old"]  # newest first
    assert reopened.alert_levels_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == {levels.WARNING: 1}


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
    text = stats_text([make_quake("a", 5.2), make_quake("b", 3.0)], {"WARNING": 1, "INFORMATION": 2})
    assert text == "2 quakes, largest M5.2, 1 at M5 or more; tsunami alerts: 2 Information, 1 Warning"
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
        assert screen.query_one(QuakeList).rows == []  # today (a day later) is empty ...
        await pilot.press("left")
        await pilot.pause()
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
            if screen.query_one(QuakeList).rows:
                break
            await pilot.pause(0.05)
        assert screen.query_one(QuakeList).selected.id == "q1"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, HistoryScreen)


async def test_history_key_without_history_stays_on_the_dashboard():
    app = EarthwaveApp(Config(), clock=ReplayClock(T0, real=lambda: 0.0), run_scheduler=False, ticks=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert app.screen is app.dashboard


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
