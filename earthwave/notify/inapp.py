"""In-app attention: which rows are flashing, pulsing or waiting to be seen. All timing uses the injected clock."""

from dataclasses import dataclass
from enum import StrEnum

from earthwave.clock import Clock
from earthwave.state.events import Event, QuakeMajor, QuakeNew, QuakeUpdated

NEW_FLASH_SECONDS = 3.0
UPDATED_FLASH_SECONDS = 1.0
MAJOR_PULSE_SECONDS = 60.0


class AttentionKind(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    MAJOR = "major"


_RANK = {AttentionKind.UPDATED: 1, AttentionKind.NEW: 2, AttentionKind.MAJOR: 3}


@dataclass(slots=True)
class Attention:
    kind: AttentionKind
    started: float  # clock.monotonic() when it began
    acked: bool = False


@dataclass(frozen=True, slots=True)
class RowStyle:
    bg: str
    fg: str = "white"
    bold: bool = False


FLASH_NEW = RowStyle("#0891b2", "white", True)
DIM_NEW = RowStyle("#10343d")
FLASH_UPDATED = RowStyle("#a16207", "white", True)
PULSE_MAJOR = RowStyle("#b91c1c", "white", True)
DIM_MAJOR = RowStyle("#5b2a05")


class AttentionTracker:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._items: dict[str, Attention] = {}

    def _elapsed(self, item: Attention) -> float:
        return self._clock.monotonic() - item.started

    def _finished(self, item: Attention) -> bool:
        elapsed = self._elapsed(item)
        if item.kind is AttentionKind.UPDATED:
            return elapsed >= UPDATED_FLASH_SECONDS
        if item.kind is AttentionKind.NEW:
            return item.acked and elapsed >= NEW_FLASH_SECONDS
        return item.acked and elapsed >= MAJOR_PULSE_SECONDS

    def observe(self, events: list[Event]) -> None:
        for event in events:
            if isinstance(event, QuakeNew):
                self._start(event.quake.id, AttentionKind.NEW)
            elif isinstance(event, QuakeUpdated):
                self._start(event.quake.id, AttentionKind.UPDATED)
            elif isinstance(event, QuakeMajor):
                self._start(event.quake.id, AttentionKind.MAJOR)

    def _start(self, quake_id: str, kind: AttentionKind) -> None:
        current = self._items.get(quake_id)
        if current is None or _RANK[kind] >= _RANK[current.kind] or self._finished(current):
            self._items[quake_id] = Attention(kind, self._clock.monotonic())

    def ack(self, quake_id: str) -> None:
        if item := self._items.get(quake_id):
            item.acked = True

    def ack_all(self) -> None:
        for item in self._items.values():
            item.acked = True

    def mark_seen(self, quake_ids: set[str]) -> None:
        """Rows that stayed in the viewport for a whole tick count as seen."""
        for quake_id in quake_ids:
            self.ack(quake_id)

    def unseen(self) -> int:
        return sum(1 for item in self._items.values() if item.kind is not AttentionKind.UPDATED and not item.acked)

    def kind_of(self, quake_id: str) -> AttentionKind | None:
        item = self._items.get(quake_id)
        return item.kind if item else None

    def style_for(self, quake_id: str) -> RowStyle | None:
        item = self._items.get(quake_id)
        if item is None:
            return None
        elapsed = self._elapsed(item)
        if item.kind is AttentionKind.UPDATED:
            return FLASH_UPDATED if elapsed < UPDATED_FLASH_SECONDS / 2 else None
        if item.kind is AttentionKind.NEW:
            if elapsed < NEW_FLASH_SECONDS:
                return FLASH_NEW if elapsed % 1.0 < 0.5 else None
            return None if item.acked else DIM_NEW
        if elapsed < MAJOR_PULSE_SECONDS:
            return PULSE_MAJOR if int(elapsed * 2) % 2 == 0 else DIM_MAJOR
        return None if item.acked else DIM_MAJOR

    def prune(self) -> None:
        for quake_id in [k for k, item in self._items.items() if self._finished(item)]:
            del self._items[quake_id]
