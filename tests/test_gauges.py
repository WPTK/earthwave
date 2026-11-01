import json
import math
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from earthwave.sources import coops, ndbc

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def dart(name: str) -> list[ndbc.GaugeReading]:
    return ndbc.parse_dart((FIX / "ndbc" / name).read_text(), name.split(".")[0])


# ---- NDBC -----------------------------------------------------------------------------------


def test_active_stations_dart_only():
    stations = ndbc.parse_active_stations((FIX / "ndbc" / "activestations.xml").read_bytes())
    assert len(stations) == 76
    by_id = {s.id: s for s in stations}
    assert by_id["46402"].kind == "dart"
    assert by_id["21414"].name.startswith("AMCHITKA")  # leading space stripped
    assert all(-90 <= s.lat <= 90 and -180 <= s.lon <= 360 for s in stations)


def test_active_stations_skips_missing_coordinates():
    xml = '<stations><station id="1" type="dart" name="a"/><station id="2" type="dart" lat="1" lon="2" name="b"/></stations>'
    assert [s.id for s in ndbc.parse_active_stations(xml)] == ["2"]


def test_parse_standard_dart_fixture():
    rows = dart("46402.dart")
    assert len(rows) > 4000
    assert {r.mode for r in rows} == {"15m"}
    assert rows == sorted(rows, key=lambda r: r.ts)
    assert rows[-1].ts == datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    assert rows[-1].value_m == pytest.approx(4717.609)
    assert not ndbc.in_event_mode(rows)


def test_parse_event_mode_fixture():
    rows = dart("32401.dart")
    assert {r.mode for r in rows} == {"15m", "1m", "15s"}
    assert any(r.mode == "15s" for r in rows)
    # The captured event ended weeks before the newest row, so the station is back to standard mode.
    assert not ndbc.in_event_mode(rows)
    event_end = max(r.ts for r in rows if r.mode != "15m")
    assert ndbc.in_event_mode([r for r in rows if r.ts <= event_end + timedelta(minutes=5)])


def test_parser_edge_cases():
    text = "\n".join(
        [
            "#YY  MM DD hh mm ss T   HEIGHT",
            "#yr  mo dy hr mn  s -        m",
            "",
            "2026 10 31 00 15 00 1 4717.700",
            "2026 10 31 00 00 00 1 99.999",  # missing marker
            "2026 10 30 23 45 00 7 4717.500",  # unknown T
            "2026 10 30 23 30 00 1 notnum",
            "2026 13 30 23 30 00 1 4717.500",  # impossible month
            "2026 10 30 23 15",  # short row
            "   ",
            "2026 10 30 23 00 00 2 4717.400",
        ]
    )
    rows = ndbc.parse_dart(text, "X")
    assert [(r.ts.hour, r.ts.minute, r.mode) for r in rows] == [(23, 0, "1m"), (0, 15, "15m")]
    assert ndbc.parse_dart("", "X") == []
    assert not ndbc.in_event_mode([])


@pytest.mark.parametrize("bad", ["9999.000", "999.999", "99.999", "0.000", "-12.500", "12000.000", "nan", "inf"])
def test_missing_markers_and_implausible_heights_are_dropped(bad):
    text = chr(10).join(
        ["2026 10 31 00 00 00 1 4717.700", f"2026 10 31 00 15 00 1 {bad}", "2026 10 31 00 30 00 1 4717.800"]
    )
    assert [r.value_m for r in ndbc.parse_dart(text, "X")] == [4717.7, 4717.8]


def tide(t: datetime) -> float:
    s = t.timestamp()
    return 4700 + 0.5 * math.sin(2 * math.pi * s / (12.42 * 3600)) + 0.2 * math.sin(2 * math.pi * s / (12 * 3600) + 1)


def synthetic(
    minutes: int, step: int, pulse_at: datetime | None = None, amp: float = 0.10, minutes_long: int = 30
) -> list[ndbc.GaugeReading]:
    start = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    out = []
    for i in range(0, minutes + 1, step):
        t = start + timedelta(minutes=i)
        v = tide(t)
        if pulse_at is not None and timedelta(0) <= t - pulse_at < timedelta(minutes=minutes_long):
            v += amp
        out.append(ndbc.GaugeReading("T", "dart", t, v, "15m" if step == 15 else "1m"))
    return out


def test_detrend_flat_on_pure_tide_and_finds_pulse():
    pulse_at = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    resid = ndbc.detrend(synthetic(12 * 60, 1, pulse_at, minutes_long=30))
    assert resid[0].ts >= datetime(2026, 10, 31, 0, 8, tzinfo=UTC)
    assert max(abs(r.residual_m) for r in resid if r.ts < pulse_at) < 0.005
    # A step arriving at 09:00 shows at its full 10 cm on the first sample; the fit then absorbs it.
    assert next(r.residual_m for r in resid if r.ts == pulse_at) == pytest.approx(0.10, abs=0.01)


