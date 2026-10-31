"""M4b: DART and CO-OPS sources (adapters, cadence), station selection, geometry chain, anomaly decisions."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, T0

from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig, PollConfig
from earthwave.models import Batch, GaugeReading, Station
from earthwave.net import build_client
from earthwave.sources import coops, ndbc
from earthwave.sources.anomaly import find_anomaly, threshold_m
from earthwave.sources.base import SourceHealth
from earthwave.sources.geo import epicentre, geometry_points, haversine_km, select_coops, select_dart

HEALTH = SourceHealth()
STATIONS_XML = (FIXTURES / "ndbc" / "activestations.xml").read_text(encoding="utf-8")
DART_46402 = (FIXTURES / "ndbc" / "46402.dart").read_text(encoding="utf-8")
DART_32401 = (FIXTURES / "ndbc" / "32401.dart").read_text(encoding="utf-8")
COOPS_STATIONS = (FIXTURES / "coops" / "stations.json").read_text(encoding="utf-8")
NO_DATA = '{"error": {"message": "No data was found."}}'


def _fixture(name: str) -> str:
    return (FIXTURES / "coops" / name).read_text(encoding="utf-8")


def _coops_stations() -> list[Station]:
    return [coops.to_station(s) for s in coops.parse_stations(COOPS_STATIONS)]


def _dart_until(text: str, stamp: str) -> str:
    """The 32401 file as it stood when its newest row was `stamp` ('YYYY MM DD hh mm')."""
    keep = [ln for ln in text.splitlines() if ln.startswith("#") or ln[:16] <= stamp]
    return "\n".join(keep) + "\n"


# --- geometry chain and selection --------------------------------------------------------------------------------


def test_geometry_chain_prefers_geometry_over_quake():
    quake = (10.0, 20.0)
    polygon = ("polygon", ((-124.0, 40.0), (-123.0, 41.0), (-122.0, 42.0)))
    assert geometry_points(polygon, quake) == [(40.0, -124.0), (41.0, -123.0), (42.0, -122.0)]
    assert geometry_points(("circle", 51.5, 179.0, 300.0), quake) == [(51.5, 179.0)]
    assert geometry_points(None, quake) == [quake]
    assert geometry_points(("polygon", ()), quake) == [quake]
    assert geometry_points(None, None) == []


def test_polygon_vertices_are_thinned():
    ring = tuple((float(i) / 100, 10.0) for i in range(1000))
    points = geometry_points(("polygon", ring))
    assert 20 < len(points) <= 48 and points[0] == (10.0, 0.0)


def test_epicentre_takes_first_known():
    assert epicentre(None, (1.0, 2.0), (3.0, 4.0)) == (1.0, 2.0)
    assert epicentre(None, None) is None


def test_select_dart_nearest_six_within_3000_km():
    stations = ndbc.load_bundled_stations()
    kamchatka = (52.8, 160.0)
    picked = select_dart(stations, kamchatka)
    km = [d for d, _ in picked]
    assert len(picked) == 6 and km == sorted(km) and km[-1] <= 3000
    assert all(abs(d - haversine_km(*kamchatka, s.lat, s.lon)) < 1e-6 for d, s in picked)
    assert len(select_dart(stations, kamchatka, max_km=500)) < 6
    assert select_dart(stations, (0.0, -140.0), n=6, max_km=1) == []


def test_select_dart_skips_excluded_and_other_kinds_and_crosses_the_dateline():
    stations = ndbc.load_bundled_stations()
    first = select_dart(stations, (50.0, 179.0))[0][1]
    second = select_dart(stations, (50.0, 179.0), exclude={first.id})[0][1]
    assert second.id != first.id
    mixed = [*stations, Station("9414290", "coops", "SF", 50.0, 179.0)]
    assert all(s.kind == "dart" for _, s in select_dart(mixed, (50.0, 179.0)))
    # a buoy at lon -179 is about 70 km from a quake at lon 179.5, not about 20,000 km
    wrap = [Station("x", "dart", "wrap", 50.0, -179.0)]
    assert select_dart(wrap, (50.0, 179.5))[0][0] < 150


def test_select_coops_nearest_four_to_points():
    stations = _coops_stations()
    picked = select_coops(stations, [(37.8, -122.4)])
    assert len(picked) == 4 and [d for d, _ in picked] == sorted(d for d, _ in picked)
    assert picked[0][0] < 10 and all(s.kind == "coops" for _, s in picked)
    # a polygon ranks each gauge by its nearest vertex, so a distant vertex pulls in its own gauges
    two_places = [(37.8, -122.4), (21.3, -157.9)]
    hawaii = [d for d, s in select_coops(stations, two_places, n=len(stations)) if s.lon < -150]
    assert hawaii and min(hawaii) < 50
    assert select_coops(stations, []) == []
    assert select_coops(ndbc.load_bundled_stations(), [(37.8, -122.4)]) == []


# --- anomaly decisions -------------------------------------------------------------------------------------------


def _dart_residuals_with_pulse(pulse_m: float):
    """46402 with `pulse_m` added to the newest reading that has a residual (a hole can silence the very last)."""
    rows = ndbc.parse_dart(DART_46402, "46402")[-120:]
    at = ndbc.detrend(rows)[-1].ts
    rows = [
        ndbc.GaugeReading(r.station_id, r.kind, r.ts, r.value_m + pulse_m, r.mode) if r.ts == at else r for r in rows
    ]
    return ndbc.detrend(rows)


def test_find_anomaly_dart_pulse_and_quiet():
    alerts = AlertsConfig()
    quiet = ndbc.detrend(ndbc.parse_dart(DART_46402, "46402")[-120:])
    assert quiet and find_anomaly("46402", "dart", quiet, alerts) is None
    pulse = _dart_residuals_with_pulse(0.10)
    got = find_anomaly("46402", "dart", pulse, alerts)
    assert got is not None and got.station_id == "46402" and got.ts == pulse[-1].ts
    assert got.residual_m == pytest.approx(0.10, abs=0.02) and got.threshold_m == 0.05
    negative = find_anomaly("46402", "dart", _dart_residuals_with_pulse(-0.10), alerts)
    assert negative is not None and negative.residual_m < 0  # sign kept


def test_find_anomaly_since_picks_largest_and_ignores_old():
    class R:
        def __init__(self, ts, residual_m):
            self.ts, self.residual_m = ts, residual_m

    series = [R(T0 + timedelta(minutes=i), v) for i, v in enumerate([0.0, 0.06, -0.09, 0.04, 0.05])]
    alerts = AlertsConfig()
    got = find_anomaly("s", "dart", series, alerts)
    assert got is not None and got.residual_m == -0.09 and got.ts == T0 + timedelta(minutes=2)
    later = find_anomaly("s", "dart", series, alerts, since=T0 + timedelta(minutes=2))
    assert later is not None and later.residual_m == 0.05  # at the threshold counts
    assert find_anomaly("s", "dart", series, alerts, since=T0 + timedelta(minutes=4)) is None
    assert find_anomaly("s", "dart", [], alerts) is None


def test_find_anomaly_uses_coops_threshold_for_gauges():
    obs = coops.parse_one_minute_water_level(_fixture("one_minute_water_level_9414290.json"))
    preds = coops.parse_predictions(_fixture("predictions_1min_9414290.json"), "9414290")
    res = coops.residuals(obs, preds)
    alerts = AlertsConfig()
    assert threshold_m("coops", alerts) == 0.3 and threshold_m("dart", alerts) == 0.05
    got = find_anomaly("9414290", "coops", res, alerts)
    assert got is not None and abs(got.residual_m) >= 0.3
    assert find_anomaly("9414290", "coops", res, AlertsConfig(gauge_anomaly_gauge_m=5.0)) is None


# --- fake server -------------------------------------------------------------------------------------------------


class Server:
    """Records requests and answers by URL; `routes` maps a URL substring to a body, a (status, body) or a callable."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for needle, answer in self.routes.items():
            if needle in str(request.url):
                answer = answer(request) if callable(answer) else answer
                status, body = answer if isinstance(answer, tuple) else (200, answer)
                return httpx.Response(status, content=body.encode("utf-8"))
        return httpx.Response(404, content=b"nope")

    def count(self, needle: str) -> int:
        return sum(needle in str(r.url) for r in self.requests)

    def products(self, product: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.params.get("product") == product]

    def client(self) -> httpx.AsyncClient:
        return build_client(transport=httpx.MockTransport(self))


