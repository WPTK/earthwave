"""USGS parsing, store merge rules, attention, and the scheduler (backoff, freshness, generation, replay)."""

import asyncio
import random
from datetime import timedelta

import httpx
import pytest
import respx
from conftest import FIXTURES, T0, load_json, make_quake

from earthwave.config import AlertsConfig, HomeConfig
from earthwave.models import Batch
from earthwave.net import ReplayTransport, build_client, recording_start
from earthwave.notify.inapp import (
    DIM_NEW,
    FLASH_NEW,
    FLASH_UPDATED,
    PULSE_MAJOR,
    AttentionTracker,
)
from earthwave.poll import Freshness, Scheduler, backoff_delay, freshness_of
from earthwave.sources.base import SourceHealth
from earthwave.sources.usgs import (
    FDSN_URL,
    FEED_BASE,
    UsgsSource,
    feed_coverage,
    parse_collection,
    parse_detail,
    parse_feature,
    steady_feed,
)
from earthwave.state.events import (
    QuakeMajor,
    QuakeNearHome,
    QuakeNew,
    QuakeUpdated,
    SourceDegraded,
    SourceRecovered,
)
from earthwave.state.store import SortKey, Store, ViewFilter, changed_fields

# --- usgs parsing -----------------------------------------------------------------------------------------------


def test_parse_all_hour_feature_fields():
    data = load_json("usgs/all_hour.geojson")
    quakes = parse_collection(data)
    assert len(quakes) == data["metadata"]["count"]
    first = quakes[0]
    assert first.id == "nc75460277"
    assert (first.lon, first.lat) == (pytest.approx(-122.8045), pytest.approx(38.8248))
    assert first.depth_km == pytest.approx(1.48, abs=0.01)
    assert first.time.tzinfo is not None and first.time.year == 2026
    assert first.updated > first.time
    assert first.mag_type == "md" and first.status == "automatic" and not first.tsunami_flag
    assert first.also_ids == ()


def test_parse_week_feed_has_reviewed_and_alerts():
    quakes = parse_collection(load_json("usgs/4.5_week.geojson"))
    assert len(quakes) == 151
    assert {q.status for q in quakes} >= {"reviewed"}
    assert all(q.mag >= 4.4 for q in quakes)


def test_parse_skips_blasts_null_mags_and_bad_features():
    base = load_json("usgs/all_hour.geojson")["features"][0]
    blast = {**base, "id": "b1", "properties": {**base["properties"], "type": "quarry blast"}}
    nomag = {**base, "id": "b2", "properties": {**base["properties"], "mag": None}}
    broken = {"id": "b3", "properties": {}}
    assert parse_feature(blast) is None and parse_feature(nomag) is None
    assert [q.id for q in parse_collection({"features": [blast, nomag, broken, base]})] == ["nc75460277"]


def test_also_ids_exclude_the_canonical_id():
    feats = load_json("usgs/significant_month.geojson")["features"]
    multi = next(f for f in feats if f["properties"]["ids"].count(",") > 2)
    quake = parse_feature(multi)
    assert multi["id"] not in quake.also_ids and quake.also_ids


def test_parse_detail_losspager_and_shakemap():
    detail = parse_detail(load_json("usgs/detail.geojson"))
    assert detail.id == "us7000ttxz"
    assert detail.pager_alert == "green"
    assert detail.pager_max_mmi == 0.0
    assert detail.shakemap_max_mmi == pytest.approx(7.814)  # 'nan' maxmmi falls back to the grid value
    assert detail.shakemap_max_pga == pytest.approx(0.544)
    assert "shakemap" in detail.products and detail.dyfi_max_mmi is None


def test_steady_feed_and_coverage():
    assert steady_feed(2.5) == "2.5_day" and steady_feed(0.0) == "all_day" and steady_feed(1.0) == "1.0_day"
    assert steady_feed(3.1) == "2.5_day" and steady_feed(6.0) == "4.5_day"
    assert feed_coverage("4.5_week") == (168.0, 4.5)
    assert feed_coverage("all_day") == (24.0, -10.0)
    assert feed_coverage("significant_month") is None


