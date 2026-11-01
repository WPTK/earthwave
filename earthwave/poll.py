"""Multi-source scheduler: per-source intervals, exponential backoff, freshness, and a generation counter."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch
from earthwave.sources.base import Source, SourceHealth
from earthwave.state.events import Event, SourceDegraded, SourceRecovered
from earthwave.state.store import Store

log = logging.getLogger(__name__)

BACKOFF_CAP = 600.0
BACKOFF_FLOOR = 30.0  # base delay for sources whose own interval is 0 (startup chain)
FETCH_DEADLINE = 120.0  # one poll's fetch and merge, end to end: a server that trickles bytes cannot stall a source
JITTER = 0.1
STALE_FACTOR = 2.5
TICK = 0.5  # longest the loop sleeps, so refresh requests and countdowns stay responsive


class Freshness(StrEnum):
    LOADING = "loading"
    FRESH = "fresh"
    STALE = "stale"
    ERROR = "error"


@dataclass(slots=True)
class SourceStatus:
    health: SourceHealth = field(default_factory=SourceHealth)
    interval: float = 0.0  # last scheduled steady interval, seconds
    next_due: float = 0.0  # clock.monotonic()
    last_poll: float | None = None  # clock.monotonic() of the last attempt
    in_flight: bool = False
    nudged_due: float | None = None  # a reschedule made mid-fetch; the fetch must not undo it


@dataclass(frozen=True, slots=True)
class PollResult:
    source: str
    generation: int
    events: tuple[Event, ...] = ()
    batch: Batch | None = None
    error: str | None = None


def describe(exc: Exception) -> str:
    """An error as the status bar shows it; the deadline's TimeoutError has no text of its own."""
    if isinstance(exc, TimeoutError):
        return f"no answer within {FETCH_DEADLINE:g} s"
    return f"{type(exc).__name__}: {exc}"[:200]


def backoff_delay(interval: float, failures: int, rng: random.Random) -> float:
    """interval x 2 per consecutive failure, capped at 10 minutes, with +-10% jitter."""
    base = max(interval, BACKOFF_FLOOR)
    delay = min(base * 2.0 ** min(failures, 32), BACKOFF_CAP)  # clamped: 2**1024 overflows a float
    return delay * (1 + rng.uniform(-JITTER, JITTER))


def freshness_of(health: SourceHealth, now: datetime, interval: float) -> Freshness:
    if health.last_ok is None:
        return Freshness.ERROR if health.failures else Freshness.LOADING
    if (now - health.last_ok).total_seconds() > STALE_FACTOR * interval:
        return Freshness.STALE
    return Freshness.ERROR if health.failures else Freshness.FRESH


