"""USGS parser and fetch hardening: one hostile feature or reply must not poison a batch (#284 #285 #288 #289 #290 #294)."""

import json
import math
from datetime import timedelta

import httpx
import pytest
from conftest import FIXTURES

from earthwave.sources.usgs import UsgsSource, parse_collection, parse_nearby_cities

HOUR = (FIXTURES / "usgs" / "all_hour.geojson").read_bytes()
DETAIL = (FIXTURES / "usgs" / "detail.geojson").read_bytes()
MS = 1_761_912_000_000


def feat(fid="us1", **over):
    props = {
        "mag": 4.5, "time": MS, "updated": MS, "type": "earthquake", "status": "automatic", "place": "x",
        "alert": None, "tsunami": 0, "sig": 100, "ids": f",{fid},", "magType": "mb", "url": "", "detail": "",
    }  # fmt: skip
    geom = over.pop("coords", [10.0, 20.0, 5.0])
    props.update(over)
    return {"id": fid, "properties": props, "geometry": {"type": "Point", "coordinates": geom}}


def coll(*features):
    return {"type": "FeatureCollection", "features": list(features)}


NAN, INF = float("nan"), float("inf")
BAD_FEATURES = {
    "time_null": feat(time=None),
    "time_huge": feat(time=10**20),
    "time_str": feat(time="soon"),
    "props_null": {"id": "p", "properties": None, "geometry": {"coordinates": [1, 2, 3]}},
    "ids_list": feat(ids=["a", "b"]),
    "ids_number": feat(ids=5),
    "geometry_null": {"id": "g", "properties": feat()["properties"], "geometry": None},
    "updated_huge": feat(updated=10**20),
    "feature_is_list": [1, 2],
    "mag_nan": feat(mag=NAN),
    "mag_inf": feat(mag=INF),
    "lat_nan": feat(coords=[10.0, NAN, 5.0]),
    "lon_inf": feat(coords=[INF, 20.0, 5.0]),
    "lat_out_of_range": feat(coords=[10.0, 123.0, 5.0]),
    "lon_out_of_range": feat(coords=[190.0, 20.0, 5.0]),
}


@pytest.mark.parametrize("name", sorted(BAD_FEATURES))
def test_one_bad_feature_is_skipped_and_neighbours_survive(name):
    ids = [q.id for q in parse_collection(coll(feat("a"), BAD_FEATURES[name], feat("b")))]
    assert ids == ["a", "b"]


def test_extreme_but_valid_coordinates_parse():
    quakes = parse_collection(coll(feat("n", coords=[-180.0, -90.0, -1.0]), feat("s", coords=[180.0, 90.0, 700.0])))
    assert [q.id for q in quakes] == ["n", "s"]
    assert all(math.isfinite(q.lat) and math.isfinite(q.lon) for q in quakes)


@pytest.mark.parametrize("raw", ["red", "Red", "RED", " red "])
def test_pager_alert_is_normalised_to_lowercase(raw):
    assert parse_collection(coll(feat(alert=raw)))[0].pager_alert == "red"


@pytest.mark.parametrize("raw", ["purple", "", 3, ["red"], None])
def test_unknown_pager_alert_is_dropped(raw):
    assert parse_collection(coll(feat(alert=raw)))[0].pager_alert is None


@pytest.mark.parametrize("rows", [{"a": 1}, None, "text", 5])
def test_nearby_cities_ignores_non_list(rows):
    assert parse_nearby_cities(rows) == ()


def test_nearby_cities_ignores_non_dict_rows():
    rows = ["x", 1, None, {"distance": 3, "direction": "N", "name": "Town"}, {"name": "partial"}]
    assert parse_nearby_cities(rows) == ("3 km N of Town",)


def make_client(respond):
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


NO_FEATURES = [b"{}", b'{"type":"FeatureCollection"}', b'{"error":"maintenance"}', b'{"features":{"a":1}}']


@pytest.mark.parametrize("body", NO_FEATURES)
async def test_fetch_without_a_feature_list_is_a_failure(clock, body):
    source = UsgsSource(clock)
    async with make_client(lambda r: httpx.Response(200, content=body)) as client:
        with pytest.raises(Exception, match="features"):
            await source.fetch(client, None)


@pytest.mark.parametrize("body", NO_FEATURES)
async def test_fetch_range_without_a_feature_list_claims_no_coverage(clock, body):
    source = UsgsSource(clock)
    async with make_client(lambda r: httpx.Response(200, content=body)) as client:
        with pytest.raises(Exception, match="features"):
            await source.fetch_range(client, clock.now() - timedelta(days=3), clock.now(), 4.5)


async def test_fetch_still_accepts_an_empty_feature_list(clock):
    source = UsgsSource(clock)
    async with make_client(lambda r: httpx.Response(200, content=b'{"features":[]}')) as client:
        batch = await source.fetch(client, None)
    assert batch.quakes == ()


async def test_304_without_a_validator_is_not_a_good_poll(clock):
    source = UsgsSource(clock)
    async with make_client(lambda r: httpx.Response(304)) as client:
        with pytest.raises(httpx.HTTPError):
            await source.fetch(client, None)
    assert source.starting, "the startup feed must not be consumed by a 304 nobody asked for"


async def test_304_to_a_conditional_request_is_still_not_modified(clock):
    source = UsgsSource(clock)
    async with make_client(
        lambda r: httpx.Response(200, content=HOUR, headers={"Last-Modified": "Sat, 01 Nov 2025 12:00:00 GMT"})
    ) as client:
        while source.starting:
            await source.fetch(client, None)
        cursor = (await source.fetch(client, None)).cursor  # the steady feed's own validator
    async with make_client(lambda r: httpx.Response(304)) as client:
        batch = await source.fetch(client, cursor)
    assert batch.not_modified


@pytest.mark.parametrize("cities", [b'{"a": 1}', b'["x", "y"]', b"[1, 2]", b"null", b'"text"'])
async def test_malformed_nearby_cities_keeps_the_rest_of_the_detail(clock, cities):
    data = json.loads(DETAIL)
    data["properties"]["products"]["nearby-cities"] = [
        {"contents": {"nearby-cities.json": {"url": "https://earthquake.usgs.gov/cities.json"}}}
    ]

    def respond(request):
        if request.url.path.endswith("cities.json"):
            return httpx.Response(200, content=cities)
        return httpx.Response(200, content=json.dumps(data).encode())

    quake = parse_collection(json.loads(HOUR))[0]
    async with make_client(respond) as client:
        detail = await UsgsSource(clock).fetch_detail(client, quake)
    assert detail.products
    assert detail.nearby_cities == ()