# --- usgs fetching (respx) --------------------------------------------------------------------------------------


@respx.mock
async def test_fetch_chain_then_conditional_request_and_304(clock):
    week = FEED_BASE + "4.5_week.geojson"
    day = FEED_BASE + "all_day.geojson"
    sig = FEED_BASE + "significant_month.geojson"
    steady = FEED_BASE + "2.5_day.geojson"
    body = (FIXTURES / "usgs" / "all_hour.geojson").read_bytes()
    respx.get(week).mock(return_value=httpx.Response(200, content=body))
    respx.get(day).mock(return_value=httpx.Response(200, content=body))
    respx.get(sig).mock(return_value=httpx.Response(200, content=body))
    route = respx.get(steady).mock(
        side_effect=[
            httpx.Response(200, content=body, headers={"Last-Modified": "Sat, 31 Oct 2026 12:00:00 GMT"}),
            httpx.Response(304),
        ]
    )
    source = UsgsSource(clock)
    async with build_client() as client:
        batches = []
        cursor = None
        for _ in range(5):
            assert source.interval(SourceHealth()) == (0.0 if source.starting else 60.0)
            batch = await source.fetch(client, cursor)
            cursor = batch.cursor or cursor
            batches.append(batch)
    assert [b.backfill for b in batches] == [True, True, True, False, False]
    assert [b.meta.get("feed") for b in batches[:4]] == ["4.5_week", "all_day", "significant_month", "2.5_day"]
    assert batches[0].coverage == (168.0, 4.5) and batches[3].coverage == (24.0, 2.5)
    assert batches[4].not_modified and batches[4].cursor == cursor
    assert "If-Modified-Since" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["If-Modified-Since"] == "Sat, 31 Oct 2026 12:00:00 GMT"


@respx.mock
async def test_changed_steady_feed_ignores_old_cursor(clock):
    source = UsgsSource(clock)
    source._startup.clear()
    source.set_min_magnitude(1.0)
    route = respx.get(FEED_BASE + "1.0_day.geojson").mock(return_value=httpx.Response(200, json={"features": []}))
    async with build_client() as client:
        await source.fetch(client, "2.5_day|Sat, 31 Oct 2026 12:00:00 GMT")
    assert "If-Modified-Since" not in route.calls[0].request.headers


@respx.mock
async def test_http_error_raises(clock):
    respx.get(FEED_BASE + "4.5_week.geojson").mock(return_value=httpx.Response(503))
    async with build_client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await UsgsSource(clock).fetch(client, None)


@respx.mock
async def test_fetch_range_builds_fdsnws_query(clock):
    route = respx.get(FDSN_URL).mock(
        return_value=httpx.Response(200, content=(FIXTURES / "usgs" / "all_hour.geojson").read_bytes())
    )
    async with build_client() as client:
        batch = await UsgsSource(clock).fetch_range(client, T0 - timedelta(days=7), T0, 1.0)
    params = dict(route.calls[0].request.url.params)
    assert (
        params["minmagnitude"] == "1" and params["starttime"] == "2026-10-24T12:00:00" and params["orderby"] == "time"
    )
    assert batch.backfill and batch.coverage == (168.0, 1.0) and len(batch.quakes) == 12


@respx.mock
async def test_detail_is_cached_and_lru_bounded(clock):
    route = respx.get(url__regex=r".*/detail/.*").mock(
        return_value=httpx.Response(200, json=load_json("usgs/detail.geojson"))
    )
    source = UsgsSource(clock)
    quake = make_quake(
        "us7000ttxz", 6.4, detail_url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/us7000ttxz.geojson"
    )
    async with build_client() as client:
        a = await source.fetch_detail(client, quake)
        b = await source.fetch_detail(client, quake)
        assert a is b and route.call_count == 1
        for i in range(70):
            await source.fetch_detail(
                client, make_quake(f"x{i}", 3.0, detail_url=f"https://example.test/detail/x{i}.geojson")
            )
    assert len(source._details) == 64


