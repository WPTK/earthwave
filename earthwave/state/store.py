"""Canonical in-memory quake store: merge and dedupe, material-update rules, filtered views, coverage tracking."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from earthwave.clock import Clock
from earthwave.config import AlertsConfig, HomeConfig
from earthwave.models import Batch, Earthquake, GaugeReading, Station, TsunamiAlert
from earthwave.sources import coops, levels, ndbc
from earthwave.sources.anomaly import find_anomaly
from earthwave.sources.geo import haversine_km
from earthwave.state.events import (
    Event,
    GaugeAnomaly,
    QuakeMajor,
    QuakeNearHome,
    QuakeNew,
    QuakeUpdated,
    TsunamiCancelled,
    TsunamiChanged,
    TsunamiIssued,
)

log = logging.getLogger(__name__)

RETAIN = timedelta(days=35)
MAG_DELTA = 0.2
DEPTH_DELTA_KM = 10.0
ALERT_RETAIN = timedelta(days=3)
LINK_SECONDS = 300.0  # an alert names its quake within 5 minutes ...
LINK_KM = 200.0  # ... and 200 km of the USGS origin
DEFAULT_TTL = timedelta(hours=12)  # an alert that states no expiry lapses this long after it was issued or updated


GAUGE_SOURCES = frozenset({"dart", "coops"})  # their batches carry no quakes and never warm the quake store
GAUGE_KEEP = timedelta(hours=24)  # readings kept per station, measured back from the newest one
GAUGE_MAX_ROWS = 4000  # 15-second event-mode data for 12 hours is 2880 rows
GAUGE_FRESH = timedelta(minutes=30)  # the first look at a station only reports exceedances this recent
EPISODE_GAP = timedelta(minutes=30)  # exceedances further apart than this are separate anomaly episodes
ALERT_HINT = frozenset({levels.WARNING, levels.ADVISORY, levels.WATCH})  # levels that speed the gauge polling up

QUIET = (levels.CANCELLATION, levels.INFORMATION)  # products that neither supersede nor merge with a warning


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
        self._tsunamis: dict[str, TsunamiAlert] = {}
        self._first_seen: dict[str, datetime] = {}
        self._by_key: dict[str, str] = {}  # dedupe_key -> alert id
        self._alias: dict[str, str] = {}  # id of a merged duplicate -> id it was merged into
        self._closed: dict[str, str] = {}  # alert id -> "cancelled" | "superseded"
        self._gauge_stations: dict[str, Station] = {}
        self._readings: dict[str, dict[datetime, GaugeReading]] = {}
        self._residuals: dict[str, dict[datetime, ndbc.Residual | coops.Residual]] = {}
        self._predictions: dict[str, list[coops.Prediction]] = {}
        self._evaluated: dict[str, datetime] = {}  # station -> newest residual already judged for an anomaly
        self._episode_end: dict[str, datetime] = {}  # station -> latest exceedance of its current anomaly episode
        self.predictions_for: Callable[[str], list[coops.Prediction]] = lambda station_id: []

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
        if not batch.backfill and batch.source not in GAUGE_SOURCES and not (batch.alerts and not batch.quakes):
            self.warm = True
        events.extend(self._merge_alerts(batch, clock))
        events.extend(self._merge_gauges(batch, clock, silent=batch.backfill or bool(batch.meta.get("silent"))))
        self._link_alerts()
        self._prune(clock)
        return events

    # --- gauges -------------------------------------------------------------------------------------------------

    def gauge_station(self, station_id: str) -> Station | None:
        return self._gauge_stations.get(station_id)

    def gauge_readings(self, station_id: str) -> list[GaugeReading]:
        """Stored readings for a station, oldest first."""
        return [r for _, r in sorted(self._readings.get(station_id, {}).items())]

    def gauge_residuals(self, station_id: str) -> list:
        """Residuals (observed minus the detided or predicted level) for a station, oldest first."""
        return [r for _, r in sorted(self._residuals.get(station_id, {}).items())]

    def gauge_alert_active(self, clock: Clock) -> bool:
        """A Warning, Advisory or Watch is in force: the gauge sources poll at their alert cadence."""
        return any(a.level in ALERT_HINT for a in self.active_alerts(clock))

    def _merge_gauges(self, batch: Batch, clock: Clock, silent: bool) -> list[Event]:
        for station in batch.stations:
            self._gauge_stations[station.id] = station
        by_station: dict[str, list[GaugeReading]] = {}
        for reading in batch.readings:
            by_station.setdefault(reading.station_id, []).append(reading)
        events: list[Event] = []
        for station_id, rows in by_station.items():
            kind = rows[0].kind
            stored = self._readings.setdefault(station_id, {})
            fresh = [r.ts for r in rows if r.ts not in stored or stored[r.ts] != r]
            for r in rows:
                stored[r.ts] = r
            newest = max(stored)
            for ts in [t for t in stored if t < newest - GAUGE_KEEP]:
                del stored[ts]
            for ts in sorted(stored)[: max(0, len(stored) - GAUGE_MAX_ROWS)]:
                del stored[ts]
            if fresh:
                self._refresh_residuals(station_id, kind, min(fresh))
            events.extend(self._judge_gauge(station_id, kind, clock, silent))
        return events

    def _refresh_residuals(self, station_id: str, kind: str, since: datetime) -> None:
        """Recompute residuals from `since` on; earlier ones cannot change (the fit looks backwards only)."""
        rows = self.gauge_readings(station_id)
        if kind == "dart":
            new = ndbc.detrend(rows, since=since)  # type: ignore[arg-type]
        else:
            self._predictions[station_id] = self.predictions_for(station_id) or self._predictions.get(station_id, [])
            new = coops.residuals(rows, self._predictions[station_id])  # type: ignore[arg-type]
            since = datetime.min.replace(tzinfo=UTC)
        kept = {ts: r for ts, r in self._residuals.get(station_id, {}).items() if ts < since}
        kept.update({r.ts: r for r in new})
        floor = rows[0].ts
        self._residuals[station_id] = {ts: r for ts, r in kept.items() if ts >= floor}

    def _judge_gauge(self, station_id: str, kind: str, clock: Clock, silent: bool) -> list[Event]:
        """An anomaly event at the start of each episode; the rest of an episode only extends it.

        Each station remembers the newest residual it judged, so a restart or a new selection (which reads hours of
        history at once) does not replay old exceedances; the first look only counts the last GAUGE_FRESH minutes.
        """
        residuals = self.gauge_residuals(station_id)
        if not residuals:
            return []
        since = self._evaluated.get(station_id) or clock.now() - GAUGE_FRESH
        self._evaluated[station_id] = residuals[-1].ts
        decision = find_anomaly(station_id, kind, residuals, self._alerts, since)
        if decision is None:
            return []
        over = [r.ts for r in residuals if r.ts > since and abs(r.residual_m) >= decision.threshold_m]
        last = self._episode_end.get(station_id)
        starts = last is None or min(over) - last > EPISODE_GAP
        self._episode_end[station_id] = max(over)
        if not starts or silent:
            return []
        station = self._gauge_stations.get(station_id)
        name = station.name.strip() if station and station.name else station_id
        return [GaugeAnomaly(station_id, decision.residual_m, decision.ts, name, decision.threshold_m)]

    # --- tsunami alerts -----------------------------------------------------------------------------------------

    def get_alert(self, alert_id: str) -> TsunamiAlert | None:
        return self._tsunamis.get(self._alias.get(alert_id, alert_id))

    def alerts(self) -> tuple[TsunamiAlert, ...]:
        return tuple(self._tsunamis.values())

    def alert_status(self, alert: TsunamiAlert, clock: Clock) -> str:
        """active | expired | cancelled (by a later product) | superseded (by an update) | cancellation."""
        if alert.level == levels.CANCELLATION:
            return "cancellation"
        if alert.id in self._closed:
            return self._closed[alert.id]
        if self.expiry(alert) <= clock.now():
            return "expired"
        return "active"

    def expiry(self, alert: TsunamiAlert) -> datetime:
        """When the alert lapses: its own expiry, else DEFAULT_TTL after it was last issued, updated or first seen."""
        if alert.expires is not None:
            return alert.expires
        base = alert.updated or alert.issued or self._first_seen.get(alert.id)
        return (base + DEFAULT_TTL) if base else datetime.max.replace(tzinfo=UTC)

    def active_alerts(self, clock: Clock, actionable_only: bool = False) -> list[TsunamiAlert]:
        """Alerts in force on the injected clock, highest level first."""
        rows = [
            a
            for a in self._tsunamis.values()
            if self.alert_status(a, clock) == "active" and (not actionable_only or a.level in levels.ACTIONABLE)
        ]
        rows.sort(key=lambda a: (-levels.RANK[a.level], -(a.issued.timestamp() if a.issued else 0.0)))
        return rows

    def quake_for(self, alert: TsunamiAlert) -> Earthquake | None:
        return self._quakes.get(alert.quake_ref) if alert.quake_ref else None

    def alerts_for_quake(self, quake_id: str) -> list[TsunamiAlert]:
        return sorted(
            (a for a in self._tsunamis.values() if a.quake_ref == quake_id),
            key=lambda a: a.issued.timestamp() if a.issued else 0.0,
        )

    def _find_duplicate(self, alert: TsunamiAlert) -> TsunamiAlert | None:
        """The stored alert this one repeats: same id, same WMO header + sent, else same quake and areas elsewhere."""
        for known_id in (self._alias.get(alert.id, alert.id), self._by_key.get(alert.dedupe_key)):
            if known_id and known_id in self._tsunamis:
                return self._tsunamis[known_id]
        if alert.quake_ref is None or alert.level in (levels.CANCELLATION, levels.INFORMATION):
            return None
        wanted = {a.lower() for a in alert.areas}
        for other in self._tsunamis.values():
            if other.source == alert.source or other.id in self._closed or other.level in QUIET:
                continue
            if other.quake_ref == alert.quake_ref and wanted & {a.lower() for a in other.areas}:
                return other
        return None

    def _merge_duplicate(self, old: TsunamiAlert, new: TsunamiAlert) -> TsunamiAlert:
        """Same product from two sources: the higher level wins, the richer fields fill the gaps."""
        top = new if levels.RANK[new.level] > levels.RANK[old.level] else old
        polygon_first = new if new.geometry and new.geometry[0] == "polygon" else old
        return replace(
            old,
            level=top.level,
            headline=top.headline,
            areas=tuple(dict.fromkeys((*old.areas, *new.areas))),
            expires=max((e for e in (old.expires, new.expires) if e), default=None),
            updated=max((u for u in (old.updated, new.updated) if u), default=None),
            geometry=polygon_first.geometry or old.geometry or new.geometry,
            text=old.text if len(old.text) >= len(new.text) else new.text,
            origin=old.origin or new.origin,
            quake_ref=old.quake_ref or new.quake_ref,
        )

    def _merge_alerts(self, batch: Batch, clock: Clock) -> list[Event]:
        events: list[Event] = []
        silent = batch.backfill or bool(batch.meta.get("silent"))
        for alert in batch.alerts:
            alert = self._link(alert)
            old = self._find_duplicate(alert)
            if old is not None:
                merged = self._merge_duplicate(old, alert)
                self._tsunamis[old.id] = merged
                self._alias[alert.id] = old.id
                self._by_key[alert.dedupe_key] = old.id
                if merged.level != old.level and not silent and self._live(merged, clock):
                    events.append(TsunamiChanged(merged, old.level))
                continue
            self._tsunamis[alert.id] = alert
            self._first_seen[alert.id] = clock.now()
            self._by_key[alert.dedupe_key] = alert.id
            closed = self._close_referenced(alert)
            if silent:
                continue
            if alert.level == levels.CANCELLATION:
                # A cancellation closes its targets even when it carries its own, already-passed expiry.
                events.extend(TsunamiCancelled(c) for c in closed if self._live(c, clock))
            elif not self._live(alert, clock):
                continue
            elif live := [c for c in closed if self._live(c, clock)]:
                # A replacement of a live alert carries the attention over, even at the same level and areas.
                events.append(TsunamiChanged(alert, max(live, key=lambda c: levels.RANK[c.level]).level))
            else:
                events.append(TsunamiIssued(alert))
        return events

    def _live(self, alert: TsunamiAlert, clock: Clock) -> bool:
        """Products already expired when first merged (a restart reading old feed entries) raise no events."""
        return self.expiry(alert) > clock.now()

    def _close_referenced(self, alert: TsunamiAlert) -> list[TsunamiAlert]:
        """Close the open alerts this product cancels or supersedes; returns those that were still open.

        An INFORMATION statement closes nothing. A cancel that names no known alert closes only the open,
        actionable alerts of its own quake and source, narrowed to overlapping areas when it states any.
        """
        if alert.level == levels.INFORMATION:
            return []
        targets = [self.get_alert(ref) for ref in alert.references]
        if alert.level == levels.CANCELLATION and not any(targets) and alert.quake_ref:
            wanted = {a.lower() for a in alert.areas}
            targets = [
                a
                for a in self._tsunamis.values()
                if a.quake_ref == alert.quake_ref
                and a.source == alert.source
                and a.id != alert.id
                and a.level not in QUIET
                and (not wanted or wanted & {x.lower() for x in a.areas})
            ]
        status = "cancelled" if alert.level == levels.CANCELLATION else "superseded"
        closed = []
        for target in targets:
            if target is None or target.id == alert.id or target.id in self._closed:
                continue
            if target.level == levels.CANCELLATION:
                continue
            self._closed[target.id] = status
            closed.append(target)
        return closed

    def _link(self, alert: TsunamiAlert) -> TsunamiAlert:
        """Attach the USGS quake an alert names (within 5 minutes and 200 km of its origin)."""
        if alert.quake_ref is not None or alert.origin is None:
            return alert
        when, lat, lon = alert.origin
        best, best_km = None, LINK_KM
        for quake in self._quakes.values():
            if abs((quake.time - when).total_seconds()) > LINK_SECONDS:
                continue
            km = haversine_km(lat, lon, quake.lat, quake.lon)
            if km <= best_km:
                best, best_km = quake, km
        return replace(alert, quake_ref=best.id) if best else alert

    def _link_alerts(self) -> None:
        """A quake can arrive after the alert that names it."""
        for alert_id, alert in list(self._tsunamis.items()):
            linked = self._link(alert)
            if linked is not alert:
                self._tsunamis[alert_id] = linked

    def _prune(self, clock: Clock) -> None:
        cutoff = clock.now() - RETAIN
        for quake_id in [q.id for q in self._quakes.values() if q.time < cutoff]:
            del self._quakes[quake_id]
        alert_cutoff = clock.now() - ALERT_RETAIN
        stale = [
            a
            for a in self._tsunamis.values()
            if (a.issued or a.updated or self._first_seen.get(a.id) or alert_cutoff) < alert_cutoff
        ]
        for alert in stale:
            del self._tsunamis[alert.id]
            self._closed.pop(alert.id, None)
            self._first_seen.pop(alert.id, None)
            self._by_key = {k: v for k, v in self._by_key.items() if v != alert.id}
            self._alias = {k: v for k, v in self._alias.items() if v != alert.id}

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
