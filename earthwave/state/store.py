"""Canonical in-memory quake store: merge and dedupe, material-update rules, filtered views, coverage tracking."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from earthwave.clock import Clock
from earthwave.config import AlertsConfig, HomeConfig
from earthwave.models import Batch, Earthquake
from earthwave.sources.geo import haversine_km
from earthwave.state.events import Event, QuakeMajor, QuakeNearHome, QuakeNew, QuakeUpdated

log = logging.getLogger(__name__)

RETAIN = timedelta(days=35)
MAG_DELTA = 0.2
DEPTH_DELTA_KM = 10.0


class SortKey(StrEnum):
    TIME = "time"
    MAG = "mag"
    DIST = "dist"

    def next(self, has_home: bool) -> "SortKey":
        order = [SortKey.TIME, SortKey.MAG, *([SortKey.DIST] if has_home else [])]
        return order[(order.index(self) + 1) % len(order)] if self in order else SortKey.TIME


@dataclass(frozen=True, slots=True)
class ViewFilter:
    min_magnitude: float = 2.5
    hours: float = 24
    query: str = ""


def changed_fields(old: Earthquake, new: Earthquake) -> tuple[str, ...]:
    """Material differences between two versions of a quake; empty means the update is not worth an event."""
    changed = []
    if abs(new.mag - old.mag) >= MAG_DELTA:
        changed.append("mag")
    if abs(new.depth_km - old.depth_km) >= DEPTH_DELTA_KM:
        changed.append("depth")
    if new.status != old.status and new.status == "reviewed":
        changed.append("status")
    if new.tsunami_flag != old.tsunami_flag:
        changed.append("tsunami")
    if new.pager_alert != old.pager_alert:
        changed.append("pager")
    if new.place != old.place:
        changed.append("place")
    return tuple(changed)


class Store:
    def __init__(self, alerts: AlertsConfig | None = None, home: HomeConfig | None = None):
        self._alerts = alerts or AlertsConfig()
        self._home = home or HomeConfig()
        self._quakes: dict[str, Earthquake] = {}
        self._coverage: list[tuple[float, float]] = []
        self.warm = False

    def __len__(self) -> int:
        return len(self._quakes)

    def get(self, quake_id: str) -> Earthquake | None:
        return self._quakes.get(quake_id)

    def all(self) -> tuple[Earthquake, ...]:
        return tuple(self._quakes.values())

    def is_covered(self, hours: float, min_magnitude: float) -> bool:
        """True when merged batches already contain every quake in the last `hours` at or above `min_magnitude`."""
        return any(h >= hours and m <= min_magnitude for h, m in self._coverage)

    def drop_coverage_below(self, min_magnitude: float) -> None:
        """Forget coverage claims for magnitudes the live feed no longer delivers (the steady feed narrowed)."""
        self._coverage = [(h, m) for h, m in self._coverage if m >= min_magnitude]

    def distance_home_km(self, quake: Earthquake) -> float | None:
        if not self._home.enabled:
            return None
        return haversine_km(self._home.lat, self._home.lon, quake.lat, quake.lon)

    def _is_major(self, quake: Earthquake) -> bool:
        return quake.mag >= self._alerts.major_magnitude

    def _is_near_home(self, quake: Earthquake) -> float | None:
        km = self.distance_home_km(quake)
        if km is not None and km <= self._home.radius_km and quake.mag >= self._alerts.near_home_magnitude:
            return km
        return None

    def _threshold_events(self, old: Earthquake | None, new: Earthquake) -> list[Event]:
        events: list[Event] = []
        if self._is_major(new) and (old is None or not self._is_major(old)):
            events.append(QuakeMajor(new))
        km = self._is_near_home(new)
        if km is not None and (old is None or self._is_near_home(old) is None):
            events.append(QuakeNearHome(new, km))
        return events

    def merge(self, batch: Batch, clock: Clock) -> list[Event]:
        """Fold a batch into the store and return the events it caused.

        Backfill batches, and everything before the first steady batch has been merged (the store is "cold"),
        update the store without raising events, so launch never produces a flash storm.
        """
        if batch.not_modified:
            return []
        silent = batch.backfill or bool(batch.meta.get("silent")) or not self.warm
        events: list[Event] = []
        for quake in batch.quakes:
            old = self._quakes.get(quake.id)
            if old is not None and quake.updated and old.updated and quake.updated < old.updated:
                continue
            self._quakes[quake.id] = quake
            if silent:
                continue
            if old is None:
                events.append(QuakeNew(quake))
                events.extend(self._threshold_events(None, quake))
            else:
                changed = changed_fields(old, quake)
                if changed:
                    events.append(QuakeUpdated(quake, changed))
                events.extend(self._threshold_events(old, quake))
        if batch.coverage and batch.coverage not in self._coverage:
            self._coverage.append(batch.coverage)
        if not batch.backfill:
            self.warm = True
        self._prune(clock)
        return events

    def _prune(self, clock: Clock) -> None:
        cutoff = clock.now() - RETAIN
        for quake_id in [q.id for q in self._quakes.values() if q.time < cutoff]:
            del self._quakes[quake_id]

    def visible(
        self,
        view: ViewFilter,
        clock: Clock,
        sort: SortKey = SortKey.TIME,
    ) -> list[Earthquake]:
        """Quakes passing the filter, sorted; recomputed from the store on every call, never cached."""
        since = clock.now() - timedelta(hours=view.hours)
        query = view.query.strip().lower()
        rows = [
            q
            for q in self._quakes.values()
            if q.mag >= view.min_magnitude and q.time >= since and (not query or query in q.place.lower())
        ]
        if sort is SortKey.MAG:
            rows.sort(key=lambda q: (-q.mag, -q.time.timestamp()))
        elif sort is SortKey.DIST and self._home.enabled:
            rows.sort(key=lambda q: haversine_km(self._home.lat, self._home.lon, q.lat, q.lon))
        else:
            rows.sort(key=lambda q: -q.time.timestamp())
        return rows