@respx.mock
async def test_detail_adds_nearby_cities(clock):
    data = load_json("usgs/detail.geojson")
    data["properties"]["products"]["nearby-cities"] = [
        {"contents": {"nearby-cities.json": {"url": "https://example.test/nc.json"}}}
    ]
    respx.get(url__regex=r".*/detail/.*").mock(return_value=httpx.Response(200, json=data))
    respx.get("https://example.test/nc.json").mock(
        return_value=httpx.Response(200, json=[{"distance": 12, "direction": "NE", "name": "Town", "population": 1}])
    )
    async with build_client() as client:
        detail = await UsgsSource(clock).fetch_detail(client, make_quake("us7000ttxz", 6.4))
    assert detail.nearby_cities == ("12 km NE of Town",)


# --- store ------------------------------------------------------------------------------------------------------


def batch(*quakes, backfill=False, coverage=None, not_modified=False) -> Batch:
    return Batch("usgs", T0, quakes=tuple(quakes), backfill=backfill, coverage=coverage, not_modified=not_modified)


def test_material_update_rules():
    base = make_quake(mag=4.0, depth_km=10.0)
    assert changed_fields(base, make_quake(mag=4.1)) == ()
    assert changed_fields(base, make_quake(mag=4.2)) == ("mag",)
    assert changed_fields(base, make_quake(mag=3.8, depth_km=20.0)) == ("mag", "depth")
    assert changed_fields(base, make_quake(depth_km=19.9)) == ()
    assert changed_fields(base, make_quake(status="reviewed")) == ("status",)
    assert changed_fields(make_quake(status="reviewed"), make_quake(status="automatic")) == ()
    assert changed_fields(base, make_quake(tsunami_flag=True, pager_alert="red", place="elsewhere")) == (
        "tsunami",
        "pager",
        "place",
    )


def test_cold_store_is_silent_then_warm_diffs_raise_events(clock):
    store = Store()
    assert store.merge(batch(make_quake("a"), backfill=True), clock) == []
    assert not store.warm
    assert store.merge(batch(make_quake("a"), make_quake("b")), clock) == []  # first steady batch: still silent
    assert store.warm
    events = store.merge(batch(make_quake("a"), make_quake("b"), make_quake("c")), clock)
    assert [type(e) for e in events] == [QuakeNew] and events[0].quake.id == "c"


def test_update_events_and_stale_updates_ignored(clock):
    store = Store()
    store.merge(batch(make_quake("a", mag=4.0)), clock)
    events = store.merge(batch(make_quake("a", mag=4.5, updated=T0 + timedelta(minutes=1))), clock)
    assert len(events) == 1 and isinstance(events[0], QuakeUpdated) and events[0].changed == ("mag",)
    assert store.merge(batch(make_quake("a", mag=3.0, updated=T0)), clock) == []  # older revision
    assert store.get("a").mag == 4.5
    store.merge(
        batch(make_quake("a", mag=4.55, updated=T0 + timedelta(minutes=2))), clock
    )  # not material, still stored
    assert store.get("a").mag == 4.55


def test_major_and_near_home_events(clock):
    home = HomeConfig("Home", 10.0, 20.0, radius_km=300)
    store = Store(AlertsConfig(major_magnitude=6.0, near_home_magnitude=3.0), home)
    store.merge(batch(make_quake("seed", 2.0)), clock)
    near = make_quake("near", 3.5, lat=10.5, lon=20.5)
    far = make_quake("far", 6.5, lat=-40.0, lon=100.0)
    events = store.merge(batch(near, far), clock)
    kinds = [type(e) for e in events]
    assert kinds == [QuakeNew, QuakeNearHome, QuakeNew, QuakeMajor]
    # crossing the major threshold on update fires once
    grow = make_quake("seed", 6.1, updated=T0 + timedelta(minutes=1))
    events = store.merge(batch(grow), clock)
    assert [type(e) for e in events] == [QuakeUpdated, QuakeMajor, QuakeNearHome]
    assert (
        store.merge(batch(make_quake("seed", 6.9, updated=T0 + timedelta(minutes=2))), clock)[-1].__class__
        is QuakeUpdated
    )


