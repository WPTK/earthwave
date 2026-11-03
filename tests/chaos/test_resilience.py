"""Chaos phase 1b: HTTP fault injection, long-run soak and random-key Pilot probes. Read-only on product code.

A test marked `xfail(strict=True, reason="R-n ...")` is a finding: it asserts the behaviour we want, fails today, and
flips to an error (XPASS strict) the day the product is fixed, so the marker must then be removed. The reasons map to
tests/chaos/FINDINGS_RESILIENCE.md.
"""

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, T0, load_json
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import earthwave.poll as pollmod
from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import MAX_VIEW_HOURS, Config, with_overrides
from earthwave.models import Batch, GaugeReading, Station
from earthwave.net import CACHE_ENTRIES, RETRY_AFTER_CAP, build_client
from earthwave.poll import BACKOFF_CAP, Scheduler
from earthwave.sources import usgs
from earthwave.sources.usgs import DETAIL_CACHE_SIZE, UsgsSource
from earthwave.state.events import SourceRecovered
from earthwave.state.store import GAUGE_MAX_ROWS, RETAIN, Store

HOUR = (FIXTURES / "usgs" / "all_hour.geojson").read_bytes()
DETAIL = (FIXTURES / "usgs" / "detail.geojson").read_bytes()
GOOD_COUNT = len(json.loads(HOUR)["features"])


# --- helpers ----------------------------------------------------------------------------------------------------


class Rig:
    """One UsgsSource + Scheduler + Store behind a MockTransport whose behaviour the test switches at will."""

    def __init__(self, clock: ReplayClock, seed: int = 1):
        self.clock = clock
        self.respond = lambda request: httpx.Response(200, content=HOUR)
        self.requests: list[httpx.Request] = []
        self.results: list[pollmod.PollResult] = []
        self.store = Store()
        self.source = UsgsSource(clock)
        self.sched = Scheduler([self.source], self.store, clock, self.results.append, rng=random.Random(seed))
        self.client = build_client(transport=httpx.MockTransport(self._handler), clock=clock)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        out = self.respond(request)
        if isinstance(out, BaseException):
            raise out
        return out

    async def poll(self) -> pollmod.PollResult:
        return await self.sched.poll_source("usgs", self.client)

    async def warm(self) -> None:
        """Three good polls: the startup chain is over and the store holds last-good data."""
        for _ in range(3):
            assert (await self.poll()).error is None
        assert len(self.store) > 0
        assert not self.source.starting

    @property
    def status(self) -> pollmod.SourceStatus:
        return self.sched.statuses["usgs"]

    def wait_left(self) -> float:
        return self.status.next_due - self.clock.monotonic()


def resp(status=200, body=b"", headers=None) -> httpx.Response:
    return httpx.Response(status, content=body, headers=headers or {})


@pytest.fixture
async def rig(clock):
    r = Rig(clock)
    yield r
    await r.client.aclose()


# --- 1. HTTP faults: the poll loop survives, backs off, keeps last good data -------------------------------------

