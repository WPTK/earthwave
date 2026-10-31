"""UAT: every tsunami state the dashboard can be in, built from the replay fixtures' products, plus the live replay."""

from dataclasses import replace

import pytest
from conftest import FIXTURES
from test_tsunami import kam
from test_ui import wait_for

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch
from earthwave.net import ReplayTransport, recording_start
from earthwave.notify.desktop import RecordingNotifier
from earthwave.sources import levels
from earthwave.state.events import TsunamiIssued
from earthwave.ui.screens.tsunami import TsunamiScreen
from uat.harness import info_alert, kam_alert_app, screen_text


def observe(app: EarthwaveApp, *indexes: int) -> None:
    app.tracker.observe([TsunamiIssued(kam(i)) for i in indexes])
    app.sync_tsunami()
    app.dashboard.refresh_view()


async def test_state_none_shows_no_banner_and_a_quiet_panel(size):
    app = kam_alert_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        text = screen_text(app)
        assert "TSUNAMI" not in text and not app.dashboard.query_one("#banner").display
        assert app.dashboard.tsunami_state()[3] == "none"
        if size[0] >= 120:
            assert "No tsunami alerts in force" in text


async def test_state_watch_raises_the_banner_and_the_status_alarm(size):
    app = kam_alert_app(0)
    async with app.run_test(size=size) as pilot:
        observe(app, 0)
        await pilot.pause()
        text = screen_text(app)
        assert "TSUNAMI WATCH" in text and "t details, a acknowledge" in text
        assert app.dashboard.query_one("#status").has_class("alert-alarm")
        if size[0] >= 120:
            assert "WATCH " in text.split("t details, a acknowledge")[-1]  # panel row, level column


async def test_state_warning_banner_then_acknowledge_clears_it(size):
    app = kam_alert_app(0, 1)
    async with app.run_test(size=size) as pilot:
        observe(app, 1)
        await pilot.pause()
        text = screen_text(app)
        assert "TSUNAMI WARNING" in text and "TSUNAMI" in text.splitlines()[-2]  # banner and status bar
        banner = app.dashboard.query_one("#banner")
        assert banner.display
        await pilot.press("a")
        await pilot.pause()
        text = screen_text(app)
        assert "t details, a acknowledge" not in text and not banner.display
        assert app.dashboard.query_one("#status").has_class("alert-active")
        if size[0] >= 120:
            assert "WARNING" in text  # the panel keeps listing it while it is in force


async def test_state_information_is_quiet(size):
    app = kam_alert_app(quake=False)
    info = info_alert()
    app.clock.advance_to(info.issued)
    events = app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(info,)), app.clock)
    async with app.run_test(size=size) as pilot:
        app.tracker.observe(events)
        app.sync_tsunami()
        app.dashboard.refresh_view()
        await pilot.pause()
        text = screen_text(app)
        assert app.dashboard.tsunami_state()[3] == "info"
        assert "TSUNAMI WARNING" not in text and "acknowledge" not in text
        assert app.dashboard.query_one("#status").has_class("alert-info")
        if size[0] >= 120:
            assert "INFORMATION" in text.upper()


async def test_state_cancellation_clears_the_banner_and_is_listed(size):
    app = kam_alert_app(0, 1)
    async with app.run_test(size=size) as pilot:
        observe(app, 1)
        await pilot.pause()
        assert "TSUNAMI WARNING" in screen_text(app)
        events = app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(kam(2),)), app.clock)
        app.tracker.observe(events)
        app.sync_tsunami()
        app.dashboard.refresh_view()
        await pilot.pause()
        text = screen_text(app)
        assert "TSUNAMI WARNING" not in text and "acknowledge" not in text
        assert not app.dashboard.query_one("#banner").display
        assert app.dashboard.tsunami_state()[3] == "none"
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TsunamiScreen)
        assert app.screen.query_one("DataTable").row_count == 3
        assert any(a.level == levels.CANCELLATION for a in app.store.alerts())


async def test_state_follow_up_keeps_one_banner_and_the_alarm(size):
    app = kam_alert_app(0, 1)
    second = kam(1)
    follow = replace(second, id="PAAQ-4-kam004", references=(second.id,), issued=app.clock.now())
    async with app.run_test(size=size) as pilot:
        observe(app, 1)
        events = app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(follow,)), app.clock)
        app.tracker.observe(events)
        app.sync_tsunami()
        app.dashboard.refresh_view()
        await pilot.pause()
        text = screen_text(app)
        assert text.count("TSUNAMI WARNING") == 1  # one banner, not one per product
        assert "more)" not in text.split("t details")[0]  # superseded product is not listed as a second alert
        assert app.dashboard.query_one("#status").has_class("alert-alarm")


async def test_tsunami_screen_lists_alerts_at_each_size(size):
    app = kam_alert_app(0, 1)
    async with app.run_test(size=size) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TsunamiScreen)
        text = screen_text(app)
        assert "Kamchatka" in text and "WARNING" in text.upper()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


@pytest.mark.parametrize("terminal", [(120, 30), (200, 50)])
async def test_replay_drives_banner_up_and_back_down(terminal):
    path = FIXTURES / "replay" / "kamchatka_watch_to_warning.jsonl"
    clock = ReplayClock(recording_start(path), speed=900)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    app = EarthwaveApp(config, clock=clock, transport=ReplayTransport(path, clock), notifier=RecordingNotifier())
    async with app.run_test(size=terminal) as pilot:
        banner = app.dashboard.query_one("#banner")
        await wait_for(lambda: banner.display and "TSUNAMI" in str(banner.render()), timeout=20)
        assert "TSUNAMI" in screen_text(app).splitlines()[1]  # the banner sits under the ticker
        await wait_for(lambda: any(a.level == levels.CANCELLATION for a in app.store.alerts()), timeout=20)
        await wait_for(lambda: not banner.display, timeout=20)
        await pilot.pause()
        assert "t details, a acknowledge" not in screen_text(app)
        assert app.store.active_alerts(clock) == []