def test_not_modified_changes_nothing(clock):
    store = Store()
    store.merge(batch(make_quake("a")), clock)
    assert store.merge(batch(not_modified=True), clock) == []
    assert len(store) == 1


def test_visible_filters_sorts_and_searches(clock):
    store = Store(home=HomeConfig("H", 0.0, 0.0, 500))
    quakes = [
        make_quake("old", 5.0, minutes_ago=60 * 30, place="Old Town"),
        make_quake("small", 1.0, minutes_ago=1, place="Tiny Village"),
        make_quake("mid", 4.0, minutes_ago=10, lat=1.0, lon=1.0, place="Mid City"),
        make_quake("big", 6.0, minutes_ago=30, lat=50.0, lon=50.0, place="Big Bay"),
    ]
    store.merge(batch(*quakes), clock)
    view = ViewFilter(2.5, 24)
    assert [q.id for q in store.visible(view, clock)] == ["mid", "big"]
    assert [q.id for q in store.visible(view, clock, SortKey.MAG)] == ["big", "mid"]
    assert [q.id for q in store.visible(view, clock, SortKey.DIST)] == ["mid", "big"]
    assert [q.id for q in store.visible(ViewFilter(0, 48), clock)] == ["small", "mid", "big", "old"]
    assert [q.id for q in store.visible(ViewFilter(0, 48, "BAY"), clock)] == ["big"]
    clock.advance(3600 * 5)
    assert [q.id for q in store.visible(ViewFilter(0, 1), clock)] == []


def test_sort_key_cycle_skips_distance_without_home():
    assert SortKey.TIME.next(False) is SortKey.MAG and SortKey.MAG.next(False) is SortKey.TIME
    assert SortKey.MAG.next(True) is SortKey.DIST and SortKey.DIST.next(True) is SortKey.TIME


def test_coverage_tracking_and_pruning(clock):
    store = Store()
    store.merge(batch(make_quake("a"), backfill=True, coverage=(168.0, 4.5)), clock)
    assert store.is_covered(168, 4.5) and store.is_covered(24, 5.0)
    assert not store.is_covered(169, 4.5) and not store.is_covered(24, 2.5)
    store.merge(batch(make_quake("ancient", 5.0, minutes_ago=60 * 24 * 40), backfill=True), clock)
    assert store.get("ancient") is None


# --- attention --------------------------------------------------------------------------------------------------


def test_new_row_flashes_three_times_then_dims_until_seen(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeNew(make_quake("a"))])
    seen = []
    for _ in range(8):  # 0, 0.5, 1.0 ... 3.5 seconds
        seen.append(tracker.style_for("a"))
        clock.advance(0.5)
    assert seen[:6] == [FLASH_NEW, None, FLASH_NEW, None, FLASH_NEW, None]
    assert seen[6] == DIM_NEW and seen[7] == DIM_NEW
    assert tracker.unseen() == 1
    tracker.ack("a")
    assert tracker.style_for("a") is None and tracker.unseen() == 0
    tracker.prune()
    assert tracker.kind_of("a") is None


def test_updated_flashes_once_and_is_not_unseen(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeUpdated(make_quake("a"), ("mag",))])
    assert tracker.style_for("a") == FLASH_UPDATED and tracker.unseen() == 0
    clock.advance(0.6)
    assert tracker.style_for("a") is None
    clock.advance(0.5)
    tracker.prune()
    assert tracker.kind_of("a") is None


def test_major_outranks_new_and_pulses_for_a_minute(clock):
    tracker = AttentionTracker(clock)
    quake = make_quake("a", 7.0)
    tracker.observe([QuakeNew(quake), QuakeMajor(quake)])
    assert tracker.kind_of("a") == "major" and tracker.style_for("a") == PULSE_MAJOR
    clock.advance(0.5)
    assert tracker.style_for("a") != PULSE_MAJOR
    clock.advance(60)
    assert tracker.style_for("a") is not None  # unseen major keeps a dim highlight after the pulse
    tracker.ack_all()
    assert tracker.style_for("a") is None
    tracker.observe([QuakeUpdated(quake, ("mag",))])  # lower rank than an unfinished major does not replace it
    assert tracker.unseen() == 0


