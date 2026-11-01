"""Staged app states for the UAT matrix and the walkthrough: first run, offline, loading, stale, empty, quiet, busy,
error and replay. Feed health is staged on the scheduler directly (no network, no timers), so every state is exact."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from test_ui import WEEK, mock_transport

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch, Earthquake
from earthwave.sources.usgs import parse_collection
from earthwave.state.store import Store
from uat.harness import WEEK_NOW, kam_alert_app

QUIET_TIMES = (35, 190, 700)  # minutes ago


class FakeHistory:
    """Stands in for the sqlite history: the day screen reads `day()`, nothing is written."""

    def __init__(self, quakes: list[Earthquake] | tuple[Earthquake, ...] = ()):
        self._quakes = list(quakes)
        self.path = "fake-history"

    def day(self, start, end):
        return [q for q in self._quakes if start <= q.time < end], Counter()

    def load_since(self, since):
        return [q for q in self._quakes if q.time >= since], set()

    def start(self): ...
    def record(self, *args, **kwargs): ...
    def mark_seen(self, ids): ...
    def close_now(self): ...

    async def close(self):
        await asyncio.sleep(0)


def week_quakes() -> list[Earthquake]:
    return list(parse_collection(WEEK))


def quiet_quakes() -> list[Earthquake]:
    base = week_quakes()[0]
    return [
        replace(base, id=f"quiet{i}", mag=2.5 + i * 0.3, place=f"{10 + i} km N of Quietville", time=WEEK_NOW - timedelta(minutes=m), updated=WEEK_NOW - timedelta(minutes=m), tsunami_flag=False, pager_alert=None)
        for i, m in enumerate(QUIET_TIMES)
    ]  # fmt: skip


def _config() -> Config:
    return with_overrides(Config(), 2.5, 168, (40.7, -74.0), "unicode")


def build(state: str) -> EarthwaveApp:
    """The app for a named state. Health is applied by `stage()` once the app runs."""
    clock = ReplayClock(WEEK_NOW, real=lambda: 0.0)
    config = _config()
    quakes: list[Earthquake] = []
    kwargs: dict = {}
    if state in ("loading", "stale", "busy", "offline"):
        quakes = week_quakes()
    elif state == "quiet":
        quakes = quiet_quakes()
    if state == "replay":
        app = kam_alert_app(0, 1, transport=mock_transport([]))
        app.history = FakeHistory()
        return app
    store = Store(config.alerts, config.home)
    history = FakeHistory(quakes if state == "offline" else ())
    if state != "offline":
        if quakes:
            store.merge(Batch("usgs", WEEK_NOW, quakes=tuple(quakes), backfill=True, coverage=(168.0, 2.5)), clock)
        if state in ("busy", "quiet", "empty", "stale", "loading"):
            store.merge(Batch("usgs", WEEK_NOW, quakes=()), clock)  # a live batch has arrived: the store is warm
    else:
        kwargs = {"offline": True, "history": history}
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False, transport=mock_transport([]), **kwargs)
    if app.history is None:
        app.history = history
    return app  # fmt: skip


HEALTH: dict[str, str] = {
    "first_run": "loading",
    "offline": "none",
    "loading": "loading",
    "stale": "stale",
    "empty": "fresh",
    "quiet": "fresh",
    "busy": "fresh",
    "error": "error",
    "replay": "fresh",
}
STATES = tuple(HEALTH)


def stage(app: EarthwaveApp, state: str) -> None:
    """Put every feed into the state's health and redraw. Call once the app is running."""
    mode = HEALTH[state]
    if mode == "none":
        return
    app.scheduler_running = True
    now = app.clock.now()
    for status in app.scheduler.statuses.values():
        health = status.health
        health.failures, health.last_ok, health.incomplete, health.last_error = 0, None, False, None
        if mode == "fresh":
            health.last_ok, health.last_complete, health.polls = now, now, 1
        elif mode == "stale":
            health.last_ok, health.last_complete, health.polls = now - timedelta(hours=6), now - timedelta(hours=6), 1
        elif mode == "error":
            health.failures, health.last_error, health.polls = 3, "ConnectError: boom", 3
    app.dashboard.refresh_view()


Builder = Callable[[], EarthwaveApp]
