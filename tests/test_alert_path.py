"""Alert-path fixes (review round 2): loading feeds, tsunami toasts, tsunami screen races, backfill alerts, late quakes,
released observation stations and attention that survives filters."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from conftest import make_quake
from test_observations import gauge_app
from test_tsunami import NOW, kam, kam_app, kam_quake
from test_ui import WEEK_NOW, wait_for, week_app

from earthwave.app import EarthwaveApp, PollDone
from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig, Config, SourcesConfig, with_overrides
from earthwave.models import Batch
from earthwave.poll import Freshness, PollResult
from earthwave.state.events import QuakeMajor, QuakeNew, TsunamiIssued
from earthwave.state.rules import AlertRules
from earthwave.state.store import Store
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen

T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def poll_done(batch: Batch, events, source="tsunamigov") -> PollDone:
    return PollDone(PollResult(source, 0, tuple(events), batch))


# --- 1. a feed that has not answered yet is not an all-clear ---------------------------------------------------------


async def test_panel_says_checking_while_tsunamigov_has_not_answered_though_nws_has():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        await asyncio.sleep(3600)  # tsunami.gov is slow on its first poll
        return httpx.Response(500)

    clock = ReplayClock(NOW, real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, sources=SourcesConfig(usgs=False, tsunamigov=True, nws=True, jma=False))
    app = EarthwaveApp(config, clock=clock, transport=httpx.MockTransport(handler))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.scheduler.freshness("nws") is Freshness.FRESH)
        assert app.scheduler.freshness("tsunamigov") is Freshness.LOADING
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "Checking tsunami feeds" in panel and "in force" not in panel


# --- 2. a Warning is toasted on every screen -------------------------------------------------------------------------


async def test_a_new_warning_toasts_over_a_full_screen_view():
    clock = ReplayClock(NOW, real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    store = Store(config.alerts, config.home)
    store.merge(Batch("usgs", NOW, quakes=(kam_quake(),), backfill=True), clock)
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_detail(kam_quake())
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        alert = kam(0)
        batch = Batch("tsunamigov", NOW, alerts=(alert,))
        events = app.store.merge(batch, app.clock)
        assert any(isinstance(e, TsunamiIssued) for e in events)
        app.on_poll_done(poll_done(batch, events))
        await pilot.pause()
        toasts = [(n.title, n.severity) for n in app._notifications]
        assert (f"Tsunami {alert.level.title()}", "error") in toasts


# --- 3. a poll right after the tsunami screen is pushed does not crash -------------------------------------------------


async def test_a_poll_landing_before_the_tsunami_screen_composes_is_harmless():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(TsunamiScreen())  # not awaited: the screen is current, but has not composed
        assert isinstance(app.screen, TsunamiScreen)
        app.on_poll_done(poll_done(Batch("tsunamigov", NOW), []))
        await pilot.pause()
        assert app.is_running and len(app.screen._rows) == 2  # the mount filled the table afterwards


# --- 4. a backfill or feed switch does not swallow a quake the steady feed has not delivered ----------------------------


def warm_store() -> tuple[Store, ReplayClock]:
    clock = ReplayClock(T0, real=lambda: 0.0)
    store = Store(AlertsConfig())
    store.merge(Batch("usgs", T0 - timedelta(minutes=2), quakes=(make_quake("old", 4.0),)), clock)  # steady: warm
    assert store.warm
    return store, clock


def test_a_backfill_announces_a_quake_published_after_the_last_steady_answer():
    store, clock = warm_store()
    big = make_quake("big", 7.5, time=T0 - timedelta(seconds=40), updated=T0 - timedelta(seconds=30))
    events = store.merge(Batch("usgs", T0, quakes=(big,), backfill=True), clock)
    assert [type(e) for e in events] == [QuakeNew, QuakeMajor]
    steady = store.merge(Batch("usgs", T0 + timedelta(seconds=30), quakes=(big,)), clock)
    assert steady == []  # and the steady feed finding it later does not announce it twice


def test_a_backfill_stays_silent_for_quakes_the_steady_feed_already_had_its_chance_at():
    store, clock = warm_store()
    older = make_quake("older", 7.5, time=T0 - timedelta(hours=3), updated=T0 - timedelta(hours=3))
    assert store.merge(Batch("usgs", T0, quakes=(older,), backfill=True), clock) == []


def test_the_first_batch_of_a_switched_feed_announces_only_what_is_new_since_the_last_answer():
    store, clock = warm_store()
    fresh = make_quake("fresh", 6.5, time=T0 - timedelta(seconds=50), updated=T0 - timedelta(seconds=45))
    stale = make_quake("stale", 6.5, time=T0 - timedelta(hours=5))
    events = store.merge(Batch("usgs", T0, quakes=(fresh, stale), meta={"silent": True}), clock)
    assert [(type(e), e.quake.id) for e in events] == [(QuakeNew, "fresh"), (QuakeMajor, "fresh")]


# --- 5. a quake announced long after it happened says so -----------------------------------------------------------------


def test_a_late_major_quake_names_its_age_and_a_burst_does_not_claim_the_last_minute():
    old = make_quake("a", 7.5, time=T0 - timedelta(hours=11), place="Chile")
    (note,) = AlertRules(AlertsConfig()).evaluate([QuakeNew(old), QuakeMajor(old)], T0)
    assert note.body == "M7.5 Chile, 11h ago"
    fresh = make_quake("b", 6.5, time=T0 - timedelta(minutes=8), place="Peru")
    assert AlertRules(AlertsConfig()).evaluate([QuakeMajor(fresh)], T0)[0].body == "M6.5 Peru"
    quakes = [make_quake(f"q{n}", 6.0 + n / 10, time=T0 - timedelta(hours=11), place="Chile") for n in range(3)]
    (burst,) = AlertRules(AlertsConfig()).evaluate([QuakeMajor(q) for q in quakes], T0)
    assert "last minute" not in burst.body and "11h ago" in burst.body


# --- 6. leaving the observations screen releases the pinned quake ------------------------------------------------------


async def test_leaving_observations_stops_polling_the_pinned_quakes_stations(clock):
    app = gauge_app(clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen) and app.dart._selected
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard
        assert app.gauge_selection.ids == () and app.dart._selected == () and app.coops._selected == ()


# --- 7. unseen and major attention survives the magnitude and hours filter -----------------------------------------------


async def test_attention_survives_a_filter_and_a_search_while_the_store_holds_the_quake():
    app = week_app()
    shown = app.store.visible(app.view, app.clock)[0]
    low = make_quake("low", 0.5, time=WEEK_NOW)  # below the 4.5 view filter, as an alert-floor quake can be
    app.store.merge(Batch("usgs", WEEK_NOW, quakes=(low,)), app.clock)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.tracker.observe([QuakeNew(shown), QuakeNew(low)])
        app.set_query(shown.place)
        await pilot.pause()
        assert app.tracker.is_unseen(shown.id) and app.tracker.is_unseen("low")
        app.view = replace(app.view, query="", min_magnitude=9.0)  # a filter that hides both
        app.dashboard.refresh_view()
        assert app.tracker.is_unseen(shown.id) and app.tracker.is_unseen("low")
        app.view = replace(app.view, min_magnitude=0.0)  # lowering it brings the rows back, still highlighted
        app.dashboard.refresh_view()
        assert app.tracker.unseen() == 2