# --- scheduler --------------------------------------------------------------------------------------------------


class FakeSource:
    name = "fake"
    min_interval = 60.0

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def interval(self, health):
        return 60.0

    async def fetch(self, client, since):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_backoff_doubles_with_jitter_and_caps():
    rng = random.Random(1)
    delays = [backoff_delay(60, n, rng) for n in (1, 2, 3, 4, 5, 9)]
    for delay, centre in zip(delays, (120, 240, 480, 600, 600, 600), strict=True):
        assert centre * 0.9 <= delay <= centre * 1.1
    assert 27 <= backoff_delay(0, 0, rng) <= 33
    for failures in (1023, 1024, 5000):  # 2**1024 does not fit a float
        assert 540 <= backoff_delay(60.0, failures, rng) <= 660


def test_freshness_states(clock):
    health = SourceHealth()
    assert freshness_of(health, clock.now(), 60) is Freshness.LOADING
    health.failures = 1
    assert freshness_of(health, clock.now(), 60) is Freshness.ERROR
    health.failures, health.last_ok = 0, T0
    assert freshness_of(health, T0 + timedelta(seconds=149), 60) is Freshness.FRESH
    assert freshness_of(health, T0 + timedelta(seconds=151), 60) is Freshness.STALE
    health.failures = 2
    assert freshness_of(health, T0 + timedelta(seconds=100), 60) is Freshness.ERROR
    assert freshness_of(health, T0 + timedelta(seconds=151), 60) is Freshness.STALE


async def test_failure_backoff_recovery_and_isolation(clock):
    good = Batch("fake", T0, quakes=(make_quake("a"),), cursor="c1")
    flaky = FakeSource([httpx.ConnectError("down"), httpx.ConnectError("down"), good])
    steady = FakeSource([good, good, good])
    steady.name = "steady"
    results = []
    sched = Scheduler([flaky, steady], Store(), clock, results.append, rng=random.Random(3))
    async with build_client() as client:
        assert sorted(sched.due()) == ["fake", "steady"]
        await sched.poll_source("fake", client)
        await sched.poll_source("steady", client)
        assert [type(e) for e in results[0].events] == [SourceDegraded] and results[0].error
        assert sched.freshness("fake") is Freshness.ERROR and sched.freshness("steady") is Freshness.FRESH
        delay1 = sched.statuses["fake"].next_due
        assert 108 <= delay1 <= 132  # 2 x 60 s +-10%
        assert sched.due() == []
        clock.advance(delay1)
        await sched.poll_source("fake", client)
        assert [type(e) for e in results[2].events] == []  # second failure raises no second degraded event
        assert 216 <= sched.statuses["fake"].next_due - clock.monotonic() <= 264
        clock.advance(300)
        assert sched.freshness("fake") is Freshness.ERROR
        clock.advance(2000)
        await sched.poll_source("fake", client)
    assert [type(e) for e in results[-1].events] == [SourceRecovered]
    assert sched.freshness("fake") is Freshness.FRESH and sched.age("fake") == 0
    assert sched.statuses["fake"].health.failures == 0


async def test_merge_crash_is_contained(clock):
    source = FakeSource([Batch("fake", T0)])
    store = Store()

    def boom(*a, **k):
        raise RuntimeError("bad batch")

    store.merge = boom
    results = []
    sched = Scheduler([source], store, clock, results.append)
    async with build_client() as client:
        result = await sched.poll_source("fake", client)
    assert result.error and "bad batch" in result.error


async def test_stale_after_missed_polls(clock):
    sched = Scheduler([FakeSource([Batch("fake", T0)])], Store(), clock, lambda r: None)
    async with build_client() as client:
        await sched.poll_source("fake", client)
    clock.advance(149)
    assert sched.freshness("fake") is Freshness.FRESH
    clock.advance(2)
    assert sched.freshness("fake") is Freshness.STALE


