"""UAT: the gauge strip on the tsunami screen, the observations view, and the DART event-mode replay."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import make_quake, wait_until
from test_observations import REPLAY, GatedReplay, drive, gauge_app

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch
from earthwave.net import recording_start
from earthwave.notify.desktop import RecordingNotifier
from earthwave.sources.base import SourceHealth
from earthwave.state.store import Store
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from uat.harness import screen_text, strip_app


async def test_gauge_strip_on_the_tsunami_screen(size):
    app = strip_app()
    async with app.run_test(size=size) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TsunamiScreen)
        text = screen_text(app)
        assert "South Dutch Harbor" in text and "last batch" in text and "1850 km" in text
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_observations_view_for_a_flagged_quake(clock, size):
    app = gauge_app(clock)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen) and len(app.screen_stack) == 3
        await wait_until(pilot, lambda: "esc Back" in screen_text(app), what="the footer")
        text = screen_text(app)
        assert "somewhere" in text and "Stations nearest this quake" in text.replace("\n", " ")
        assert "has not flagged" not in text and "esc Back" in text
        await pilot.press("o")  # `o` toggles back, like Esc
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_observations_view_says_so_for_an_unflagged_quake(clock):
    app = gauge_app(clock)
    quiet = make_quake("us2", 8.5, lat=0.0, lon=0.0, time=clock.now())
    app.store.merge(Batch("usgs", clock.now(), quakes=(quiet,), backfill=True), clock)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert "has not flagged this quake for a tsunami" in screen_text(app).replace("\n", " ")
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


@pytest.mark.parametrize("terminal", [(120, 30), (200, 50)])
async def test_dart_event_mode_replay_on_screen(monkeypatch, terminal):
    """Real 32401 payloads: stale standard batch ('last batch'), then the 3 cm pulse flagged, one toast, ticker line."""
    monkeypatch.setattr("earthwave.notify.inapp.GAUGE_SECONDS", 1e9)
    clock = ReplayClock(recording_start(REPLAY), real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, alerts=replace(config.alerts, gauge_anomaly_dart_m=0.02))
    store = Store(config.alerts, config.home)
    quake = make_quake("us_chile", 7.2, time=datetime(2026, 10, 9, 16, 30, tzinfo=UTC), lat=-20.5, lon=-70.5)
    store.merge(Batch("usgs", clock.now(), quakes=(replace(quake, tsunami_flag=True),), backfill=True), clock)
    fake = RecordingNotifier()
    transport = GatedReplay(REPLAY, clock)
    app = EarthwaveApp(config, store=store, clock=clock, transport=transport, notifier=fake)
    async with app.run_test(size=terminal) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen)

        # standard mode: the strip says 'last batch', never implying a live stream
        await drive(clock, lambda: app.store.gauge_readings("32401") and transport.requests >= 2, step=300)
        app.screen.refresh_gauges()
        await pilot.pause()
        text = screen_text(app)
        assert "180 NM West of Iquiq" in text  # the DART buoy nearest the quake
        assert "last batch" in text and not fake.sent

        # event mode: the pulse is flagged on screen and announced once
        transport.gate.set()
        await drive(clock, lambda: any(n.title == "Gauge anomaly" for n in fake.sent))
        await app._outbox.join()

        def flagged() -> bool:
            app.screen.refresh_gauges()
            return "-0.031 m !" in screen_text(app)

        await drive(clock, flagged, step=15)
        assert [n.title for n in fake.sent].count("Gauge anomaly") == 1
        assert next(r for r in app.gauge_rows() if r.station_id == "32401").mode != "15m"

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard
        assert "Gauge anomaly" in screen_text(app)  # the ticker line
        assert app.dart.interval(SourceHealth()) in (60.0, 900)
