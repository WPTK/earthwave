"""Property tests for the USGS, JMA and DART parsers: arbitrary input never escapes the parser's own error contract,
and what does come out obeys the invariants the rest of the app leans on. Product code is not touched."""

import contextlib
import math
from datetime import UTC, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from earthwave.sources import ndbc
from earthwave.sources.jma import fallback_alert, parse_coordinate, parse_detail
from earthwave.sources.levels import RANK
from earthwave.sources.usgs import PAGER_LEVELS, parse_collection, parse_feature, parse_nearby_cities
from earthwave.sources.usgs import parse_detail as parse_usgs_detail

FAST = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much]
)

# JSON-shaped junk: what a hostile or broken feed could hold anywhere in a document.
scalars = st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20)
json_values = st.recursive(
    scalars,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)

# numbers a feed can send: finite, non-finite, out of range, numeric strings
numbers = (
    st.floats(allow_nan=True, allow_infinity=True)
    | st.integers(-1000, 1000)
    | st.sampled_from(["1.5", "x", "nan", "inf"])
)
alerts = st.none() | st.sampled_from(["green", "Orange ", " RED", "Yellow", "purple", "", "none"]) | json_values
millis = st.integers(min_value=0, max_value=4_000_000_000_000)


@st.composite
def usgs_features(draw):
    props = {
        "mag": draw(st.none() | st.floats(min_value=-2, max_value=10) | numbers | json_values),
        "time": draw(millis | json_values),
        "updated": draw(millis | st.none()),
        "type": draw(st.sampled_from(["earthquake", "quarry blast", "explosion"])),
        "place": draw(st.none() | st.text(max_size=30)),
        "ids": draw(st.none() | st.text(max_size=30)),
        "alert": draw(alerts),
        "tsunami": draw(st.sampled_from([0, 1])),
    }
    coords = draw(st.lists(numbers | st.none(), max_size=4))
    return {"id": draw(st.text(min_size=1, max_size=12)), "properties": props, "geometry": {"coordinates": coords}}


@FAST
@given(st.lists(usgs_features() | json_values, max_size=6))
def test_usgs_collection_keeps_only_sane_quakes(features):
    quakes = parse_collection({"type": "FeatureCollection", "features": features})
    for q in quakes:
        assert math.isfinite(q.mag) and math.isfinite(q.lat) and math.isfinite(q.lon) and math.isfinite(q.depth_km)
        assert -90 <= q.lat <= 90 and -180 <= q.lon <= 180
        assert q.time.tzinfo is not None
        assert q.id not in q.also_ids
        assert q.pager_alert is None or q.pager_alert in PAGER_LEVELS


@FAST
@given(json_values)
def test_usgs_collection_rejects_non_collections_with_value_error_only(data):
    with contextlib.suppress(ValueError):
        parse_collection(data)  # type: ignore[arg-type]


@FAST
@given(usgs_features())
def test_usgs_feature_is_a_quake_or_none_or_an_error_the_collection_absorbs(feature):
    try:
        quake = parse_feature(feature)
    except Exception:  # parse_collection logs and skips these; the collection itself must not raise
        assert parse_collection({"features": [feature]}) == ()
        return
    assert (quake is None) == (parse_collection({"features": [feature]}) == ())


coordinate_text = st.from_regex(
    r"[+-]\d{1,3}(\.\d{1,3})?[+-]\d{1,3}(\.\d{1,3})?([+-]\d{1,6}(\.\d+)?)?/?", fullmatch=True
)


@FAST
@given(st.text(max_size=30) | coordinate_text)
def test_jma_coordinate_is_none_or_a_pair_of_floats(text):
    result = parse_coordinate(text)
    assert result is None or all(isinstance(v, float) and math.isfinite(v) for v in result)


@FAST
@given(st.floats(min_value=-90, max_value=90), st.floats(min_value=-180, max_value=180))
def test_jma_coordinate_round_trips(lat, lon):
    lat, lon = round(lat, 3), round(lon, 3)
    assert parse_coordinate(f"{lat:+08.3f}{lon:+09.3f}/") == (lat, lon)


KINDS = ["51", "52", "53", "62", "71", "72", "73", "00", "unknown"]