def test_detrend_fifteen_minute_cadence_and_since():
    pulse_at = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    resid = ndbc.detrend(synthetic(12 * 60, 15, pulse_at), since=pulse_at - timedelta(minutes=30))
    assert resid[0].ts == pulse_at - timedelta(minutes=30)
    assert next(r for r in resid if r.ts == pulse_at).residual_m == pytest.approx(0.10, abs=0.02)


def test_detrend_needs_history():
    rows = synthetic(60, 15)
    assert ndbc.detrend(rows[:5]) == []
    assert ndbc.detrend([]) == []


def test_detrend_real_fixture_is_small():
    rows = dart("46402.dart")[-200:]
    resid = ndbc.detrend(rows, since=rows[-50].ts)
    assert resid
    assert max(abs(r.residual_m) for r in resid) < 0.02
    # 14:15 follows a 75-minute hole in the batch data and is dropped rather than extrapolated.
    assert datetime(2026, 10, 30, 14, 15, tzinfo=UTC) not in {r.ts for r in resid}


# ---- CO-OPS ---------------------------------------------------------------------------------


def load(name: str) -> dict:
    return json.loads((FIX / "coops" / name).read_text())


def test_stations_full():
    stations = coops.parse_stations((FIX / "coops" / "stations.json").read_text())
    assert len(stations) > 250
    by_id = {s.id: s for s in stations}
    assert by_id["9414290"].name == "San Francisco"
    assert by_id["9414290"].lon == pytest.approx(-122.4659, abs=1e-3)
    assert all(s.kind == "coops" for s in stations)
    assert coops.parse_stations({"stations": [{"id": "1"}, {"id": "2", "lat": "x", "lng": 1}]}) == []


def test_water_level_fixture():
    rows = coops.parse_water_level(load("water_level_9414290.json"))
    assert len(rows) == 59
    assert rows[0].station_id == "9414290"
    assert rows[0].ts == datetime(2026, 10, 31, 5, 54, tzinfo=UTC)
    assert {r.mode for r in rows} == {"6m"}


def test_water_level_missing_and_quality():
    payload = {
        "metadata": {"id": "1"},
        "data": [
            {"t": "2026-10-31 00:06", "v": "0.2", "s": "0.0", "f": "0,0,0,0", "q": "v"},
            {"t": "2026-10-31 00:00", "v": "0.1", "s": "0.0", "f": "0,0,0,0", "q": "p"},
            {"t": "2026-10-31 00:12", "v": "", "s": "", "f": "1,1,1,1", "q": "p"},
            {"t": "2026-10-31 00:18", "v": "0.4", "s": "0.0", "f": "0,0,0,0", "q": "x"},
            {"t": "bad", "v": "0.5", "q": "p"},
        ],
    }
    rows = coops.parse_water_level(payload)
    assert [r.value_m for r in rows] == [0.1, 0.2]


def test_one_minute_and_predictions_fixtures():
    obs = coops.parse_one_minute_water_level(load("one_minute_water_level_9414290.json"))
    assert len(obs) > 100 and {r.mode for r in obs} == {"1m"}
    pred6 = coops.parse_predictions(load("predictions_9414290.json"), "9414290")
    pred1 = coops.parse_predictions(load("predictions_1min_9414290.json"), "9414290")
    assert pred6[1].ts - pred6[0].ts == timedelta(minutes=6)
    assert pred1[1].ts - pred1[0].ts == timedelta(minutes=1)


def test_residual_matches_timestamps_only():
    obs = coops.parse_water_level(load("water_level_9414290.json"))
    pred = coops.parse_predictions(load("predictions_9414290.json"), "9414290")
    res = coops.residuals(obs, pred)
    assert res and len(res) < len(obs) + 1
    p = {x.ts: x.value_m for x in pred}
    for r in res:
        assert r.ts in p
        assert r.residual_m == pytest.approx(r.observed_m - p[r.ts])
    assert max(abs(r.residual_m) for r in res) < 1.0  # sane: a quiet day is nowhere near a metre off


def test_residual_injected_pulse():
    t0 = datetime(2026, 10, 31, tzinfo=UTC)
    pred = [coops.Prediction("S", t0 + timedelta(minutes=6 * i), 1.0) for i in range(5)]
    obs = [coops.GaugeReading("S", "coops", p.ts, 1.0 + (0.1 if i == 2 else 0), "6m") for i, p in enumerate(pred)]
    obs.append(coops.GaugeReading("S", "coops", t0 + timedelta(minutes=3), 9.0, "6m"))  # no prediction
    res = coops.residuals(obs, pred)
    assert [round(r.residual_m, 3) for r in res] == [0, 0, 0.1, 0, 0]


