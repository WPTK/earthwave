"""Store regressions found by the chaos audit: future timestamps and unstable tie order (#286, #287, #295)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import T0, make_quake

from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch, GaugeReading, Station
from earthwave.state.store import SortKey, Store, ViewFilter

HOME = (40.7, -74.0)
SORTS = [SortKey.TIME, SortKey.MAG, SortKey.DIST]


def gauge_batch(readings, clock, station="D1"):
    return Batch("dart", clock.now(), stations=(Station(station, "dart", "x", 0.0, 0.0),), readings=tuple(readings))


def reading(ts, value=1.0, station="D1"):
    return GaugeReading(station, "dart", ts, value, "15s")


def test_future_gauge_timestamp_does_not_erase_history(clock):
    store = Store()
    good = [reading(T0 - timedelta(minutes=15 * i)) for i in range(20)]
    store.merge(gauge_batch(good, clock), clock)
    assert len(store.gauge_readings("D1")) == 20
    store.merge(gauge_batch([reading(T0 + timedelta(days=3650))], clock), clock)
    assert len(store.gauge_readings("D1")) >= 20, "history deleted by a single reading from the year 2036"


def test_future_updated_does_not_freeze_a_quake(clock):
    store = Store()
    q = make_quake("a", 4.0)
    store.merge(Batch("usgs", T0, quakes=(replace(q, updated=T0 + timedelta(days=36500)),), backfill=True), clock)
    store.merge(Batch("usgs", T0, quakes=(replace(q, mag=6.0, updated=T0),), backfill=True), clock)
    assert store.get("a").mag == 6.0


def _clock():
    return ReplayClock(T0, real=lambda: 0.0)


def tie_store(order: list[int], count: int = 6) -> Store:
    """`count` quakes sharing time, magnitude and epicentre (a swarm), merged in the given arrival order."""
    config = with_overrides(Config(), 2.5, 24, HOME, "unicode")
    store = Store(config.alerts, config.home)
    quakes = [make_quake(f"sw{i}", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0) for i in range(count)]
    for i in order:
        store.merge(Batch("usgs", T0, quakes=(quakes[i],)), _clock())
    return store


@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_equal_key_rows_keep_their_order_across_repeated_unchanged_polls(sort):
    store = tie_store(list(range(6)))
    clock = _clock()
    view = ViewFilter(2.5, 24)
    first = [q.id for q in store.visible(view, clock, sort)]
    reversed_batch = tuple(
        reversed([make_quake(f"sw{i}", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0) for i in range(6)])
    )
    for _ in range(10):
        store.merge(Batch("usgs", T0, quakes=reversed_batch), clock)
        assert [q.id for q in store.visible(view, clock, sort)] == first


@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_a_revised_quake_does_not_jump_among_its_equals(sort):
    store = tie_store(list(range(6)))
    clock = _clock()
    view = ViewFilter(2.5, 24)
    first = [q.id for q in store.visible(view, clock, sort)]
    revised = make_quake("sw2", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0, updated=T0, place="revised")
    store.merge(Batch("usgs", T0 + timedelta(seconds=1), quakes=(revised,)), clock)
    assert [q.id for q in store.visible(view, clock, sort)] == first


@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_equal_key_order_does_not_depend_on_arrival_order(sort):
    clock = _clock()
    view = ViewFilter(2.5, 24)
    a = tie_store([0, 1, 2, 3, 4, 5])
    b = tie_store([5, 3, 1, 0, 4, 2])
    assert [q.id for q in a.visible(view, clock, sort)] == [q.id for q in b.visible(view, clock, sort)]
