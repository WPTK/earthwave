"""Injectable time: every timestamp and delay in the app goes through a Clock so replay and tests control it."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Aware UTC wall-clock time."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ReplayClock:
    """Starts at a recorded time, runs `speed` times faster than real time and can be pushed forward to a timestamp.

    `real` is the real-time source; tests pass a constant so only `advance`/`advance_to` move the clock.
    """

    def __init__(self, start: datetime, speed: float = 1.0, real: Callable[[], float] = time.monotonic):
        if speed <= 0:
            raise ValueError("speed must be positive")
        self._origin = start
        self._base = start
        self._speed = speed
        self._real = real
        self._anchor = real()

    def now(self) -> datetime:
        return self._base + timedelta(seconds=(self._real() - self._anchor) * self._speed)

    def monotonic(self) -> float:
        return (self.now() - self._origin).total_seconds()

    def advance_to(self, ts: datetime) -> None:
        """Jump forward to `ts`; a timestamp in the past leaves the clock alone."""
        current = self.now()
        if ts > current:
            self._base += ts - current

    def advance(self, seconds: float) -> None:
        self._base += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self._speed)
