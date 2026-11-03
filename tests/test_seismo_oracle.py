"""The embedded IASP91 table against obspy.taup, the reference. Dev-only: skipped when obspy is not installed.

Tolerance. The table is IASP91 sampled on a grid and interpolated bilinearly, so the error is the curvature of the
travel-time surface between nodes. Measured against taup on a 1.5 degree sweep at depths 33, 100, 250 and 550 km
(P and S): worst 8.1 s, an S time at 84 degrees and 33 km (0.6% of 1366 s); the 15 to 25 degree triplication cells
come out under 1 s. We accept the larger of 4 s and 1% of the travel time, which covers that worst case and is
below the several-second error of a catalogue origin time plus 3-D Earth structure.

The one exception is the Pdiff cutoff: IASP91 carries the diffracted P to 156-158.5 degrees (depth dependent), then
the first arrival jumps about 115 s to PKIKP. Nodes at 154-160 degrees confine the jump to one cell, where a
linear interpolation can be off by up to about 30 s (measured); the test points here stay clear of it.
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
def test_first_s_arrival_matches_taup(model, deg, depth):
    """Taup has an S-type arrival (S, Sdiff, SKS, SKIKS) in every cell, shadow zone included, so the generator's
    1.75 P fallback is never taken; if a taup update ever drops one, this fails and the fallback needs a look."""
    ours_p, ours_s = travel_times(deg * KM_PER_DEGREE, depth)
    theirs = first(model, depth, deg, S_PHASES)
    assert theirs is not None, (deg, depth)
    assert close(ours_s, theirs), (deg, depth, ours_s, theirs)
    assert ours_s >= ours_p


@pytest.mark.parametrize("deg", (110, 130, 150, 170, 180))
def test_in_the_s_shadow_s_is_the_core_phase_and_not_the_early_converted_one(model, deg):
    """Past ~104 degrees PKIKS (P in the mantle) beats SKS; the table follows SKS/SKIKS/Sdiff, as a seismometer's S."""
    _, ours_s = travel_times(deg * KM_PER_DEGREE, 10)
    converted = first(model, 10, deg, ["PKIKS"])
    assert converted is not None and ours_s > converted + 20


@pytest.mark.parametrize("deg", (15, 20, 25, 45, 75, 104, 118, 133, 165))
@pytest.mark.parametrize("depth", (33, 100, 250, 550))
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
