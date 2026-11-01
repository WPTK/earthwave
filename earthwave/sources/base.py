"""The contract every data source implements."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from earthwave.models import Batch

ENTRY_BUDGET = 90.0  # seconds a multi-request source spends on a poll's entries; poll.FETCH_DEADLINE (120) outlasts it


@dataclass(slots=True)
class SourceHealth:
    """What a source may look at when choosing its next interval."""

    failures: int = 0
    last_ok: datetime | None = None
    incomplete: bool = False  # the newest good poll skipped something
    last_complete: datetime | None = None  # start of the newest poll that skipped nothing
    last_error: str | None = None
    polls: int = 0


class Source(Protocol):
    name: str
    min_interval: float  # seconds; the scheduler never polls this source faster

    def interval(self, health: SourceHealth) -> float:
        """Seconds to wait after a successful fetch (0 = poll again immediately)."""
        ...

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        """Fetch one batch. `since` is the `cursor` of this source's previous batch. Raise on any failure."""
        ...
