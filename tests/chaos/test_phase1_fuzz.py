"""Beyond-UAT chaos probes: hostile USGS GeoJSON, swarm + revision merges, geodesic oracle. Read-only audit tests."""

import copy
import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from earthwave.clock import ReplayClock
from earthwave.models import Batch
from earthwave.sources.geo import bearing_deg, haversine_km
from earthwave.sources.usgs import parse_collection
from earthwave.state.store import SortKey, Store, ViewFilter

T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
MS = int(T0.timestamp() * 1000)


def feat(fid="us1", **over):
    props = {
        "mag": 4.5, "time": MS, "updated": MS, "type": "earthquake", "status": "automatic", "place": "x",
        "alert": None, "cdi": None, "mmi": None, "felt": None, "tz": None, "dmin": None, "tsunami": 0, "sig": 100,
        "ids": f",{fid},", "magType": "mb", "url": "", "detail": "",
    }
    geom = over.pop("coords", [10.0, 20.0, 5.0])
    props.update(over)
    return {"id": fid, "properties": props, "geometry": {"type": "Point", "coordinates": geom}}


def coll(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# --- targeted payloads: one malformed feature must never take the batch with it ---------------------------------
HOSTILE = {
    "time_null": feat(time=None),
    "time_huge": feat(time=10**20),
    "time_str": feat(time="soon"),
    "props_null": {"id": "p", "properties": None, "geometry": {"coordinates": [1, 2, 3]}},
    "ids_list": feat(ids=["a", "b"]),
    "geometry_null": {"id": "g", "properties": feat()["properties"], "geometry": None},
    "mag_nan": feat(mag=float("nan")),
    "lat_nan": feat(coords=[10.0, float("nan"), 5.0]),
    "lat_inf": feat(coords=[10.0, float("inf"), 5.0]),
    "lat_out_of_range": feat(coords=[10.0, 123.0, 5.0]),
    "updated_huge": feat(updated=10**20),
    "feature_is_list": [1, 2],
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_one_bad_feature_never_raises(name):
    good = feat("ok1")
    result = parse_collection(coll(HOSTILE[name], good))
    assert any(q.id == "ok1" for q in result), "good neighbour lost"


@pytest.mark.parametrize("name", ["mag_nan", "lat_nan", "lat_inf", "lat_out_of_range"])
def test_non_finite_values_do_not_enter_store(name):
    assert all(math.isfinite(q.mag) and math.isfinite(q.lat) and math.isfinite(q.lon) and -90 <= q.lat <= 90
               for q in parse_collection(coll(HOSTILE[name])))


def test_spec_negative_and_null_fields_parse():
    qs = parse_collection(coll(
        feat("n1", mag=-0.4, coords=[1, 2, -1.2]), feat("n2", mag=0.0, coords=[1, 2]), feat("n3", mag=None),
        feat("b1", type="quarry blast"), feat("b2", type="ice quake"), feat("b3", type="explosion"),
        feat("b4", type="volcanic eruption"), feat("n4", alert="red", felt=None, cdi=None),
    ))
    assert sorted(q.id for q in qs) == ["n1", "n2", "n4"]


# --- property fuzz -----------------------------------------------------------------------------------------------
junk = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=8),
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=5), c, max_size=3),
    max_leaves=6,
)


@st.composite
def mutated(draw):
    f = copy.deepcopy(feat("fz"))
    for _ in range(draw(st.integers(1, 4))):
        target = draw(st.sampled_from(["mag", "time", "updated", "ids", "alert", "felt", "cdi", "mmi", "sig", "type", "tsunami", "status", "place"]))
        f["properties"][target] = draw(junk)
    if draw(st.booleans()):
        f["geometry"]["coordinates"] = draw(st.lists(junk, max_size=4))
    return f


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(mutated())
def test_fuzz_parse_collection_total(f):
    for q in parse_collection(coll(f)):
        assert math.isfinite(q.mag) and math.isfinite(q.lat) and math.isfinite(q.lon)


# --- swarm + revision merge --------------------------------------------------------------------------------------
def quakes_from(feats):
    return parse_collection(coll(*feats))


def test_swarm_with_revisions_no_dupes_no_drops_no_sort_crash():
    clock = ReplayClock(T0 + timedelta(hours=1), real=lambda: 0.0)
    store = Store()
    store.merge(Batch("usgs", clock.now(), quakes=quakes_from([feat("seed")]), coverage=(24.0, -10.0), generated=T0), clock)
    store.warm = True
    expected = {"seed"}
    for i in range(300):
        t = MS + i * 1000
        fid = f"nc{i}"
        expected.add(fid)
        feats = [feat(fid, time=t, updated=t, mag=1.0 + (i % 40) / 10, ids=f",{fid},", status="automatic")]
        if i % 3 == 0 and i:  # regional id superseded by authoritative `us` id, reviewed, updated > time
            prev = f"nc{i - 1}"
            feats.append(feat(f"us{i}", time=t - 1000, updated=t + 5000, status="reviewed", ids=f",us{i},{prev},"))
            expected.discard(prev)
            expected.add(f"us{i}")
        if i % 7 == 0:  # same event delivered twice, second older revision arriving late
            feats.append(feat(fid, time=t, updated=t - 9999, mag=9.9, ids=f",{fid},"))
        store.merge(Batch("usgs", clock.now(), quakes=quakes_from(feats)), clock)
        clock.advance(1)
    ids = [q.id for q in store.all()]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected, (sorted(expected - set(ids))[:5], sorted(set(ids) - expected)[:5])
    assert 9.9 not in {q.mag for q in store.all()}, "stale revision overwrote newer"
    for s in SortKey:
        store.visible(ViewFilter(min_magnitude=-10, hours=48), clock, s)


def test_ring_bound_over_simulated_days():
    clock = ReplayClock(T0, real=lambda: 0.0)
    store = Store()
    peak = 0
    for day in range(50):
        for i in range(200):
            t = int((clock.now().timestamp() + i) * 1000)
            store.merge(Batch("usgs", clock.now(), quakes=quakes_from([feat(f"d{day}-{i}", time=t, updated=t)])), clock)
        clock.advance(86400)
        peak = max(peak, len(store))
    assert peak <= 200 * 37, f"store grew to {peak}"


# --- geodesic oracle ---------------------------------------------------------------------------------------------
geographiclib = pytest.importorskip("geographiclib.geodesic")
G = geographiclib.Geodesic.WGS84.Inverse
lat_s = st.floats(-90, 90)
lon_s = st.floats(-180, 180)


@settings(max_examples=500, deadline=None)
@given(lat_s, lon_s, lat_s, lon_s)
def test_haversine_vs_geographiclib(a, b, c, d):
    ref = G(a, b, c, d)["s12"] / 1000
    got = haversine_km(a, b, c, d)
    assert abs(got - ref) <= max(0.6e-2 * ref, 1e-6)


@pytest.mark.parametrize("p", [(0, 0, 0, 0), (90, 0, 90, 180), (-90, 5, 90, 5), (0, 0, 0, 180), (10, 20, -10, -160),
                               (0, 179.999, 0, -179.999), (45, 180, 45, -180)])
def test_edge_points(p):
    d = haversine_km(*p)
    assert math.isfinite(d) and 0.0 <= d <= 20038
    b = bearing_deg(*p)
    assert math.isfinite(b) and 0.0 <= b < 360.0
    assert haversine_km(*p) == pytest.approx(haversine_km(p[2], p[3], p[0], p[1]))