# --- DART source -------------------------------------------------------------------------------------------------


async def test_dart_fetch_adapts_selected_stations_only():
    server = Server({"activestations.xml": STATIONS_XML, "46402.dart": DART_46402, "32401.dart": DART_32401})
    source = ndbc.DartSource(ReplayClock(T0, real=lambda: 0.0))
    source.set_selection(["46402"])
    async with server.client() as client:
        batch = await source.fetch(client, None)
    assert isinstance(batch, Batch) and batch.source == "dart"
    assert server.count("32401") == 0 and server.count("46402.dart") == 1
    assert batch.readings and all(isinstance(r, GaugeReading) and r.station_id == "46402" for r in batch.readings)
    newest = max(r.ts for r in batch.readings)
    assert min(r.ts for r in batch.readings) >= newest - ndbc.HISTORY
    assert batch.readings[0].kind == "dart" and batch.readings[0].mode == "15m"
    assert batch.stations and all(isinstance(s, Station) and s.kind == "dart" for s in batch.stations)


async def test_dart_station_list_hourly_with_bundled_fallback(clock):
    answers = [(500, "down"), STATIONS_XML]
    server = Server({"activestations.xml": lambda _r: answers.pop(0) if len(answers) > 1 else answers[0]})
    source = ndbc.DartSource(clock)
    assert len(source.stations) == 76  # bundled snapshot before any fetch
    async with server.client() as client:
        first = await source.fetch(client, None)
        assert first.stations == () and len(source.stations) == 76 and server.count("activestations") == 1
        clock.advance(ndbc.STATIONS_RETRY - 1)
        await source.fetch(client, None)
        assert server.count("activestations") == 1  # not before the retry delay
        clock.advance(2)
        again = await source.fetch(client, None)
        assert again.stations and server.count("activestations") == 2
        clock.advance(ndbc.STATIONS_TTL - 1)
        await source.fetch(client, None)
        assert server.count("activestations") == 2  # cached for the hour
        clock.advance(2)
        await source.fetch(client, None)
        assert server.count("activestations") == 3


