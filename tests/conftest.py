import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest

from earthwave.clock import ReplayClock
from earthwave.models import Earthquake

FIXTURES = Path(__file__).parent / "fixtures"
# Resident-memory ceiling for one test process (the pytest process, or each xdist worker). A runaway test then dies
# on every platform, Windows included, where resource.setrlimit does not exist. Override with EARTHWAVE_TEST_RSS_MB.
RSS_LIMIT_MB = int(os.environ.get("EARTHWAVE_TEST_RSS_MB", "2048"))
RSS_EXIT_CODE = 70
T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def load_json(rel: str) -> dict:
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))


def make_quake(quake_id: str = "q1", mag: float = 4.0, minutes_ago: float = 5, **kwargs) -> Earthquake:
    time = kwargs.pop("time", T0 - timedelta(minutes=minutes_ago))
    defaults = {"lat": 10.0, "lon": 20.0, "depth_km": 10.0, "place": "somewhere", "updated": time}
    return Earthquake(id=quake_id, time=time, mag=mag, **{**defaults, **kwargs})


async def wait_until(pilot, predicate, timeout: float = 2.0, what: str = "condition") -> None:
    """Poll `predicate()` between frames; the footer and other late-composed widgets fill in a few frames after a
    screen appears, so a fixed pause is a race."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause(0.02)


async def footer_keys(pilot, timeout: float = 2.0) -> list:
    """The current screen's footer keys once the footer has composed and been laid out."""
    from textual.widgets._footer import FooterKey

    def ready() -> bool:
        keys = list(pilot.app.screen.query(FooterKey))
        return bool(keys) and all(k.region.width > 0 for k in keys)

    await wait_until(pilot, ready, timeout, "the footer")
    return list(pilot.app.screen.query(FooterKey))


@pytest.fixture
def clock() -> ReplayClock:
    """A clock that only moves when the test advances it."""
    return ReplayClock(T0, real=lambda: 0.0)


def watch_rss(limit_bytes: int, interval: float = 0.25, die=os._exit) -> threading.Thread:
    """Start a daemon thread that ends this process (`die`) once its resident set passes `limit_bytes`.

    The process is killed rather than the test failed: a test that is allocating without bound is not
    interruptible from a thread, and under xdist the controller reports the lost worker as a failed run."""
    proc = psutil.Process()

    def loop() -> None:
        while True:
            rss = proc.memory_info().rss
            if rss > limit_bytes:
                sys.stderr.write(f"\nFATAL: test process using {rss >> 20} MB, over the {limit_bytes >> 20} MB limit\n")
                sys.stderr.flush()
                die(RSS_EXIT_CODE)
                return
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="rss-watchdog", daemon=True)
    thread.start()
    return thread


@pytest.fixture(scope="session", autouse=True)
def _memory_limit():
    """Applies to every process that runs tests: the plain pytest process and each xdist worker."""
    watch_rss(RSS_LIMIT_MB << 20)
    yield