class Scheduler:
    def __init__(
        self,
        sources: Sequence[Source],
        store: Store,
        clock: Clock,
        on_done: Callable[[PollResult], None],
        rng: random.Random | None = None,
    ):
        self._sources = {s.name: s for s in sources}
        self._store = store
        self._clock = clock
        self._on_done = on_done
        self._rng = rng or random.Random()
        self._cursors: dict[str, str | None] = {}
        self._tasks: set[asyncio.Task] = set()
        self.statuses = {s.name: SourceStatus(interval=s.min_interval) for s in sources}
        self.generation = 0

    # --- state the UI reads -------------------------------------------------------------------------------------

    def freshness(self, name: str) -> Freshness:
        status = self.statuses[name]
        interval = max(status.interval, self._sources[name].min_interval)
        return freshness_of(status.health, self._clock.now(), interval)

    def age(self, name: str) -> float | None:
        last_ok = self.statuses[name].health.last_ok
        return None if last_ok is None else (self._clock.now() - last_ok).total_seconds()

    def answered_since(self, name: str, when: datetime) -> bool:
        """True when a poll that skipped nothing began at or after `when`; an unknown source is never in doubt."""
        status = self.statuses.get(name)
        return status is None or (status.health.last_complete is not None and status.health.last_complete >= when)

    def next_poll_in(self, name: str) -> float:
        return max(0.0, self.statuses[name].next_due - self._clock.monotonic())

    # --- control ------------------------------------------------------------------------------------------------

    def switch(self) -> int:
        """The view changed: older generations' backfill results are dropped. Returns the new generation."""
        self.generation += 1
        return self.generation

    def refresh_now(self) -> None:
        """Poll every source as soon as its minimum interval allows."""
        now = self._clock.monotonic()
        for name, status in self.statuses.items():
            if status.last_poll is None:
                status.next_due = 0.0
            else:
                earliest = max(now, status.last_poll + self._sources[name].min_interval)
                status.next_due = min(status.next_due, earliest)

    def reschedule(self, name: str, immediate: bool = False) -> None:
        """A source's cadence or selection changed: poll it again as soon as its new interval (or `immediate`) allows.

        Only ever moves the next poll earlier, never into a backoff, and never below the source's minimum interval.
        """
        status, source = self.statuses[name], self._sources[name]
        if status.last_poll is None or status.health.failures:
            return
        target = self._clock.monotonic() if immediate else status.last_poll + source.interval(status.health)
        due = max(target, status.last_poll + source.min_interval)
        status.next_due = min(status.next_due, due)
        if status.in_flight:
            status.nudged_due = due if status.nudged_due is None else min(status.nudged_due, due)

    def due(self) -> list[str]:
        now = self._clock.monotonic()
        return [name for name, status in self.statuses.items() if status.next_due <= now]

    # --- polling ------------------------------------------------------------------------------------------------

    async def poll_source(self, name: str, client: httpx.AsyncClient) -> PollResult:
        """One fetch + merge for one source. Never raises: a failure becomes a PollResult with `error` set."""
        source, status = self._sources[name], self.statuses[name]
        generation = self.generation
        status.last_poll = self._clock.monotonic()
        status.in_flight, status.nudged_due = True, None
        status.health.polls += 1
        started = self._clock.now()
        events: list[Event] = []
        batch = None
        error = None
        try:
            async with asyncio.timeout(FETCH_DEADLINE):
                batch = await source.fetch(client, self._cursors.get(name))
            events.extend(self._store.merge(batch, self._clock))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = describe(exc)
            log.warning("%s: poll failed: %s", name, error)
        health = status.health
        if error is None:
            if health.failures:
                events.append(SourceRecovered(name))
            health.failures = 0
            health.last_ok = self._clock.now()
            health.incomplete = bool(batch is not None and batch.meta.get("incomplete"))
            if batch is not None and not health.incomplete:
                health.last_complete = started
            health.last_error = None
            if batch is not None and batch.cursor is not None:
                self._cursors[name] = batch.cursor
            status.interval = max(source.interval(health), 0.0)
            status.next_due = self._clock.monotonic() + status.interval
            if status.nudged_due is not None:
                status.next_due = min(status.next_due, status.nudged_due)
        else:
            health.failures += 1
            health.last_error = error
            if health.failures == 1:
                events.append(SourceDegraded(name, error))
            delay = backoff_delay(source.interval(health), health.failures, self._rng)
            status.next_due = self._clock.monotonic() + delay
        status.in_flight = False
        result = PollResult(name, generation, tuple(events), batch, error)
        self._on_done(result)
        return result

    def submit_backfill(
        self, source: str, client: httpx.AsyncClient, job: Callable[[httpx.AsyncClient], Awaitable[Batch]]
    ) -> asyncio.Task:
        """Run a one-off query (an fdsnws backfill); its result is dropped if the generation moved on meanwhile."""
        generation = self.generation

        async def run() -> None:
            try:
                async with asyncio.timeout(FETCH_DEADLINE):
                    batch = await job(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: backfill failed: %r", source, exc)
                self._on_done(PollResult(source, generation, error=describe(exc)))
                return
            if generation != self.generation:
                log.debug("%s: dropping backfill from generation %d (now %d)", source, generation, self.generation)
                return
            events = self._store.merge(batch, self._clock)
            self._on_done(PollResult(source, generation, tuple(events), batch))

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(self, client: httpx.AsyncClient) -> None:
        """Poll forever; each source has its own task, so a slow or failing one never delays the others."""
        running: dict[str, asyncio.Task] = {}
        self._store.answered_since = self.answered_since  # an alert past its expiry now needs a fresh answer
        try:
            while True:
                for name in self.due():
                    if name not in running:
                        running[name] = asyncio.get_running_loop().create_task(self.poll_source(name, client))
                for name in [n for n, t in running.items() if t.done()]:
                    del running[name]
                now = self._clock.monotonic()
                wait = min(
                    (s.next_due - now for n, s in self.statuses.items() if n not in running),
                    default=TICK,
                )
                await self._clock.sleep(min(max(wait, 0.05), TICK))
        finally:
            for task in [*running.values(), *self._tasks]:
                task.cancel()