async def test_dart_404_marks_unavailable_and_total_failure_raises(clock):
    server = Server({"activestations.xml": STATIONS_XML, "21413.dart": (404, "x"), "46402.dart": DART_46402})
    source = ndbc.DartSource(clock)
    source.set_selection(["21413", "46402"])
    async with server.client() as client:
        batch = await source.fetch(client, None)
    assert {r.station_id for r in batch.readings} == {"46402"} and source.unavailable == {"21413"}
    broken = Server({"activestations.xml": STATIONS_XML, "46402.dart": (503, "x")})
    source.set_selection(["46402"])
    async with broken.client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch(client, None)


def test_dart_cadence_follows_alert_hint(clock):
    source = ndbc.DartSource(clock)
    assert source.interval(HEALTH) == 900
    source.set_alert_active(True)
    assert source.interval(HEALTH) == 60
    source.set_alert_active(False)
    assert source.interval(HEALTH) == 900
    custom = ndbc.DartSource(clock, PollConfig(dart_seconds=600, alert_gauge_seconds=30))
    assert custom.interval(HEALTH) == 600
    custom.set_alert_active(True)
    assert custom.interval(HEALTH) == 30 == custom.min_interval


async def test_dart_cadence_switches_on_event_mode_under_a_fake_clock():
    event_end = datetime(2026, 10, 9, 21, 59, tzinfo=UTC)
    clock = ReplayClock(event_end + timedelta(minutes=5), real=lambda: 0.0)
    server = Server({"32401.dart": _dart_until(DART_32401, "2026 10 09 21 59"), "activestations": (500, "")})
    source = ndbc.DartSource(clock)
    source.set_selection(["32401"])
    assert source.interval(HEALTH) == 900 and not source.event_mode()
    async with server.client() as client:
        batch = await source.fetch(client, None)
    assert {r.mode for r in batch.readings} >= {"1m", "15m"}
    assert source.event_mode() and source.interval(HEALTH) == 60
    clock.advance(26 * 60)  # 31 minutes after the newest event row
    assert source.interval(HEALTH) == 900  # the window expired on the injected clock without another fetch
    # the whole file ends on a standard 15-minute row, so it is not event mode
    quiet = ndbc.DartSource(ReplayClock(datetime(2026, 10, 31, 6, 5, tzinfo=UTC), real=lambda: 0.0))
    quiet.set_selection(["32401"])
    async with Server({"32401.dart": DART_32401, "activestations": (500, "")}).client() as client:
        await quiet.fetch(client, None)
    assert quiet.interval(HEALTH) == 900