async def test_refresh_now_respects_the_minimum_interval(clock):
    source = FakeSource([Batch("fake", T0), Batch("fake", T0)])
    sched = Scheduler([source], Store(), clock, lambda r: None)
    async with build_client() as client:
        await sched.poll_source("fake", client)
    clock.advance(10)
    sched.refresh_now()
    assert sched.due() == [] and 49 <= sched.next_poll_in("fake") <= 51
    clock.advance(50)
    assert sched.due() == ["fake"]


async def test_old_generation_backfill_is_dropped(clock):
    store = Store()
    results = []
    sched = Scheduler([], store, clock, results.append)
    gate = {}

    async def job(client):
        await gate["go"].wait()
        return Batch("usgs", T0, quakes=(make_quake("late"),), backfill=True)

    import asyncio

    gate["go"] = asyncio.Event()
    stale = sched.submit_backfill("usgs", None, job)
    sched.switch()
    fresh = sched.submit_backfill("usgs", None, job)
    gate["go"].set()
    await asyncio.gather(stale, fresh)
    assert len(results) == 1 and results[0].generation == 1
    assert store.get("late") is not None


async def test_backfill_failure_is_reported_not_raised(clock):
    results = []
    sched = Scheduler([], Store(), clock, results.append)

    async def job(client):
        raise httpx.ReadTimeout("slow")

    await sched.submit_backfill("usgs", None, job)
    assert results[0].error and "ReadTimeout" in results[0].error


async def test_replayed_session_drives_scheduler_end_to_end():
    from earthwave.clock import ReplayClock

    path = FIXTURES / "replay" / "usgs_short.jsonl"
    clock = ReplayClock(recording_start(path), real=lambda: 0.0)
    source = UsgsSource(clock)
    store = Store(AlertsConfig(major_magnitude=6.0))
    results = []
    sched = Scheduler([source], store, clock, results.append)
    async with build_client(transport=ReplayTransport(path, clock)) as client:
        for _ in range(6):
            clock.advance(0)
            await sched.poll_source("usgs", client)
            assert not results[-1].error, results[-1].error
    assert [bool(r.batch.not_modified) for r in results] == [False, False, False, False, True, False]
    assert all(not r.events for r in results[:4])  # backfill and the first steady batch are silent
    assert store.warm and store.get("replay_new1") is not None
    last = results[-1].events
    assert {type(e) for e in last} == {QuakeNew, QuakeMajor, QuakeUpdated}
    assert clock.now() >= recording_start(path) + timedelta(seconds=180)


# --- review fixes -----------------------------------------------------------------------------------------------


def test_threshold_crossing_without_a_material_change_still_fires(clock):
    store = Store(AlertsConfig(major_magnitude=6.0), HomeConfig())
    store.merge(batch(make_quake("a", 5.9)), clock)
    events = store.merge(batch(make_quake("a", 6.0, updated=T0 + timedelta(minutes=1))), clock)
    assert [type(e) for e in events] == [QuakeMajor]  # 0.1 is not a material change, the threshold still counts
    assert store.merge(batch(make_quake("a", 6.1, updated=T0 + timedelta(minutes=2))), clock) == []


def test_tracker_retain_drops_attention_for_quakes_the_store_no_longer_holds(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeNew(make_quake("in")), QuakeNew(make_quake("out"))])
    tracker.retain({"in"})
    assert tracker.unseen() == 1 and tracker.kind_of("out") is None


@respx.mock
async def test_startup_feed_is_retried_until_it_succeeds(clock):
    first = respx.get(FEED_BASE + "4.5_week.geojson").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"features": []})]
    )
    respx.get(FEED_BASE + "all_day.geojson").mock(return_value=httpx.Response(200, json={"features": []}))
    respx.get(FEED_BASE + "significant_month.geojson").mock(return_value=httpx.Response(200, json={"features": []}))
    source = UsgsSource(clock)
    async with build_client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch(client, None)
        assert source.starting
        feeds = [(await source.fetch(client, None)).meta["feed"] for _ in range(3)]
    assert first.call_count == 2 and feeds == ["4.5_week", "all_day", "significant_month"]
    assert not source.starting


