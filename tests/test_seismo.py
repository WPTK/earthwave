"""Energy and travel-time arithmetic: properties and edge cases that need no obspy."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from earthwave.seismo import (
    ENERGY_PER_MAGNITUDE_STEP,
    KM_PER_DEGREE,
    arrivals,
    energy_joules,
    energy_ratio,
    fmt_tnt,
    tnt_tons,
    travel_times,
)
from earthwave.sources.geo import haversine_km

ANTIPODE_KM = math.pi * 6371.0088
distances = st.floats(0, ANTIPODE_KM, allow_nan=False)
depths = st.floats(0, 700, allow_nan=False)


def test_one_magnitude_step_is_exactly_ten_to_the_one_and_a_half():
    assert energy_ratio(4.0, 5.0) == 10**1.5 == ENERGY_PER_MAGNITUDE_STEP
    assert f"{energy_ratio(6.0, 7.0):.1f}" == "31.6"
    assert energy_ratio(5.0, 5.0) == 1.0
    assert energy_ratio(7.0, 6.0) == pytest.approx(1 / 31.6227766)


@given(st.floats(-2, 10, allow_nan=False), st.floats(-2, 10, allow_nan=False))
def test_energy_ratio_matches_the_energy_formula(m1, m2):
    assert energy_ratio(m1, m2) == pytest.approx(energy_joules(m2) / energy_joules(m1), rel=1e-9)


def test_energy_anchors():
    assert energy_joules(0) == pytest.approx(10**4.8)
    assert energy_joules(9.5) == pytest.approx(1.122e19, rel=0.01)  # 10^19.05
    assert tnt_tons(7.0) == pytest.approx(4.77e5, rel=0.01)  # about 480 kt


def test_tnt_text_picks_a_readable_unit():
    assert fmt_tnt(0.0005) == "0.5 kg"
    assert fmt_tnt(0.02) == "20 kg"
    assert fmt_tnt(3.0) == "3.0 t"
    assert fmt_tnt(4.77e5) == "477 kt"
    assert fmt_tnt(1.2e6) == "1.2 Mt"
    assert fmt_tnt(3e10) == "30 Gt"


def test_an_event_at_the_station_with_no_depth_arrives_at_the_origin_time():
    assert travel_times(0.0, 0.0) == (0.0, 0.0)


def test_a_deep_event_straight_below_still_takes_time():
    p, s = travel_times(0.0, 700.0)
    assert 70 < p < 90 and p < s < 160  # IASP91: about 80 s and 145 s


def test_the_antipode_is_reached_in_about_twenty_minutes():
    p, s = travel_times(ANTIPODE_KM, 10.0)
    assert 1200 < p < 1220 and 1600 < s < 1650  # PKIKP and SKIKS


def test_out_of_range_inputs_clamp_to_the_table_edges():
    assert travel_times(-5.0, -5.0) == travel_times(0.0, 0.0)
    assert travel_times(1e6, 700.0) == travel_times(180 * KM_PER_DEGREE, 700.0)
    assert travel_times(100.0, 5000.0) == travel_times(100.0, 700.0)


@pytest.mark.parametrize("lon_a, lon_b", [(179.9, -179.9), (-180.0, 180.0), (180.0, -180.0), (0.0, 180.0)])
def test_longitudes_either_side_of_the_dateline_are_near_and_the_antipode_is_far(lon_a, lon_b):
    km = haversine_km(0.0, lon_a, 0.0, lon_b)
    p, _ = travel_times(km, 10.0)
    assert (p < 60) if km < 100 else (p > 700)


def test_the_antipode_by_haversine_gets_the_antipode_time():
    km = haversine_km(45.0, 10.0, -45.0, -170.0)
    assert km == pytest.approx(ANTIPODE_KM, rel=1e-9)
    assert travel_times(km, 33.0)[0] == pytest.approx(travel_times(ANTIPODE_KM, 33.0)[0], abs=0.01)


def test_arrival_is_origin_plus_travel_time():
    origin = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    p, s = travel_times(500.0, 20.0)
    assert arrivals(500.0, 20.0, origin) == (origin + timedelta(seconds=p), origin + timedelta(seconds=s))


@given(distances, depths)
def test_p_comes_before_s_and_both_are_finite_and_not_negative(km, depth):
    p, s = travel_times(km, depth)
    assert math.isfinite(p) and math.isfinite(s) and 0 <= p <= s
    assert p < s or (km == 0 and depth == 0)


@given(distances, distances, depths)
def test_travel_time_never_decreases_with_distance(a, b, depth):
    near, far = sorted((a, b))
    p_near, s_near = travel_times(near, depth)
    p_far, s_far = travel_times(far, depth)
    assert p_near <= p_far + 1e-9 and s_near <= s_far + 1e-9
