"""Round 4 (core): backfill backoff, the feed-wait wording, CO-OPS residual merge, rate-limit keys, the recorder, the
silent-batch window and the gauge pin. Each test fails on the code it replaced."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from conftest import make_quake
from test_observations import gauge_app
from test_ui import HOUR_BODY, week_app

from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig
from earthwave.models import Batch, GaugeReading
from earthwave.net import PoliteTransport, RecordingTransport
from earthwave.poll import Freshness, PollResult
from earthwave.sources import coops
from earthwave.state.events import QuakeMajor, QuakeNew
from earthwave.state.store import Store

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

# --- 1. a failing fdsnws backfill backs off ---------------------------------------------------------------------------


class Done:
    def __init__(self, result: PollResult):
        self.result = result


def counting(responder):
    seen = []

    def handler(request):
        if "fdsnws" in request.url.path:
            seen.append(request)
            return responder(request)
        return httpx.Response(404)

    return seen, httpx.MockTransport(handler)


async def run_backfill(app, pilot, first=True):
    """M2.5+ for a week is not covered by the stored claims, so the filter change asks fdsnws."""
    await pilot.pause()
    if first:
        app.apply_filters(2.5, 168)
    else:
        app._backfill_if_needed(force=False)
    await asyncio.wait({app._backfill}, timeout=5)
    await pilot.pause()


async def good_steady_poll(app, pilot):
    """What the next good 60 s summary-feed answer does to the backfill decision."""
    app._coverage_checked = False
    app.usgs._startup = []
    batch = Batch("usgs", app.clock.now(), generated=app.clock.now())
    app.on_poll_done(Done(PollResult("usgs", app.scheduler.generation, (), batch)))
    if app._backfill is not None:
        await asyncio.wait({app._backfill}, timeout=5)
    await pilot.pause()


async def test_a_failed_backfill_waits_out_retry_after_before_the_next_good_poll_asks_again():
    seen, transport = counting(lambda r: httpx.Response(429, headers={"Retry-After": "300"}))
    app = week_app(transport=transport)
    async with app.run_test(size=(120, 40)) as pilot:
        await run_backfill(app, pilot)
        assert len(seen) == 1
        app.clock.advance_to(app.clock.now() + timedelta(seconds=61))
        await good_steady_poll(app, pilot)
        assert len(seen) == 1  # a minute later, Retry-After says five
        app.clock.advance_to(app.clock.now() + timedelta(seconds=300))
        await good_steady_poll(app, pilot)
        assert len(seen) == 2


async def test_repeated_backfill_failures_back_off_exponentially():
    _, transport = counting(lambda r: httpx.Response(503))
    app = week_app(transport=transport)
    async with app.run_test(size=(120, 40)) as pilot:
        await run_backfill(app, pilot)
        status = app.scheduler.statuses["usgs"]
        first = status.backfill_due - app.clock.monotonic()
        app.clock.advance_to(app.clock.now() + timedelta(seconds=first + 1))
        await run_backfill(app, pilot, first=False)
        second = status.backfill_due - app.clock.monotonic()
        assert status.backfill_failures == 2 and second > first * 1.5


async def test_a_refused_backfill_is_not_repeated_until_the_filter_changes():
    seen, transport = counting(lambda r: httpx.Response(400))
    app = week_app(transport=transport)
    async with app.run_test(size=(120, 40)) as pilot:
        await run_backfill(app, pilot)
        app.clock.advance_to(app.clock.now() + timedelta(hours=3))
        await good_steady_poll(app, pilot)
        assert len(seen) == 1
        app.apply_filters(2.5, 168)  # a new query is a new chance
        await asyncio.wait({app._backfill}, timeout=5)
        assert len(seen) == 2


async def test_a_failed_backfill_marks_the_feed_partial_until_it_lands_and_survives_good_polls():
    state = {"ok": False}

    def respond(request):
        return httpx.Response(200, content=HOUR_BODY) if state["ok"] else httpx.Response(503)

    _, transport = counting(respond)
    app = week_app(transport=transport)
    async with app.run_test(size=(120, 40)) as pilot:
        await run_backfill(app, pilot)
        status = app.scheduler.statuses["usgs"]
        assert status.health.incomplete
        state["ok"] = True
        status.backfill_due = 0.0
        await run_backfill(app, pilot, first=False)
        assert not status.health.incomplete and not status.partial


async def test_a_good_steady_poll_keeps_the_partial_mark():
    def handler(request):
        if "fdsnws" in request.url.path:
            return httpx.Response(503)
        return httpx.Response(200, content=HOUR_BODY)

    app = week_app(transport=httpx.MockTransport(handler))
    async with app.run_test(size=(120, 40)) as pilot:
        await run_backfill(app, pilot)
        status = app.scheduler.statuses["usgs"]
        assert status.partial
        await app.scheduler.poll_source("usgs", app.client)
        assert status.health.failures == 0 and status.health.incomplete


# --- 2. the feed-wait wording -----------------------------------------------------------------------------------------


async def test_quake_feed_wait_says_not_answering_when_stale_or_failing_warm_or_not():
    app = week_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        health = app.scheduler.statuses["usgs"].health
        health.last_ok = app.clock.now() - timedelta(minutes=4)
        health.failures = 0
        assert app.scheduler.freshness("usgs") is Freshness.STALE
        assert app.quake_feed_wait() == "USGS is not answering; last data 4m ago"  # STALE, not warm
        app.store.warm = True
        assert app.quake_feed_wait() == "USGS is not answering; last data 4m ago"  # and warm
        health.last_ok, health.failures = app.clock.now() - timedelta(seconds=30), 2
        assert app.quake_feed_wait() == "USGS is not answering; last data 30s ago"
        health.failures = 0
        assert app.quake_feed_wait() == ""  # fresh and warm
        app.store.warm = False
        assert app.quake_feed_wait() == "Waiting for USGS"


async def test_an_empty_filtered_list_blames_a_stale_feed_not_the_filter():
    app = week_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.store.warm = True
        app.scheduler.statuses["usgs"].health.last_ok = app.clock.now() - timedelta(hours=3)
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        text = str(app.dashboard.query_one("#empty").render())
        assert "USGS is not answering; last data 3h ago" in text and "No quakes match" not in text


# --- 3. CO-OPS mode flip keeps the off-mark residuals ----------------------------------------------------------------


def test_a_coops_mode_flip_keeps_residuals_the_new_predictions_cannot_reproduce():
    clock = ReplayClock(T0, real=lambda: 0.0)
    store = Store(AlertsConfig())
    minutes = [T0 - timedelta(hours=5) + timedelta(minutes=m) for m in range(0, 300)]
    peak = T0 - timedelta(minutes=57)  # 1-minute data, off the 6-minute marks
    assert peak.minute % 6 != 0
    one = [GaugeReading("9414290", "coops", t, 0.6 if t == peak else 0.0, "1m") for t in minutes]
    store.predictions_for = lambda sid: [coops.Prediction(sid, t, 0.0) for t in minutes]
    store.merge(Batch("coops", T0, readings=tuple(one)), clock)
    assert max(r.residual_m for r in store.gauge_residuals("9414290")) > 0.5
    marks = [t for t in minutes if t.minute % 6 == 0]
    store.predictions_for = lambda sid: [coops.Prediction(sid, t, 0.0) for t in marks]  # back to 6-minute data
    six = [GaugeReading("9414290", "coops", t, 0.0, "6m") for t in marks[-5:]]
    store.merge(Batch("coops", T0, readings=tuple(six)), clock)
    assert max(r.residual_m for r in store.gauge_residuals("9414290")) > 0.5


# --- 4. the rate-limit block is per endpoint ---------------------------------------------------------------------------


async def test_a_dated_query_url_honours_the_rate_limit_of_the_endpoint():
    clock = ReplayClock(T0, real=lambda: 0.0)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "120"})

    transport = PoliteTransport(httpx.MockTransport(handler), clock)
    base = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    other = "https://api.tidesandcurrents.noaa.gov/api/prod/other"
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get(base, params={"begin_date": "20261031 11:00"})).status_code == 429
        later = await client.get(base, params={"begin_date": "20261031 11:01"})
        assert later.status_code == 429 and len(calls) == 1  # synthetic: the origin was not asked
        assert 0 < int(later.headers["Retry-After"]) <= 120
        await client.get(other)
        assert len(calls) == 2  # a different path is asked
        clock.advance_to(T0 + timedelta(seconds=200))
        await client.get(base, params={"begin_date": "x"})
        assert len(calls) == 3


async def test_expired_blocks_are_purged_on_the_next_request():
    clock = ReplayClock(T0, real=lambda: 0.0)

    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(429, headers={"Retry-After": "10"})
        return httpx.Response(200)

    transport = PoliteTransport(httpx.MockTransport(handler), clock)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://x.test/a?q=1")
        assert len(transport._blocked) == 1
        clock.advance_to(T0 + timedelta(seconds=60))
        await client.get("https://x.test/b")
        assert transport._blocked == {}


# --- 5. a recorder write error does not fail the poll ------------------------------------------------------------------


async def test_a_recording_disk_error_lets_the_response_through_and_stops_recording(tmp_path, caplog, monkeypatch):
    clock = ReplayClock(T0, real=lambda: 0.0)
    transport = RecordingTransport(
        httpx.MockTransport(lambda r: httpx.Response(200, text="hello")), tmp_path / "rec.jsonl", clock
    )
    attempts = []

    def full(line):
        attempts.append(line)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transport, "_append", full)
    with caplog.at_level(logging.ERROR, logger="earthwave.net"):
        async with httpx.AsyncClient(transport=transport) as client:
            first = await client.get("https://x.test/1")
            second = await client.get("https://x.test/2")
    assert first.text == second.text == "hello"
    assert len(attempts) == 1 and len(caplog.records) == 1  # logged once, then recording stopped


# --- 6. a new major quake published late is announced by a silent batch -------------------------------------------------


def warm_store():
    clock = ReplayClock(T0, real=lambda: 0.0)
    store = Store(AlertsConfig())
    store.merge(Batch("usgs", T0, generated=T0, quakes=(make_quake("old", 4.0),)), clock)
    assert store.warm
    return store, clock


def test_a_new_major_quake_listed_long_after_its_origin_is_still_announced_by_a_silent_batch():
    store, clock = warm_store()
    late = make_quake("late", 7.5, time=T0 - timedelta(minutes=12), updated=T0 + timedelta(seconds=30))
    events = store.merge(Batch("usgs", T0 + timedelta(seconds=40), quakes=(late,), backfill=True), clock)
    assert [type(e) for e in events] == [QuakeNew, QuakeMajor]


def test_a_day_old_unheld_quake_and_a_held_quake_merely_revised_stay_quiet():
    store, clock = warm_store()
    ancient = make_quake("ancient", 6.0, time=T0 - timedelta(days=3), updated=T0 + timedelta(seconds=30))
    assert store.merge(Batch("usgs", T0, quakes=(ancient,), backfill=True), clock) == []
    revised = replace(store.get("old"), mag=4.1, updated=T0 + timedelta(seconds=30))
    assert store.merge(Batch("usgs", T0, quakes=(revised,), backfill=True), clock) == []


# --- 7. pinning the observations quake ----------------------------------------------------------------------------------


async def test_pin_gauge_quake_waits_for_the_flag_follows_a_relocation_and_is_idempotent(clock):
    app = gauge_app(clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake = make_quake("us9", 7.4, lat=52.5, lon=160.3)
        app.store.merge(Batch("usgs", clock.now(), quakes=(quake,), backfill=True), clock)
        app.pin_gauge_quake(quake)
        assert app._gauge_quake is None and app.gauge_selection.ids == ()  # not flagged yet
        flagged = replace(quake, tsunami_flag=True, updated=quake.updated + timedelta(minutes=1))
        app.store.merge(Batch("usgs", clock.now(), quakes=(flagged,), backfill=True), clock)
        app.pin_gauge_quake(quake)  # the screen still holds the unflagged object; the store has the revision
        assert app._gauge_quake is not None and app._gauge_quake.tsunami_flag and app.gauge_selection.ids
        before = app.gauge_selection
        app.pin_gauge_quake(quake)
        assert app.gauge_selection is before  # idempotent: nothing reselected
        moved = replace(flagged, lat=-20.5, lon=-70.5, updated=flagged.updated + timedelta(minutes=1))
        app.store.merge(Batch("usgs", clock.now(), quakes=(moved,), backfill=True), clock)
        app.pin_gauge_quake(quake)
        assert app._gauge_quake.lat == -20.5 and app.gauge_selection != before  # a relocation re-selects


async def test_close_observations_still_releases_the_pin(clock):
    app = gauge_app(clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake = next(q for q in app.store.all() if q.tsunami_flag)
        app.pin_gauge_quake(quake)
        assert app.gauge_selection.ids
        app.close_observations()
        assert app._gauge_quake is None and app.gauge_selection.ids == ()
