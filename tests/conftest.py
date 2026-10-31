import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from earthwave.clock import ReplayClock
from earthwave.models import Earthquake

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def load_json(rel: str) -> dict:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def make_quake(quake_id: str = "q1", mag: float = 4.0, minutes_ago: float = 5, **kwargs) -> Earthquake:
    time = kwargs.pop("time", T0 - timedelta(minutes=minutes_ago))
    defaults = {"lat": 10.0, "lon": 20.0, "depth_km": 10.0, "place": "somewhere", "updated": time}
    return Earthquake(id=quake_id, time=time, mag=mag, **{**defaults, **kwargs})


@pytest.fixture
def clock() -> ReplayClock:
    """A clock that only moves when the test advances it."""
    return ReplayClock(T0, real=lambda: 0.0)
