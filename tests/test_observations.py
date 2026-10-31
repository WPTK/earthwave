"""M4c: gauge readings in the store, anomaly episodes, station selection, alert-driven cadence, the gauge views."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FIXTURES, T0, make_quake

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig, Config, with_overrides
from earthwave.models import Batch, GaugeReading, Station, TsunamiAlert
from earthwave.net import ReplayTransport, recording_start
from earthwave.notify.desktop import RecordingNotifier
from earthwave.notify.inapp import GAUGE_SECONDS, AttentionTracker
from earthwave.poll import Scheduler
from earthwave.sources import coops, levels, ndbc
from earthwave.sources.base import SourceHealth
from earthwave.state.events import GaugeAnomaly
from earthwave.state.gauges import GaugeSelection, choose_stations
from earthwave.state.rules import AlertRules
from earthwave.state.store import Store
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen

DART_46402 = (FIXTURES / "ndbc" / "46402.dart").read_text(encoding="utf-8")
STATION = Station("46402", "dart", " South Dutch Harbor - 220 NM SSE of Dutch Harbor, AK", 50.913, -164.147)
ALERTS = AlertsConfig()  # DART threshold 0.05 m


HOLE = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)  # the captured file has a 75-minute gap after this row


def dart_rows(count: int = 40) -> list[GaugeReading]:
    """`count` contiguous rows of the captured 46402 file ending at the last one before its gap, oldest first."""
    return [ndbc.to_reading(r) for r in ndbc.parse_dart(DART_46402, "46402") if r.ts <= HOLE][-count:]


def pulse_rows(after: GaugeReading, minutes: range, amplitude: float) -> list[GaugeReading]:
    """1-minute event-mode rows continuing the tide flat after `after`, with a `amplitude` bump in the middle."""
    return [
        GaugeReading(
            "46402",
            "dart",
            after.ts + timedelta(minutes=m),
            after.value_m + (amplitude if 8 <= m - minutes[0] < 14 else 0),
            "1m",
        )
        for m in minutes
    ]


def merge_dart(store: Store, clock, rows, stations=(STATION,)) -> list:
    return store.merge(Batch("dart", clock.now(), readings=tuple(rows), stations=tuple(stations)), clock)


@pytest.fixture
def dclock() -> ReplayClock:
    """A clock at the newest row of the 46402 fixture."""
    return ReplayClock(dart_rows()[-1].ts + timedelta(minutes=2), real=lambda: 0.0)


# --- store: readings, residuals, bounded history ----------------------------------------------------------------


def test_readings_and_stations_merge_without_warming_the_quake_store(dclock):
    store = Store(ALERTS)
    events = merge_dart(store, dclock, dart_rows())
    assert events == [] and not store.warm
    assert store.gauge_station("46402") == STATION
    assert [r.ts for r in store.gauge_readings("46402")] == [r.ts for r in dart_rows()]
    merge_dart(store, dclock, dart_rows(), stations=())  # the same batch again changes nothing
    assert len(store.gauge_readings("46402")) == 40 and store.gauge_station("46402") == STATION


def test_dart_residuals_use_the_cubic_detrend(dclock):
    store = Store(ALERTS)
    merge_dart(store, dclock, dart_rows(60))
    expected = ndbc.detrend(dart_rows(60))  # type: ignore[arg-type]
    got = store.gauge_residuals("46402")
    assert [r.ts for r in got] == [r.ts for r in expected] and got
    assert got[-1].residual_m == pytest.approx(expected[-1].residual_m)


def test_coops_residuals_use_the_source_predictions(dclock):
    obs = coops.parse_water_level((FIXTURES / "coops" / "water_level_9414290.json").read_text(encoding="utf-8"))
    preds = coops.parse_predictions(
        (FIXTURES / "coops" / "predictions_9414290.json").read_text(encoding="utf-8"), "9414290"
    )
    store = Store(ALERTS)
    store.predictions_for = lambda station_id: preds
    clock = ReplayClock(obs[-1].ts, real=lambda: 0.0)
    store.merge(Batch("coops", clock.now(), readings=tuple(coops.to_reading(r) for r in obs)), clock)
    assert [r.residual_m for r in store.gauge_residuals("9414290")] == pytest.approx(
        [r.residual_m for r in coops.residuals(obs, preds)]
    )


def test_late_rows_recompute_only_what_they_change(dclock):
    rows = dart_rows(60)
    store = Store(ALERTS)
    merge_dart(store, dclock, rows[:40])
    merge_dart(store, dclock, rows)
    reference = Store(ALERTS)
    merge_dart(reference, dclock, rows)
    assert [(r.ts, r.residual_m) for r in store.gauge_residuals("46402")] == [
        (r.ts, r.residual_m) for r in reference.gauge_residuals("46402")
    ]


def test_history_is_bounded_per_station(dclock, monkeypatch):
    monkeypatch.setattr("earthwave.state.store.GAUGE_KEEP", timedelta(hours=2))
    store = Store(ALERTS)
    merge_dart(store, dclock, dart_rows(60))
    kept = store.gauge_readings("46402")
    assert kept[-1].ts - kept[0].ts <= timedelta(hours=2) and len(kept) < 60
    assert all(r.ts >= kept[0].ts for r in store.gauge_residuals("46402"))
    monkeypatch.undo()
    monkeypatch.setattr("earthwave.state.store.GAUGE_MAX_ROWS", 10)
    merge_dart(store, dclock, dart_rows(60))
    kept = store.gauge_readings("46402")
    assert len(kept) == 10 and kept[-1].ts == dart_rows(60)[-1].ts


# --- anomaly episodes --------------------------------------------------------------------------------------------


def anomaly_store(dclock):
    store = Store(ALERTS)
    merge_dart(store, dclock, dart_rows())
    return store


def test_history_on_first_sight_raises_no_anomaly_but_a_fresh_pulse_does(dclock):
    rows = dart_rows()
    pulse = pulse_rows(rows[-1], range(1, 25), 0.15)
    old = ReplayClock(
        dclock.now() + timedelta(hours=3), real=lambda: 0.0
    )  # the whole pulse is hours old when first seen
    assert merge_dart(Store(ALERTS), old, [*rows, *pulse]) == []
    assert [type(e) for e in merge_dart(Store(ALERTS), dclock, [*rows, *pulse])] == [GaugeAnomaly]


def test_one_event_per_anomaly_episode(dclock):
    store = anomaly_store(dclock)
    last = dart_rows()[-1]
    first = pulse_rows(last, range(1, 25), 0.15)
    events = merge_dart(store, dclock, first)
    assert [type(e) for e in events] == [GaugeAnomaly]
    event = events[0]
    assert event.station_id == "46402" and event.name == "South Dutch Harbor - 220 NM SSE of Dutch Harbor, AK"
    assert abs(event.residual_m) >= 0.05 and event.threshold_m == 0.05
    # the pulse keeps going in the next batch: same episode, no new event
    more = pulse_rows(last, range(25, 40), 0.15)
    more = [replace(r, value_m=last.value_m + 0.15) for r in more]
    assert merge_dart(store, dclock, more) == []
    # two quiet hours, then another pulse: a new episode
    calm = pulse_rows(last, range(40, 160), 0.0)  # the detrend takes a while to let go of the step
    assert merge_dart(store, dclock, calm) == []
    again = pulse_rows(last, range(160, 184), 0.15)
    assert [type(e) for e in merge_dart(store, dclock, again)] == [GaugeAnomaly]


def test_resending_a_batch_does_not_repeat_the_event(dclock):
    store = anomaly_store(dclock)
    batch = pulse_rows(dart_rows()[-1], range(1, 25), 0.15)
    assert len(merge_dart(store, dclock, batch)) == 1
    assert merge_dart(store, dclock, batch) == []


def test_backfill_and_silent_batches_track_episodes_without_events(dclock):
    store = anomaly_store(dclock)
    batch = Batch("dart", dclock.now(), readings=tuple(pulse_rows(dart_rows()[-1], range(1, 25), 0.15)), backfill=True)
    assert store.merge(batch, dclock) == []
    more = pulse_rows(dart_rows()[-1], range(25, 30), 0.15)
    more = [replace(r, value_m=r.value_m + 0.15) for r in more]
    assert merge_dart(store, dclock, more) == []  # still the same episode


def test_below_threshold_pulse_is_quiet(dclock):
    store = anomaly_store(dclock)
    assert merge_dart(store, dclock, pulse_rows(dart_rows()[-1], range(1, 25), 0.02)) == []


def test_rules_toast_an_episode_once_and_attention_expires(clock):
    event = GaugeAnomaly("46402", -0.12, T0, "Dutch Harbor", 0.05)
    rules = AlertRules(ALERTS)
    (note,) = rules.evaluate([event])
    assert note.title == "Gauge anomaly" and "Dutch Harbor" in note.body and "-0.120" in note.body
    assert rules.evaluate([event]) == []
    assert AlertRules(replace(ALERTS, tsunami=False)).evaluate([event]) == []
    tracker = AttentionTracker(clock)
    tracker.observe([event])
    assert tracker.gauge_active() == ["46402"] and tracker.unseen() == 0
    clock.advance(GAUGE_SECONDS + 1)
    assert tracker.gauge_active() == []
    tracker.observe([event])
    tracker.ack_all()
    assert tracker.gauge_active() == []


# --- selection ---------------------------------------------------------------------------------------------------


def alert(level=levels.WARNING, **kwargs) -> TsunamiAlert:
    fields = {
        "id": "a1",
        "dedupe_key": "a1",
        "source": "tsunamigov",
        "level": level,
        "headline": "Tsunami",
        "issued": T0 - timedelta(minutes=5),
        "expires": T0 + timedelta(hours=3),
    }
    return TsunamiAlert(**{**fields, **kwargs})


def stations() -> tuple[list[Station], list[Station]]:
    darts = [
        ndbc.to_station(s) for s in ndbc.parse_active_stations((FIXTURES / "ndbc" / "activestations.xml").read_bytes())
    ]
    tides = [
        coops.to_station(s) for s in coops.parse_stations((FIXTURES / "coops" / "stations.json").read_text("utf-8"))
    ]
    return [s for s in darts if s.kind == "dart"], tides


def test_selection_follows_the_geometry_chain(clock):
    darts, tides = stations()
    store = Store(ALERTS)
    store.merge(Batch("usgs", T0, quakes=(make_quake("us1", 7.5, lat=52.5, lon=160.3),), backfill=True), clock)
    nothing = choose_stations(store, clock, darts, tides)
    assert nothing == GaugeSelection() and nothing.ids == ()
    circle = alert(geometry=("circle", 52.5, 160.3, 300.0), origin=(T0, 52.5, 160.3))
    store.merge(Batch("tsunamigov", T0, alerts=(circle,)), clock)
    sel = choose_stations(store, clock, darts, tides)
    assert 0 < len(sel.dart) <= 6 and 0 < len(sel.coops) <= 4
    assert [km for km, _ in sel.dart] == sorted(km for km, _ in sel.dart) and all(km <= 3000 for km, _ in sel.dart)
    assert sel.ids == sel.dart_ids + sel.coops_ids


def test_selection_uses_nws_polygon_for_tide_gauges_and_the_quake_for_buoys(clock):
    darts, tides = stations()
    store = Store(ALERTS)
    quake = make_quake("us1", 7.5, lat=52.5, lon=160.3)
    store.merge(Batch("usgs", T0, quakes=(quake,), backfill=True), clock)
    polygon = ("polygon", ((-124.0, 40.0), (-123.5, 41.0), (-123.0, 40.5), (-124.0, 40.0)))
    nws = alert(source="nws", geometry=polygon, quake_ref="us1")
    store.merge(Batch("nws", T0, alerts=(nws,)), clock)
    sel = choose_stations(store, clock, darts, tides)
    assert sel.coops and all(km < 500 for km, _ in sel.coops)  # tide gauges near the polygon, not the Kamchatka coast
    assert sel.dart and all(km < 3000 for km, _ in sel.dart)  # buoys near the linked quake


def test_manual_quake_and_unavailable_buoys(clock):
    darts, tides = stations()
    store = Store(ALERTS)
    quake = make_quake("us1", 7.0, lat=-20.5, lon=-70.5, tsunami_flag=True)
    sel = choose_stations(store, clock, darts, tides, quake=quake)
    assert "32401" in sel.dart_ids
    skipped = choose_stations(store, clock, darts, tides, unavailable={"32401"}, quake=quake)
    assert "32401" not in skipped.dart_ids and len(skipped.dart) == len(sel.dart)


# --- cadence -----------------------------------------------------------------------------------------------------


class Const:
    name = "x"
    min_interval = 30.0

    def __init__(self):
        self.seconds = 900.0

    def interval(self, health: SourceHealth) -> float:
        return self.seconds

    async def fetch(self, client, since):
        return Batch("x", T0)


async def test_reschedule_pulls_the_next_poll_forward(clock):
    source = Const()
    sched = Scheduler([source], Store(ALERTS), clock, lambda result: None)
    sched.reschedule("x")  # never polled: nothing to move
    await sched.poll_source("x", None)  # type: ignore[arg-type]
    assert sched.next_poll_in("x") == 900
    source.seconds = 60.0
    sched.reschedule("x")
    assert sched.next_poll_in("x") == 60
    source.seconds = 900.0
    sched.reschedule("x")
    assert sched.next_poll_in("x") == 60  # never pushed back
    sched.reschedule("x", immediate=True)
    assert sched.next_poll_in("x") == 30  # but not below the source's minimum interval
    sched.statuses["x"].health.failures = 2
    sched.statuses["x"].next_due = clock.monotonic() + 500
    sched.reschedule("x", immediate=True)
    assert sched.next_poll_in("x") == 500  # a failing source keeps its backoff


def gauge_app(clock, alerts=(), config=None, **kwargs) -> EarthwaveApp:
    config = config or with_overrides(Config(), 2.5, 24, None, "ascii")
    store = Store(config.alerts, config.home)
    quakes = (make_quake("us1", 7.5, lat=52.5, lon=160.3, tsunami_flag=True),)
    store.merge(Batch("usgs", clock.now(), quakes=quakes, backfill=True), clock)
    if alerts:
        store.merge(Batch("tsunamigov", clock.now(), alerts=tuple(alerts)), clock)
    return EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False, **kwargs)


async def test_alert_hint_speeds_both_sources_up_and_back_down(clock):
    watch = alert(levels.WATCH, origin=(T0, 52.5, 160.3), geometry=("circle", 52.5, 160.3, 300.0))
    app = gauge_app(clock, [watch])
    async with app.run_test(size=(120, 40)):
        assert (app.dart.interval(SourceHealth()), app.coops.interval(SourceHealth())) == (900, 300)
        app.sync_gauges()
        assert (app.dart.interval(SourceHealth()), app.coops.interval(SourceHealth())) == (60, 60)
        assert app.gauge_selection.dart_ids and app.dart._selected == app.gauge_selection.dart_ids
        clock.advance(4 * 3600)  # the watch lapses on the injected clock
        app.sync_gauges()
        assert (app.dart.interval(SourceHealth()), app.coops.interval(SourceHealth())) == (900, 300)
        assert app.dart._selected == () and app.gauge_selection == GaugeSelection()


async def test_information_statements_select_stations_without_speeding_up(clock):
    info = alert(levels.INFORMATION, origin=(T0, 52.5, 160.3))
    app = gauge_app(clock, [info])
    async with app.run_test(size=(120, 40)):
        app.sync_gauges()
        assert app.gauge_selection.dart_ids and app.dart.interval(SourceHealth()) == 900


async def test_gauge_sources_follow_the_config(clock):
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    app = gauge_app(clock, config=replace(config, alerts=replace(config.alerts, tsunami=False)))
    assert app.dart is None and set(app.scheduler.statuses) == {"usgs", "tsunamigov", "nws", "jma"}
    app.sync_gauges()  # a no-op without the sources
    on = gauge_app(clock)
    assert set(on.scheduler.statuses) >= {"dart", "coops"}


async def test_o_selects_stations_for_a_flagged_quake_and_opens_the_view(clock):
    app = gauge_app(clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.gauge_selection.ids == ()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen)
        assert app.gauge_selection.dart_ids and app.dart._selected == app.gauge_selection.dart_ids
        assert "has not flagged" not in str(app.screen.query_one("#observations_header").render())
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_o_on_an_unflagged_quake_changes_no_selection(clock):
    app = gauge_app(clock)
    quiet = make_quake("us2", 8.5, lat=0.0, lon=0.0, time=clock.now() - timedelta(minutes=1))  # newest: sorts first
    app.store.merge(Batch("usgs", clock.now(), quakes=(quiet,), backfill=True), clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen) and app.screen.quake.id == "us2"
        assert app.gauge_selection.ids == ()
        assert "has not flagged" in str(app.screen.query_one("#observations_header").render())


# --- replay: DART 32401 from standard to event mode ----------------------------------------------------------------


REPLAY = FIXTURES / "replay" / "dart_event_mode.jsonl"


async def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


class GatedReplay(ReplayTransport):
    """Holds the second 32401 request until the test opens the gate, so the standard-mode state can be inspected."""

    def __init__(self, path, clock):
        super().__init__(path, clock)
        self.gate = asyncio.Event()
        self.requests = 0

    async def handle_async_request(self, request):
        if request.url.path.endswith("/32401.dart"):
            self.requests += 1
            if self.requests == 2:
                await self.gate.wait()
        return await super().handle_async_request(request)


async def test_event_mode_replay_shows_the_pulse_the_anomaly_and_the_cadence_switch(monkeypatch):
    """Real 32401 payloads of 2026-10-09: a stale standard batch, then 1-minute and 15-second rows with a 3 cm pulse."""
    monkeypatch.setattr("earthwave.notify.inapp.GAUGE_SECONDS", 1e9)  # replay at 300x would age the flag out in 1 s
    clock = ReplayClock(recording_start(REPLAY), speed=300)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, alerts=replace(config.alerts, gauge_anomaly_dart_m=0.02))  # the real pulse peaks at 3.1 cm
    store = Store(config.alerts, config.home)
    quake = make_quake("us_chile", 7.2, time=datetime(2026, 10, 9, 16, 30, tzinfo=UTC), lat=-20.5, lon=-70.5)
    store.merge(Batch("usgs", clock.now(), quakes=(replace(quake, tsunami_flag=True),), backfill=True), clock)
    fake = RecordingNotifier()
    transport = GatedReplay(REPLAY, clock)
    app = EarthwaveApp(config, store=store, clock=clock, transport=transport, notifier=fake)
    intervals: list[float] = []  # the cadence the source asked for after each batch that carried 32401 readings
    async with app.run_test(size=(140, 40)) as pilot:
        real_fetch = app.dart.fetch

        async def spy(client, since):
            batch = await real_fetch(client, since)
            if any(r.station_id == "32401" for r in batch.readings):
                intervals.append(app.dart.interval(SourceHealth()))
            return batch

        app.dart.fetch = spy  # type: ignore[method-assign]
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, ObservationsScreen)

        # standard mode: one stale batch, the default 15-minute cadence, and a strip that says 'last batch'
        await wait_for(lambda: app.store.gauge_readings("32401") and transport.requests >= 2)
        row = next(r for r in app.gauge_rows() if r.station_id == "32401")
        assert row.mode == "15m" and intervals == [900]
        app.screen.refresh_gauges()
        await wait_for(lambda: "last batch 6h ago" in strip_text(app))
        assert not app.tracker.gauge_active() and not fake.sent

        # event mode: 1-minute and 15-second rows arrive, the source drops to alert cadence, the pulse trips the flag
        transport.gate.set()
        await wait_for(lambda: intervals[-1:] == [60.0] and len(intervals) >= 3)
        await wait_for(lambda: any(n.title == "Gauge anomaly" for n in fake.sent))
        await app._outbox.join()
        assert [n.title for n in fake.sent].count("Gauge anomaly") == 1  # one toast for the whole episode
        await wait_for(lambda: min(app.store.gauge_residuals("32401"), key=lambda r: r.residual_m).residual_m < -0.025)
        row = next(r for r in app.gauge_rows() if r.station_id == "32401")
        assert row.mode != "15m" and row.threshold_m == 0.02

        def pulse_flagged() -> bool:
            app.screen.refresh_gauges()
            return "-0.031 m !" in strip_text(app)  # the signed maximum of the real pulse, flagged

        await wait_for(pulse_flagged)
        assert app.tracker.gauge_active() == ["32401"]
        assert "Gauge anomaly" in str(app.dashboard.query_one("#ticker").render())

        # back to standard cadence once the event window has passed on the replay clock, with no further fetch
        clock.advance(2 * 3600)
        assert app.dart.interval(SourceHealth()) == 900


def strip_text(app: EarthwaveApp) -> str:
    screen = app.screen
    texts = [str(w.render()) for w in screen.query("Static") if w.has_class("gauge-label") or w.has_class("gauge-max")]
    return "\n".join(texts)


# --- UI ----------------------------------------------------------------------------------------------------------


def strip_app() -> EarthwaveApp:
    """The Kamchatka-like tsunami screen with three gauge rows from saved payloads, fully deterministic."""
    now = datetime(2026, 10, 31, 6, 20, tzinfo=UTC)
    clock = ReplayClock(now, real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, view=replace(config.view, time_zone="UTC"))
    store = Store(config.alerts, config.home)
    rows = dart_rows(120)
    store.merge(Batch("dart", now, readings=tuple(rows), stations=(STATION,)), clock)
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False)
    app.gauge_selection = GaugeSelection(dart=((1850.0, STATION),))
    return app


def test_tsunami_screen_snapshot_with_the_gauge_strip_120x40(snap_compare):
    async def open_screen(pilot):
        await pilot.press("t")
        await pilot.pause()

    assert snap_compare(strip_app(), terminal_size=(120, 40), run_before=open_screen)


async def test_tsunami_screen_embeds_the_strip():
    app = strip_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TsunamiScreen)
        text = strip_text(app)
        assert "South Dutch Harbor" in text and "last batch" in text and "1850 km" in text