HTTP_FAULTS = {
    "429-retry-120": (lambda r: resp(429, headers={"Retry-After": "120"}), 120.0),
    "429-no-header": (lambda r: resp(429), 60.0),
    "429-retry-huge": (lambda r: resp(429, headers={"Retry-After": "86400"}), RETRY_AFTER_CAP),
    "429-retry-date-far-future": (lambda r: resp(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}), 0),
    "429-retry-garbage": (lambda r: resp(429, headers={"Retry-After": "soon"}), 60.0),
    "429-retry-negative": (lambda r: resp(429, headers={"Retry-After": "-5"}), 0),
    "429-retry-nan": (lambda r: resp(429, headers={"Retry-After": "nan"}), 0),
    "429-retry-inf": (lambda r: resp(429, headers={"Retry-After": "inf"}), RETRY_AFTER_CAP),
    "429-retry-1e400": (lambda r: resp(429, headers={"Retry-After": "1e400"}), RETRY_AFTER_CAP),
    "502": (lambda r: resp(502, b"<html>Bad Gateway</html>"), 0),
    "503": (lambda r: resp(503), 0),
    "503-retry-90": (lambda r: resp(503, headers={"Retry-After": "90"}), 90.0),
    "500": (lambda r: resp(500, b"oops"), 0),
    "404": (lambda r: resp(404), 0),
    "204-empty": (lambda r: resp(204), 0),
    "200-empty": (lambda r: resp(200, b""), 0),
    "200-html": (lambda r: resp(200, b"<html>captive portal</html>"), 0),
    "200-truncated-json": (lambda r: resp(200, HOUR[: len(HOUR) // 2]), 0),
    "200-null": (lambda r: resp(200, b"null"), 0),
    "200-list": (lambda r: resp(200, b"[]"), 0),
    "200-features-null": (lambda r: resp(200, b'{"type":"FeatureCollection","features":null}'), 0),
    "200-binary": (lambda r: resp(200, bytes(range(256))), 0),
    "200-bad-utf8": (lambda r: resp(200, b'{"features":[]}\xff\xfe'), 0),
    "read-timeout": (lambda r: httpx.ReadTimeout("slow"), 0),
    "connect-timeout": (lambda r: httpx.ConnectTimeout("slow"), 0),
    "connect-error": (lambda r: httpx.ConnectError("refused"), 0),
    "remote-protocol": (lambda r: httpx.RemoteProtocolError("peer closed"), 0),
    "decoding-error": (lambda r: httpx.DecodingError("bad gzip"), 0),
}


@pytest.mark.parametrize("name", HTTP_FAULTS)
async def test_fault_poll_survives_backs_off_and_keeps_data(rig, name):
    respond, min_wait = HTTP_FAULTS[name]
    await rig.warm()
    held = {q.id for q in rig.store.all()}
    rig.respond = respond
    result = await rig.poll()  # must not raise
    assert result.error, f"{name}: a bad answer was accepted as a good poll"
    assert {q.id for q in rig.store.all()} == held, "store lost or gained quakes on a failed poll"
    assert not rig.status.in_flight
    assert rig.status.health.failures == 1
    assert rig.wait_left() >= min(min_wait, RETRY_AFTER_CAP) * 0.99
    assert rig.wait_left() <= BACKOFF_CAP * 1.11 + 1, "back-off beyond the cap"
    rig.respond = lambda r: resp(200, HOUR)  # recovery
    rig.clock.advance(1000)
    ok = await rig.poll()
    assert ok.error is None and rig.status.health.failures == 0
    assert any(isinstance(e, SourceRecovered) for e in ok.events)


async def test_backoff_grows_and_is_capped_over_a_long_outage(rig):
    await rig.warm()
    rig.respond = lambda r: resp(502)
    waits = []
    for _ in range(40):
        rig.clock.advance(1000)  # past any back-off, so the request reaches the (failing) origin
        assert (await rig.poll()).error
        waits.append(rig.wait_left())
    assert waits[1] > waits[0] * 1.5
    assert max(waits) <= BACKOFF_CAP * 1.11
    assert min(waits[8:]) >= BACKOFF_CAP * 0.89


async def test_polite_transport_short_circuits_during_a_429_window(rig):
    await rig.warm()
    rig.respond = lambda r: resp(429, headers={"Retry-After": "200"})
    assert (await rig.poll()).error
    seen = len(rig.requests)
    rig.respond = lambda r: resp(200, HOUR)
    rig.clock.advance(50)  # still inside the 200 s window: the origin must not be asked
    assert (await rig.poll()).error
    assert len(rig.requests) == seen
    rig.clock.advance(300)
    assert (await rig.poll()).error is None


async def test_hanging_server_is_cut_by_the_deadline(rig, monkeypatch):
    await rig.warm()
    monkeypatch.setattr(pollmod, "FETCH_DEADLINE", 0.2)

    class Hang(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(3600)

    hung = build_client(transport=Hang(), clock=rig.clock)
    result = await asyncio.wait_for(rig.sched.poll_source("usgs", hung), 5)
    assert result.error and "no answer" in result.error
    assert not rig.status.in_flight and rig.status.health.failures == 1
    await hung.aclose()


async def test_startup_chain_skips_a_dead_feed_and_still_reaches_steady(rig):
    rig.respond = lambda r: resp(502)
    for _ in range(2 * len(usgs.STARTUP_FEEDS)):
        assert (await rig.poll()).error
        rig.clock.advance(1000)
    assert not rig.source.starting, "startup chain stuck on dead feeds"


async def test_startup_with_a_204_does_not_wedge(rig):
    rig.respond = lambda r: resp(204)
    for _ in range(2 * len(usgs.STARTUP_FEEDS)):
        assert (await rig.poll()).error
    assert not rig.source.starting


@pytest.mark.xfail(strict=True, reason="R-3 a 304 to a request with no validator is a good poll (usgs.py:fetch)")
async def test_304_to_an_unconditional_request_is_not_a_good_poll(rig):
    """A proxy answering 304 with no validator sent: the store is still empty and the poll must not count as ok."""
    rig.respond = lambda r: resp(304)
    result = await rig.poll()
    assert result.error or len(rig.store) > 0, "an empty store was marked fresh by a 304 nobody asked for"


@pytest.mark.xfail(strict=True, reason="R-1 200 without a feature list is a good, empty poll (usgs.py:fetch)")
@pytest.mark.parametrize("body", [b"{}", b'{"type":"FeatureCollection"}', b'{"error":"maintenance"}', b'{"features":{"a":1}}'])
async def test_200_without_features_is_a_failure(rig, body):
    await rig.warm()
    rig.respond = lambda r: resp(200, body)
    result = await rig.poll()
    assert result.error, "a 200 body with no `features` list was accepted and resets the failure count"


@pytest.mark.xfail(strict=True, reason="R-2 a 200 `{}` claims feed coverage for a window it never listed (usgs.py:fetch)")
async def test_200_without_features_claims_no_coverage(rig):
    rig.respond = lambda r: resp(200, b"{}")
    for _ in range(3):
        await rig.poll()
    assert not rig.store.is_covered(168, 4.5), "store believes it holds every M4.5+ of the week from an empty reply"


async def test_blank_feature_list_does_not_wipe_the_store(rig):
    """`{"features": []}` with no metadata (a proxy's blank reply): an empty listing proves nothing."""
    await rig.warm()
    held = len(rig.store)
    rig.respond = lambda r: resp(200, b'{"features":[]}')
    await rig.poll()
    assert len(rig.store) == held


# --- fdsnws backfill ---------------------------------------------------------------------------------------------


def backfill_job(rig):
    start = rig.clock.now() - timedelta(days=3)
    return lambda client: rig.source.fetch_range(client, start, rig.clock.now(), 4.5)


async def run_backfill(rig):
    task = rig.sched.submit_backfill("usgs", rig.client, backfill_job(rig))
    await task
    assert not rig.sched._tasks, "finished backfill task leaked"
    return rig.results[-1]


async def test_backfill_429_retry_after_is_honoured(rig):
    await rig.warm()
    rig.respond = lambda r: resp(429, headers={"Retry-After": "150"})
    result = await run_backfill(rig)
    assert result.error
    st = rig.status
    assert not st.backfill_ready if False else not rig.sched.backfill_ready("usgs")
    assert st.backfill_due - rig.clock.monotonic() >= 149
    assert st.partial and not st.backfill_blocked


@pytest.mark.parametrize("code", [400, 404])
async def test_backfill_4xx_blocks_for_good_until_view_change(rig, code):
    await rig.warm()
    rig.respond = lambda r: resp(code)
    await run_backfill(rig)
    assert rig.status.backfill_blocked
    rig.sched.switch()
    assert rig.sched.backfill_ready("usgs")


@pytest.mark.parametrize("name", ["502", "503", "204-empty", "200-html", "200-list", "200-features-null", "read-timeout"])
async def test_backfill_faults_never_touch_store_or_loop(rig, name):
    await rig.warm()
    held = len(rig.store)
    rig.respond = HTTP_FAULTS[name][0]
    result = await run_backfill(rig)
    assert result.error and len(rig.store) == held
    assert rig.status.backfill_failures == 1


async def test_backfill_errors_grow_backoff_and_cap(rig):
    await rig.warm()
    rig.respond = lambda r: resp(502)
    for _ in range(30):
        rig.clock.advance(1000)
        await run_backfill(rig)
    assert rig.status.backfill_due - rig.clock.monotonic() <= BACKOFF_CAP * 1.11


async def test_backfill_over_limit_claims_no_coverage(rig):
    await rig.warm()
    feat = json.loads(HOUR)["features"][0]
    body = json.dumps({"features": [feat] * usgs.FDSN_LIMIT}).encode()
    rig.respond = lambda r: resp(200, body)
    batch = await rig.source.fetch_range(rig.client, rig.clock.now() - timedelta(days=30), rig.clock.now(), 1.0)
    assert batch.coverage is None


# --- detail product ----------------------------------------------------------------------------------------------


def a_quake(rig):
    return sorted(rig.store.all(), key=lambda q: q.id)[0]


@pytest.mark.parametrize("name", ["502", "429-retry-120", "204-empty", "200-html", "200-null", "200-list", "read-timeout"])
async def test_detail_faults_raise_cleanly(rig, name):
    await rig.warm()
    rig.respond = HTTP_FAULTS[name][0]
    with pytest.raises(Exception) as caught:
        await rig.source.fetch_detail(rig.client, a_quake(rig))
    assert not isinstance(caught.value, (SystemExit, KeyboardInterrupt))
    assert len(rig.source._details) == 0, "a failed detail fetch was cached"


@pytest.mark.parametrize("body", [b'{"properties":null}', b'{"properties":{"products":null}}', b"{}"])
async def test_detail_with_null_sections_degrades_not_crashes(rig, body):
    """Odd but valid JSON: the detail screen shows `Detail unavailable: <ExceptionName>` (acceptable) or empty fields.
    Whatever happens must be a normal exception, never a hang or a cached poison entry."""
    await rig.warm()
    rig.respond = lambda r: resp(200, body)
    try:
        detail = await rig.source.fetch_detail(rig.client, a_quake(rig))
    except Exception:
        return
    assert detail is not None


def detail_with_cities(cities_body: bytes):
    data = json.loads(DETAIL)
    data["properties"]["products"]["nearby-cities"] = [
        {"contents": {"nearby-cities.json": {"url": "https://earthquake.usgs.gov/cities.json"}}}
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("cities.json"):
            return resp(200, cities_body)
        return resp(200, json.dumps(data).encode())

    return respond


@pytest.mark.xfail(strict=True, reason="R-4 a malformed nearby-cities list throws away the whole detail (usgs.py:fetch_detail)")
@pytest.mark.parametrize("cities", [b'{"a": 1}', b'["x", "y"]', b"[1, 2]", b"null", b'"text"'])
async def test_malformed_nearby_cities_keeps_the_rest_of_the_detail(rig, cities):
    await rig.warm()
    rig.respond = detail_with_cities(cities)
    detail = await rig.source.fetch_detail(rig.client, a_quake(rig))
    assert detail.products, "PAGER/ShakeMap products lost because the optional city list was odd"


async def test_nearby_cities_http_failure_degrades(rig):
    await rig.warm()
    base = detail_with_cities(b"")
    rig.respond = lambda r: resp(503) if r.url.path.endswith("cities.json") else base(r)
    detail = await rig.source.fetch_detail(rig.client, a_quake(rig))
    assert detail.products and detail.nearby_cities == ()


async def test_detail_cache_is_bounded(rig):
    await rig.warm()
    base = a_quake(rig)
    rig.respond = lambda r: resp(200, DETAIL)
    from dataclasses import replace

    for i in range(DETAIL_CACHE_SIZE * 3):
        await rig.source.fetch_detail(rig.client, replace(base, id=f"x{i}", detail_url=f"https://example.invalid/{i}"))
        assert len(rig.source._details) <= DETAIL_CACHE_SIZE


# --- poisoned timestamps: one bad row must not erase good data ------------------------------------------------


def gauge_batch(readings, clock, station="D1"):
    return Batch("dart", clock.now(), stations=(Station(station, "dart", "x", 0.0, 0.0),), readings=tuple(readings))


def reading(ts, value=1.0, station="D1"):
    return GaugeReading(station, "dart", ts, value, "15s")


@pytest.mark.xfail(strict=True, reason="R-5 one future-dated gauge reading prunes the station's real history (store.py:_merge_gauges)")
def test_future_gauge_timestamp_does_not_erase_history(clock):
    store = Store()
    good = [reading(T0 - timedelta(minutes=15 * i)) for i in range(20)]
    store.merge(gauge_batch(good, clock), clock)
    assert len(store.gauge_readings("D1")) == 20
    store.merge(gauge_batch([reading(T0 + timedelta(days=3650))], clock), clock)
    assert len(store.gauge_readings("D1")) >= 20, "history deleted by a single reading from the year 2036"


@pytest.mark.xfail(strict=True, reason="R-6 a quake with a far-future `updated` freezes it against all later updates (store.py:merge)")
def test_future_updated_does_not_freeze_a_quake(clock):
    from conftest import make_quake
    from dataclasses import replace

    store = Store()
    q = make_quake("a", 4.0)
    store.merge(Batch("usgs", T0, quakes=(replace(q, updated=T0 + timedelta(days=36500)),), backfill=True), clock)
    store.merge(Batch("usgs", T0, quakes=(replace(q, mag=6.0, updated=T0),), backfill=True), clock)
    assert store.get("a").mag == 6.0


# --- 2. soak: simulated days of swarm under random faults ----------------------------------------------------


def swarm_body(world: list[dict], now: datetime, rng: random.Random, hours: float = 24) -> bytes:
    cutoff = now - timedelta(hours=hours)
    feats = [f for f in world if f["properties"]["time"] / 1000 >= cutoff.timestamp()]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "metadata": {"generated": int(now.timestamp() * 1000), "count": len(feats)},
            "features": feats,
        }
    ).encode()


def new_quake(i: int, when: datetime, rng: random.Random) -> dict:
    ms = int(when.timestamp() * 1000)
    return {
        "type": "Feature",
        "id": f"us{i:07d}",
        "properties": {"mag": round(rng.uniform(2.5, 6.5), 1), "time": ms, "updated": ms, "type": "earthquake",
                       "place": "swarm", "status": "automatic", "magType": "ml"},
        "geometry": {"type": "Point", "coordinates": [rng.uniform(-180, 180), rng.uniform(-80, 80), rng.uniform(0, 300)]},
    }


def random_fault(rng: random.Random) -> httpx.Response | BaseException:
    return rng.choice(
        [
            resp(429, headers={"Retry-After": str(rng.choice([1, 30, 600, 99999]))}),
            resp(429),
            resp(502),
            resp(503),
            resp(204),
            resp(200, b"<html>"),
            resp(200, b"[]"),
            resp(200, b'{"features":null}'),
            httpx.ReadTimeout("t"),
            httpx.ConnectError("c"),
        ]
    )


async def test_soak_40_days_caps_hold_under_random_faults(clock):
    """40 simulated days (past the 35-day RETAIN), a quake every ~20 min, 30% of answers faulty, plus a gauge flood and
    a stream of detail lookups. Memory-shaped structures must stay bounded and the loop must never raise."""
    rng = random.Random(20261102)
    world: list[dict] = []
    rig = Rig(clock, seed=3)
    counter = 0
    next_quake = clock.now()
    rate = timedelta(minutes=20)
    store_peak = coverage_peak = cache_peak = 0
    steps = 40 * 24 * 6  # a poll every 10 simulated minutes
    for step in range(steps):
        clock.advance(600)
        now = clock.now()
        while next_quake <= now:
            world.append(new_quake(counter, next_quake, rng))
            counter += 1
            next_quake += rate
        world = world[-3 * 24 * 3 :]  # the server's own feed only ever needs the last day (+ slack)
        rig.respond = (
            (lambda r: random_fault(rng)) if rng.random() < 0.3 else (lambda r, w=list(world), n=now: resp(200, swarm_body(w, n, rng)))
        )
        if rig.sched.due():
            result = await rig.poll()  # never raises
            assert result.source == "usgs"
        # a reading every 15 s would be 57,600 rows over 10 days; feed 40 at a time, ts monotonically increasing
        if step % 6 == 0:
            rows = [reading(now - timedelta(seconds=15 * k)) for k in range(40)]
            rig.store.merge(gauge_batch(rows, clock), clock)
            assert len(rig.store.gauge_readings("D1")) <= GAUGE_MAX_ROWS
        if step % 50 == 0 and len(rig.store):
            q = rng.choice(rig.store.all())
            rig.respond = lambda r: resp(200, DETAIL)
            from dataclasses import replace

            await rig.source.fetch_detail(rig.client, replace(q, id=f"d{step}", detail_url=f"https://x.invalid/{step}"))
        store_peak = max(store_peak, len(rig.store))
        coverage_peak = max(coverage_peak, len(rig.store._coverage))
        cache_peak = max(cache_peak, len(rig.source._details))
        assert len(rig.client._transport._cache) <= CACHE_ENTRIES
    per_retain = RETAIN / rate
    assert store_peak <= per_retain * 1.05, f"store grew past RETAIN: {store_peak} > {per_retain}"
    assert min(q.time for q in rig.store.all()) >= clock.now() - RETAIN - timedelta(hours=1)
    assert coverage_peak <= 8, f"coverage claims keep accumulating: {coverage_peak}"
    assert cache_peak <= DETAIL_CACHE_SIZE
    assert len(rig.store._by_alias) <= store_peak * 3
    assert not rig.sched._tasks
    assert len(rig.client._transport._blocked) <= 2
    await rig.client.aclose()
    assert rig.client.is_closed
    assert rig.status.health.polls > steps * 0.5, "poll loop starved by faults"
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


class FastClock(ReplayClock):
    """Sleep advances the clock instead of waiting, so Scheduler.run() can be driven for hours of simulated time."""

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


async def test_scheduler_run_12_simulated_hours_no_task_or_client_leak():
    clock = FastClock(T0, real=lambda: 0.0)
    rng = random.Random(5)
    rig = Rig(clock, seed=9)
    world = [new_quake(i, T0 - timedelta(minutes=10 * i), rng) for i in range(50)]
    state = {"n": 0}

    def respond(request):
        state["n"] += 1
        if rng.random() < 0.3:
            return random_fault(rng)
        return resp(200, swarm_body(world, clock.now(), rng))

    rig.respond = respond
    runner = asyncio.create_task(rig.sched.run(rig.client))
    deadline = T0 + timedelta(hours=12)
    while clock.now() < deadline and not runner.done():
        await asyncio.sleep(0)
    assert not runner.done(), f"scheduler.run exited: {runner.exception() if runner.done() else ''}"
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    for _ in range(5):
        await asyncio.sleep(0)
    assert state["n"] > 20
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == [], "poll tasks outlived run()"
    assert not rig.status.in_flight
    # honours min_interval even when sleep is free: no more than one steady poll per 60 s of simulated time
    assert state["n"] <= 12 * 3600 / 60 + 20
    await rig.client.aclose()


# --- 3. random keys against the real app ----------------------------------------------------------------------

KEYS = [
    "f", "s", "slash", "m", "M", "z", "t", "o", "h", "a", "r", "question_mark", "escape", "enter", "j", "k", "up",
    "down", "left", "right", "pageup", "pagedown", "home", "end", "tab", "shift+tab", "b", "y", "x", "space", "1", "backspace",
]  # fmt: skip
SIZES = [(80, 24), (120, 40), (200, 60), (40, 12), (24, 6)]


def key_app() -> EarthwaveApp:
    week = load_json("usgs/4.5_week.geojson")
    from earthwave.sources.usgs import parse_collection

    now = datetime.fromtimestamp(week["metadata"]["generated"] / 1000, tz=UTC)
    clock = ReplayClock(now, real=lambda: 0.0)
    config = with_overrides(Config(), 4.5, 168, (40.7, -74.0), "unicode")
    store = Store(config.alerts, config.home)
    store.merge(Batch("usgs", now, quakes=parse_collection(week), backfill=True, coverage=(168.0, 4.5)), clock)

    def handler(request):
        if "/detail/" in request.url.path:
            return resp(200, DETAIL)
        return resp(503)

    return EarthwaveApp(
        config, store=store, clock=clock, run_scheduler=False, ticks=False, transport=httpx.MockTransport(handler)
    )


async def press_all(keys: list[str], resizes: dict[int, tuple[int, int]]) -> list[str]:
    """Press `keys`; return the trace. Fails on the first uncaught exception or lost focus."""
    app = key_app()
    trace: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for i, key in enumerate(keys):
            if i in resizes:
                await pilot.resize_terminal(*resizes[i])
            if key == "q":
                continue
            trace.append(key)
            await pilot.press(key)
            await pilot.pause()
            assert app._exception is None, f"uncaught {app._exception!r} after {trace}"
            assert app.is_running, f"app stopped after {trace}"
            assert app.focused is not None or not app.screen.focus_chain, f"focus lost on {app.screen!r} after {trace}"
    return trace


@pytest.mark.parametrize("seed", range(6))
async def test_random_keys_seeded(seed):
    rng = random.Random(seed)
    keys = [rng.choice(KEYS) for _ in range(120)]
    resizes = {rng.randrange(120): rng.choice(SIZES) for _ in range(4)}
    await press_all(keys, resizes)


@settings(max_examples=12, deadline=None, suppress_health_check=list(HealthCheck))
@given(
    st.lists(st.sampled_from(KEYS), min_size=1, max_size=40),
    st.dictionaries(st.integers(0, 39), st.sampled_from(SIZES), max_size=2),
)
def test_random_keys_hypothesis(keys, resizes):
    asyncio.run(press_all(keys, resizes))


async def test_every_screen_then_escape_returns_to_dashboard_with_focus():
    app = key_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for opener in ["f", "t", "o", "h", "question_mark", "enter"]:
            await pilot.press(opener)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.dashboard, f"escape after {opener!r} did not return to the dashboard"
            assert app.focused is not None, f"focus lost after {opener!r}"