# --- CO-OPS source -----------------------------------------------------------------------------------------------


def coops_server(one_minute: bool = True) -> Server:
    def data(request: httpx.Request):
        product = request.url.params["product"]
        if product == "water_level":
            return _fixture("water_level_9414290.json")
        if product == "one_minute_water_level":
            return _fixture("one_minute_water_level_9414290.json") if one_minute else NO_DATA
        one = request.url.params["interval"] == "1"
        return _fixture("predictions_1min_9414290.json" if one else "predictions_9414290.json")

    return Server({"mdapi": COOPS_STATIONS, "datagetter": data})


async def test_coops_standard_poll_uses_six_minute_data(clock):
    server = coops_server()
    source = coops.CoopsSource(clock)
    source.set_selection(["9414290"])
    assert source.interval(HEALTH) == 300
    async with server.client() as client:
        batch = await source.fetch(client, None)
    [water] = server.products("water_level")
    assert dict(water.url.params) == {
        "product": "water_level",
        "station": "9414290",
        "begin_date": "20261031 06:00",
        "end_date": "20261031 12:00",
        "datum": "MLLW",
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "application": "earthwave",
    }
    assert len(server.products("predictions")) == 1 and server.products("predictions")[0].url.params["interval"] == "6"
    assert batch.source == "coops" and batch.readings and {r.mode for r in batch.readings} == {"6m"}
    assert all(isinstance(r, GaugeReading) and r.kind == "coops" for r in batch.readings)
    assert len(batch.stations) == 302 and len(source.stations) == 302
    observed = [coops.GaugeReading(r.station_id, r.kind, r.ts, r.value_m, r.mode) for r in batch.readings]
    assert coops.residuals(observed, source.predictions("9414290"))


async def test_coops_alert_polls_one_minute_data_every_60s(clock):
    server = coops_server()
    source = coops.CoopsSource(clock)
    source.set_selection(["9414290"])
    source.set_alert_active(True)
    assert source.interval(HEALTH) == 60
    async with server.client() as client:
        batch = await source.fetch(client, None)
    assert {r.mode for r in batch.readings} == {"1m"}
    assert server.products("predictions")[0].url.params["interval"] == "1"
    source.set_alert_active(False)
    assert source.interval(HEALTH) == 300


async def test_coops_falls_back_to_six_minute_without_one_minute_data(clock):
    source = coops.CoopsSource(clock)
    source.set_selection(["9414290"])
    source.set_alert_active(True)
    async with coops_server(one_minute=False).client() as client:
        batch = await source.fetch(client, None)
    assert {r.mode for r in batch.readings} == {"6m"}


async def test_coops_predictions_cached_and_stations_daily(clock):
    server = coops_server()
    source = coops.CoopsSource(clock)
    source.set_selection(["9414290"])
    async with server.client() as client:
        await source.fetch(client, None)
        clock.advance(300)
        second = await source.fetch(client, None)
        assert second.stations == () and server.count("mdapi") == 1
        assert len(server.products("predictions")) == 1
        clock.advance(coops.PREDICTION_TTL.total_seconds())
        await source.fetch(client, None)
        assert len(server.products("predictions")) == 2
        clock.advance(coops.STATIONS_TTL)
        again = await source.fetch(client, None)
        assert again.stations and server.count("mdapi") == 2


async def test_coops_skips_a_gauge_with_no_data_but_raises_when_all_fail(clock):
    def data(request: httpx.Request):
        if request.url.params["station"] == "bad":
            return NO_DATA
        if request.url.params["product"] == "water_level":
            return _fixture("water_level_9414290.json")
        return _fixture("predictions_9414290.json")

    source = coops.CoopsSource(clock)
    source.set_selection(["bad", "9414290"])
    async with Server({"mdapi": COOPS_STATIONS, "datagetter": data}).client() as client:
        batch = await source.fetch(client, None)
        assert {r.station_id for r in batch.readings} == {"9414290"}
        source.set_selection(["bad"])
        with pytest.raises(coops.CoopsError):
            await source.fetch(client, None)


async def test_coops_station_failure_with_no_cache_raises(clock):
    source = coops.CoopsSource(clock)
    async with Server({"mdapi": (500, "x")}).client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch(client, None)