@st.composite
def jma_details(draw):
    items = [
        {
            "Category": {"Kind": {"Code": draw(st.sampled_from(KINDS))}},
            "Area": {"Name": draw(st.text(max_size=8)), "enName": draw(st.text(max_size=8))},
        }
        for _ in range(draw(st.integers(0, 4)))
    ]
    head = {"InfoType": draw(st.sampled_from(["発表", "取消", "訂正"])), "ReportDateTime": "2026-10-31T12:00:00+09:00"}
    body = {"Tsunami": {"Forecast": {"Item": items}}}
    if draw(st.booleans()):
        body["Earthquake"] = [
            {"OriginTime": "2026-10-31T11:00:00+09:00", "Hypocenter": {"Area": {"Coordinate": draw(coordinate_text)}}}
        ]
    return {"Head": head, "Body": body}


entries = st.fixed_dictionaries(
    {"json": st.text(min_size=1, max_size=10)},
    optional={
        "en_ttl": st.text(max_size=20),
        "en_anm": st.text(max_size=20),
        "mag": st.sampled_from(["6.5", "7.0", ""]),
    },
)


@FAST
@given(jma_details(), entries)
def test_jma_detail_always_yields_a_ranked_alert(detail, entry):
    alert = parse_detail(detail, entry)
    assert alert.id == f"jma:{entry['json']}" == alert.dedupe_key
    assert alert.source == "jma" and alert.level in RANK


@FAST
@given(json_values, entries)
def test_jma_detail_junk_raises_only_what_the_source_catches(detail, entry):
    with contextlib.suppress(
        KeyError, TypeError, ValueError, IndexError, AttributeError
    ):  # the set JmaSource.poll absorbs
        parse_detail(detail, entry)  # type: ignore[arg-type]


@FAST
@given(entries)
def test_jma_fallback_alert_needs_only_the_list_entry(entry):
    alert = fallback_alert(entry)
    assert alert.id == f"jma:{entry['json']}" and alert.level in RANK


dart_rows = st.builds(
    lambda y, mo, d, h, mi, s, t, ht: f"{y} {mo} {d} {h} {mi} {s} {t} {ht:.3f}",
    st.integers(2000, 2040),
    st.integers(1, 12),
    st.integers(1, 28),
    st.integers(0, 23),
    st.integers(0, 59),
    st.integers(0, 59),
    st.integers(0, 4),
    st.floats(min_value=-1000, max_value=12000),
)


@FAST
@given(st.lists(dart_rows | st.text(max_size=40) | st.just("#yr mo dy hr mn s T HEIGHT"), max_size=12))
def test_dart_readings_are_sorted_in_range_and_utc(lines):
    readings = ndbc.parse_dart("\n".join(lines), "46402")
    assert [r.ts for r in readings] == sorted(r.ts for r in readings)
    for r in readings:
        assert r.ts.tzinfo is UTC and isinstance(r.ts, datetime)
        assert ndbc.MIN_HEIGHT_M <= r.value_m <= ndbc.MAX_HEIGHT_M
        assert r.station_id == "46402"


@FAST
@given(st.text(max_size=400))
def test_dart_never_raises_on_arbitrary_text(text):
    ndbc.parse_dart(text, "x")


@FAST
@given(alerts, st.lists(numbers | st.none(), min_size=2, max_size=3))
def test_usgs_non_finite_or_out_of_range_position_never_becomes_a_quake(alert, coords):
    feature = {
        "id": "us1",
        "properties": {"mag": 5.0, "time": 1_700_000_000_000, "type": "earthquake", "alert": alert},
        "geometry": {"coordinates": coords},
    }
    (quake,) = parse_collection({"features": [feature]}) or (None,)
    if quake is not None:
        assert math.isfinite(quake.depth_km) and abs(quake.lat) <= 90 and abs(quake.lon) <= 180


@FAST
@given(json_values)
def test_usgs_detail_pager_alert_is_normalised_or_it_raises_what_the_caller_absorbs(alert):
    data = {"id": "us1", "properties": {"products": {"losspager": [{"properties": {"alertlevel": alert}}]}}}
    with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError, IndexError):
        detail = parse_usgs_detail(data)
        assert detail.pager_alert is None or detail.pager_alert in PAGER_LEVELS


@FAST
@given(json_values)
def test_usgs_nearby_cities_is_a_tuple_of_strings_for_any_input(rows):
    cities = parse_nearby_cities(rows)
    assert isinstance(cities, tuple) and all(isinstance(c, str) for c in cities)
