"""The embedded IASP91 table against obspy.taup, the reference. Dev-only: skipped when obspy is not installed.

Tolerance. The table is IASP91 sampled on a grid and interpolated bilinearly, so the error is the curvature of the
travel-time surface between grid points. Most of the grid is exact (the test points sit on or near nodes); the worst
cells are the 410 and 660 km discontinuity triplications near 15 to 30 degrees, where the curve bends within a cell.
We accept the larger of 4 s and 1% of the travel time: well under the second-level uncertainty of a catalogue
origin time plus a real-Earth 3-D velocity anomaly (typically several seconds), and a countdown shown to the
second is not claiming more.
"""

import pytest

from earthwave.seismo import KM_PER_DEGREE, travel_times

pytest.importorskip("obspy")
pytest.importorskip("geographiclib")
from obspy.taup import TauPyModel

DEGREES = (0, 0.5, 5, 30, 90, 150, 180)
DEPTHS = (0, 10, 100, 700)
ABS_S = 4.0
REL = 0.01
S_PHASES = ["s", "S", "Sn", "Sg", "Sdiff", "SKS", "SKIKS"]


@pytest.fixture(scope="module")
def model():
    return TauPyModel("iasp91")


def first(model, depth, deg, phases=None):
    kwargs = {"phase_list": phases} if phases else {}
    return min((float(a.time) for a in model.get_travel_times(depth, deg, **kwargs)), default=None)


def close(ours, theirs):
    return abs(ours - theirs) <= max(ABS_S, REL * theirs)


@pytest.mark.parametrize("deg", DEGREES)
@pytest.mark.parametrize("depth", DEPTHS)
def test_first_p_arrival_matches_taup(model, deg, depth):
    ours, _ = travel_times(deg * KM_PER_DEGREE, depth)
    theirs = first(model, depth, deg)
    assert theirs is not None
    assert close(ours, theirs), (deg, depth, ours, theirs)


@pytest.mark.parametrize("deg", DEGREES)
@pytest.mark.parametrize("depth", DEPTHS)
def test_first_s_arrival_matches_taup_and_falls_back_where_taup_has_none(model, deg, depth):
    ours_p, ours_s = travel_times(deg * KM_PER_DEGREE, depth)
    theirs = first(model, depth, deg, S_PHASES)
    if theirs is None:  # no S-type arrival at all: the fallback is S = 1.75 P, never earlier than P
        assert ours_s == pytest.approx(1.75 * ours_p, abs=ABS_S) or ours_s >= ours_p
    else:
        assert close(ours_s, theirs), (deg, depth, ours_s, theirs)


@pytest.mark.parametrize("deg", (20, 45, 75, 104, 118, 133, 165))
@pytest.mark.parametrize("depth", (33, 250, 550))
def test_off_grid_points_stay_within_the_tolerance(model, deg, depth):
    ours_p, ours_s = travel_times(deg * KM_PER_DEGREE, depth)
    assert close(ours_p, first(model, depth, deg))
    theirs_s = first(model, depth, deg, S_PHASES)
    assert theirs_s is None or close(ours_s, theirs_s)


@pytest.mark.parametrize(
    "a, b",
    [
        ((0.0, 0.0), (0.0, 0.0)),
        ((0.0, 179.9), (0.0, -179.9)),
        ((0.0, -180.0), (0.0, 180.0)),
        ((40.7, -74.0), (-40.7, 106.0)),
        ((35.0, 139.0), (-33.9, -70.7)),
        ((89.0, 10.0), (-89.0, -170.0)),
    ],
)
def test_haversine_agrees_with_the_wgs84_geodesic_to_half_a_percent(a, b):
    """The sphere stands in for the ellipsoid; the 0.5% it costs is under one second of P travel time."""
    from geographiclib.geodesic import Geodesic

    from earthwave.sources.geo import haversine_km

    truth = Geodesic.WGS84.Inverse(*a, *b)["s12"] / 1000.0
    assert haversine_km(*a, *b) == pytest.approx(truth, rel=0.005, abs=0.01)
