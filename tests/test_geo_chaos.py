"""bearing_deg stays in [0, 360) at the poles and antipodes (#291)."""

import math

import pytest

from earthwave.sources.geo import bearing_deg


@pytest.mark.parametrize(
    "p", [(0, 0, 0, 0), (90, 0, 90, 180), (-90, 5, 90, 5), (0, 0, 0, 180), (10, 20, -10, -160), (45, 180, 45, -180)]
)
def test_bearing_is_in_half_open_range(p):
    b = bearing_deg(*p)
    assert math.isfinite(b) and 0.0 <= b < 360.0