@respx.mock
async def test_a_startup_feed_that_keeps_failing_is_skipped_so_the_steady_feed_runs(clock):
    broken = respx.get(FEED_BASE + "4.5_week.geojson").mock(return_value=httpx.Response(500))
    respx.get(FEED_BASE + "all_day.geojson").mock(return_value=httpx.Response(200, json={"features": []}))
    respx.get(FEED_BASE + "significant_month.geojson").mock(return_value=httpx.Response(500))
    respx.get(FEED_BASE + "2.5_day.geojson").mock(return_value=httpx.Response(200, json={"features": []}))
    source = UsgsSource(clock)
    async with build_client() as client:
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await source.fetch(client, None)
        assert broken.call_count == 2 and source.starting
        assert (await source.fetch(client, None)).meta["feed"] == "all_day"
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await source.fetch(client, None)
        assert not source.starting
        assert (await source.fetch(client, None)).meta["feed"] == "2.5_day"


def test_the_steady_feed_also_covers_quakes_that_can_alert():
    assert UsgsSource(None, min_magnitude=5.0).steady == "4.5_day"
    assert UsgsSource(None, min_magnitude=5.0, alert_floor=3.0).steady == "2.5_day"
    source = UsgsSource(None, min_magnitude=1.0, alert_floor=3.0)
    assert source.steady == "1.0_day"
    source.set_min_magnitude(6.0)
    assert source.steady == "2.5_day"


async def test_attention_survives_a_search_that_hides_the_quake(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeNew(make_quake("hidden"))])
    tracker.retain({"hidden"})  # the store still holds it, whatever the view shows
    assert tracker.unseen() == 1


@respx.mock
async def test_first_batch_of_a_new_feed_is_silent_then_normal(clock):
    source = UsgsSource(clock)
    source._startup.clear()
    respx.get(FEED_BASE + "2.5_day.geojson").mock(
        return_value=httpx.Response(200, json={"features": []}, headers={"Last-Modified": "x"})
    )
    async with build_client() as client:
        first = await source.fetch(client, None)
        again = await source.fetch(client, first.cursor)
    assert first.meta["silent"] and not again.meta["silent"]
    store = Store()
    store.merge(batch(make_quake("seed")), clock)
    loud = Batch("usgs", T0, quakes=(make_quake("n1"),), meta={"silent": False})
    quiet = Batch("usgs", T0, quakes=(make_quake("n2"),), meta={"silent": True})
    assert [type(e) for e in store.merge(loud, clock)] == [QuakeNew]
    assert store.merge(quiet, clock) == [] and store.get("n2") is not None


def test_coverage_below_the_narrowed_feed_is_forgotten(clock):
    store = Store()
    store.merge(Batch("usgs", T0, quakes=(), coverage=(24.0, 0.0)), clock)
    store.merge(Batch("usgs", T0, quakes=(), coverage=(24.0, 4.5)), clock)
    assert store.is_covered(24, 1.0)
    store.drop_coverage_below(2.5)
    assert not store.is_covered(24, 1.0) and store.is_covered(24, 4.5)


class SlowSource(FakeSource):
    name = "slow"

    def __init__(self, release):
        super().__init__([])
        self.release = release

    async def fetch(self, client, since):
        self.calls += 1
        await self.release.wait()
        return Batch("slow", T0)


class TickSource(FakeSource):
    name = "tick"
    min_interval = 0.0

    def interval(self, health):
        return 0.05

    async def fetch(self, client, since):
        self.calls += 1
        return Batch("tick", T0)


async def test_slow_source_does_not_delay_a_fast_one():
    from test_observations import drive

    from earthwave.clock import ReplayClock

    release = asyncio.Event()
    slow, fast = SlowSource(release), TickSource([])
    # moves only with `drive`, so the 0.05 s interval is not a wall-clock race
    clock = ReplayClock(T0, real=lambda: 0.0)
    sched = Scheduler([slow, fast], Store(), clock, lambda result: None)
    async with build_client() as client:
        task = asyncio.create_task(sched.run(client))
        await drive(clock, lambda: fast.calls >= 3, step=1.0)
        assert fast.calls >= 3 and slow.calls == 1  # the fast source kept polling while the slow one hung
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
