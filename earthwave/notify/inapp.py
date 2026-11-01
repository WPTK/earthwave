"""In-app attention: which rows are flashing, pulsing or waiting to be seen. All timing uses the injected clock."""

from dataclasses import dataclass
from enum import StrEnum

from earthwave.clock import Clock
from earthwave.sources import levels
from earthwave.state.events import (
    Event,
    GaugeAnomaly,
    QuakeMajor,
    QuakeNew,
    QuakeRenamed,
    QuakeUpdated,
    TsunamiCancelled,
    TsunamiChanged,
    TsunamiIssued,
)

NEW_FLASH_SECONDS = 3.0
UPDATED_FLASH_SECONDS = 1.0
MAJOR_PULSE_SECONDS = 60.0
PULSE_COOLDOWN_SECONDS = 120.0
TSUNAMI_PREFIX = "tsunami:"
GAUGE_PREFIX = "gauge:"
GAUGE_SECONDS = 300.0  # a gauge anomaly stays on the ticker this long unless acknowledged


class AttentionKind(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    MAJOR = "major"
    TSUNAMI = "tsunami"
    GAUGE = "gauge"


_RANK = {AttentionKind.UPDATED: 1, AttentionKind.NEW: 2, AttentionKind.MAJOR: 3, AttentionKind.TSUNAMI: 4}


_UNSEEN_KINDS = (AttentionKind.NEW, AttentionKind.MAJOR)  # rows that count toward the unseen badge


class PulseGate:
    """Cooldown per (id, kind): a repeated minor update does not re-flash a row that just flashed."""

    def __init__(self, clock: Clock, cooldown: float = PULSE_COOLDOWN_SECONDS):
        self._clock = clock
        self._cooldown = cooldown
        self._last: dict[tuple[str, str], float] = {}

    def allow(self, key: str, kind: str) -> bool:
        now = self._clock.monotonic()
        self._last = {k: t for k, t in self._last.items() if now - t < self._cooldown}
        last = self._last.get((key, kind))
        if last is not None and now - last < self._cooldown:
            return False
        self._last[(key, kind)] = now
        return True


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
        self._seen: set[str] = set()  # quakes acknowledged since the last take_seen()
        self._gate = PulseGate(clock)

    def _elapsed(self, item: Attention) -> float:
        return self._clock.monotonic() - item.started

    def _finished(self, item: Attention) -> bool:
        elapsed = self._elapsed(item)
        if item.kind is AttentionKind.UPDATED:
            return elapsed >= UPDATED_FLASH_SECONDS
        if item.kind is AttentionKind.NEW:
            return item.acked and elapsed >= NEW_FLASH_SECONDS
        if item.kind is AttentionKind.TSUNAMI:
            return item.acked
        if item.kind is AttentionKind.GAUGE:
            return item.acked or elapsed >= GAUGE_SECONDS
        return item.acked and elapsed >= MAJOR_PULSE_SECONDS

    def observe(self, events: list[Event]) -> None:
        for event in events:
            if isinstance(event, QuakeNew):
                self._start(event.quake.id, AttentionKind.NEW)
            elif isinstance(event, QuakeUpdated):
                if self._gate.allow(event.quake.id, AttentionKind.UPDATED):
                    self._start(event.quake.id, AttentionKind.UPDATED)
            elif isinstance(event, QuakeRenamed):
                if (item := self._items.pop(event.old_id, None)) is not None:
                    kept = self._items.get(event.quake.id)  # the strongest attention of the joined ids survives
                    if kept is None or _RANK[item.kind] > _RANK[kept.kind]:
                        self._items[event.quake.id] = item
            elif isinstance(event, QuakeMajor):
                self._start(event.quake.id, AttentionKind.MAJOR)
            elif isinstance(event, TsunamiIssued | TsunamiChanged):
                if event.alert.level in levels.ACTIONABLE:
                    self._items[TSUNAMI_PREFIX + event.alert.id] = Attention(AttentionKind.TSUNAMI, self._now())
            elif isinstance(event, TsunamiCancelled):
                self._items.pop(TSUNAMI_PREFIX + event.alert.id, None)
            elif isinstance(event, GaugeAnomaly):
                self._items[GAUGE_PREFIX + event.station_id] = Attention(AttentionKind.GAUGE, self._now())

    def _now(self) -> float:
        return self._clock.monotonic()

    def tsunami_unacked(self) -> int:
        """Actionable tsunami alerts nobody has acknowledged: drives the banner, panel pulse and red status bar."""
        return sum(1 for k, i in self._items.items() if k.startswith(TSUNAMI_PREFIX) and not i.acked)

    def gauge_active(self) -> list[str]:
        """Station ids with an anomaly that is neither acknowledged nor older than GAUGE_SECONDS."""
        return [
            k[len(GAUGE_PREFIX) :]
            for k, i in self._items.items()
            if k.startswith(GAUGE_PREFIX) and not self._finished(i)
        ]

    def tsunami_sync(self, active_ids: set[str]) -> None:
        """Forget tsunami attention for alerts no longer in force (expired, cancelled or superseded)."""
        for key in [
            k for k in self._items if k.startswith(TSUNAMI_PREFIX) and k[len(TSUNAMI_PREFIX) :] not in active_ids
        ]:
            del self._items[key]

    def _start(self, quake_id: str, kind: AttentionKind) -> None:
        current = self._items.get(quake_id)
        if current is None or _RANK[kind] >= _RANK[current.kind] or self._finished(current):
            self._items[quake_id] = Attention(kind, self._clock.monotonic())

    def _ack(self, key: str, item: Attention) -> None:
        if not item.acked and item.kind in _UNSEEN_KINDS:
            self._seen.add(key)
        item.acked = True

    def take_seen(self) -> set[str]:
        """Quakes whose unseen highlight was acknowledged since the last call (rows dropped from the view are not)."""
        seen, self._seen = self._seen, set()
        return seen

    def ack(self, quake_id: str) -> None:
        if item := self._items.get(quake_id):
            self._ack(quake_id, item)

    def ack_tsunami(self) -> None:
        """Acknowledge tsunami alerts only; quake attention is left alone."""
        for key, item in self._items.items():
            if key.startswith(TSUNAMI_PREFIX):
                item.acked = True

    def ack_all(self) -> None:
        for key, item in self._items.items():
            self._ack(key, item)

    def mark_seen(self, quake_ids: set[str]) -> None:
        """Rows that stayed in the viewport for a whole tick count as seen."""
        for quake_id in quake_ids:
            self.ack(quake_id)

    def retain(self, quake_ids: set[str]) -> None:
        """Drop attention for quakes the store no longer holds; a search or filter only hides rows, so
        their attention is kept (and returns when the filter is lowered)."""
        keep = (TSUNAMI_PREFIX, GAUGE_PREFIX)
        for quake_id in [k for k in self._items if k not in quake_ids and not k.startswith(keep)]:
            del self._items[quake_id]

    def restore(self, new_ids: set[str], major_ids: set[str]) -> None:
        """Quakes announced in an earlier run and never seen: back to the dim highlight, with no flash or pulse."""
        for quake_id in new_ids | major_ids:
            kind, age = (
                (AttentionKind.MAJOR, MAJOR_PULSE_SECONDS)
                if quake_id in major_ids
                else (AttentionKind.NEW, NEW_FLASH_SECONDS)
            )
            self._items.setdefault(quake_id, Attention(kind, self._clock.monotonic() - age))

    def is_unseen(self, quake_id: str) -> bool:
        item = self._items.get(quake_id)
        return item is not None and item.kind in _UNSEEN_KINDS and not item.acked

    def unseen(self, countable: set[str] | None = None) -> int:
        """Rows still to be seen; with `countable`, only those ids (the ones the filter can show)."""
        return sum(
            1
            for key, item in self._items.items()
            if item.kind in _UNSEEN_KINDS and not item.acked and (countable is None or key in countable)
        )

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