def test_baseline_removal_drops_a_steady_offset_and_keeps_a_pulse():
    t0 = datetime(2026, 10, 31, tzinfo=UTC)
    pred = [coops.Prediction("S", t0 + timedelta(minutes=6 * i), 1.0) for i in range(60)]
    obs = [coops.GaugeReading("S", "coops", p.ts, 1.4 + (0.5 if i == 50 else 0), "6m") for i, p in enumerate(pred)]
    out = coops.remove_baseline(coops.residuals(obs, pred))
    assert out[0].ts >= t0 + coops.BASELINE_LAG  # no baseline, no residual, until there is an hour of history
    assert all(abs(r.residual_m) < 1e-9 for r in out if r.ts != pred[50].ts)
    pulse = next(r for r in out if r.ts == pred[50].ts)
    assert pulse.residual_m == pytest.approx(0.5) and pulse.observed_m - pulse.predicted_m == pytest.approx(0.5)


# ---- snapshot script ------------------------------------------------------------------------


def test_snapshot_script_matches_bundled_json(tmp_path):
    out = tmp_path / "d.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "snapshot_stations.py"), "--out", str(out)], check=True)
    data = json.loads(out.read_text())
    assert len(data["stations"]) == 76
    for st in data["stations"]:
        assert {
            "id",
            "kind",
            "name",
            "lat",
            "lon",
            "owner",
            "pgm",
            "type",
            "met",
            "currents",
            "waterquality",
            "dart",
        } <= set(st)
        assert isinstance(st["lat"], float) and isinstance(st["lon"], float)
    bundled = ROOT / "earthwave" / "data" / "dart_stations.json"
    assert json.loads(bundled.read_text()) == data


def test_snapshot_build_skips_rows_without_coordinates():
    sys.path.insert(0, str(ROOT / "scripts"))
    import snapshot_stations

    xml = b'<stations created="x"><station id="1" type="dart" name="a"/><station id="2" type="dart" lat="1" lon="2"/></stations>'
    assert [s["id"] for s in snapshot_stations.build(xml)["stations"]] == ["2"]


# ---- detrend guards -------------------------------------------------------------------------


def series(minutes: list[int], value=lambda m: 4700.0) -> list[ndbc.GaugeReading]:
    t0 = datetime(2026, 10, 31, tzinfo=UTC)
    return [ndbc.GaugeReading("T", "dart", t0 + timedelta(minutes=m), value(m), "15m") for m in minutes]


def test_detrend_eight_point_boundary():
    rows = series(list(range(0, 15 * 9, 15)))  # 9 readings: the last has exactly 8 prior, spanning 1h45
    assert [r.ts for r in ndbc.detrend(rows)] == [rows[-1].ts]
    assert ndbc.detrend(rows[:-1]) == []  # 7 prior


def test_detrend_needs_time_span():
    rows = series(list(range(0, 60)))  # plenty of points, only an hour of them
    assert ndbc.detrend(rows) == []
    assert ndbc.detrend(series(list(range(0, 91)))) != []  # 90 minutes of history is enough


def test_detrend_duplicate_timestamps_do_not_blow_up():
    rows = series(list(range(0, 200, 5)), lambda m: 4700 + 0.001 * m)
    dup = rows + [r._replace(value_m=r.value_m + 0.02) for r in rows[10:30]]
    res = ndbc.detrend(dup)
    assert res and max(abs(r.residual_m) for r in res) < 0.05
    assert len({r.ts for r in res}) == len(res)


def test_detrend_hole_mid_window():
    minutes = [m for m in range(0, 331) if not 100 < m < 132]  # 32-minute gap, 31 minutes with no samples
    res = ndbc.detrend(series(minutes))
    ts = {r.ts for r in res}
    t0 = datetime(2026, 10, 31, tzinfo=UTC)
    assert t0 + timedelta(minutes=99) in ts  # before the hole
    assert t0 + timedelta(minutes=132) not in ts  # first point after it
    assert t0 + timedelta(minutes=200) not in ts  # hole still inside the prior 3 h
    assert t0 + timedelta(minutes=330) in ts  # hole has left the window


def test_coops_one_minute_residual_against_one_minute_predictions():
    obs = coops.parse_one_minute_water_level(load("one_minute_water_level_9414290.json"))
    pred = coops.parse_predictions(load("predictions_1min_9414290.json"), "9414290")
    res = coops.residuals(obs, pred)
    assert len(res) >= 3
    assert {r.ts for r in res} <= {o.ts for o in obs} & {p.ts for p in pred}
    assert max(abs(r.residual_m) for r in res) < 1.0


def test_coops_error_payload_raises():
    err = {"error": {"message": "No data was found. This product may not be offered at this station."}}
    for fn in (coops.parse_water_level, coops.parse_one_minute_water_level):
        with pytest.raises(coops.CoopsError, match="No data"):
            fn(err)
    with pytest.raises(coops.CoopsError):
        coops.parse_predictions(json.dumps(err), "1")
