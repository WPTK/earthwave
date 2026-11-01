"""Canonical in-memory quake store: merge and dedupe, material-update rules, filtered views, coverage tracking."""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from earthwave.clock import Clock
from earthwave.config import MAX_VIEW_HOURS, AlertsConfig, HomeConfig
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
    QuakeRemoved,
    QuakeRenamed,
    QuakeUpdated,
    TsunamiCancelled,
    TsunamiChanged,
    TsunamiIssued,
)

log = logging.getLogger(__name__)

RETAIN = timedelta(hours=MAX_VIEW_HOURS)
MAG_DELTA = 0.2
DEPTH_DELTA_KM = 10.0
ALERT_RETAIN = timedelta(days=3)
FEED_LAG = timedelta(minutes=10)  # a feed may not list a quake this fresh yet, nor drop one this old
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
LATE_GRACE = timedelta(minutes=30)  # a healthy feed's follow-up may run this late
OVERDUE_MAX = timedelta(hours=6)  # a feed that never completes a poll cannot hold an alert open longer
IN_FORCE = ("active", "overdue")  # alert statuses that count as in force
ALERT_SOURCES = frozenset(
    {"tsunamigov", "nws", "jma"}
)  # their batches carry alerts only and never warm the quake store
LISTING_SOURCES = frozenset({"nws"})  # each complete batch lists every alert in force, so a missing one has ended
SNAPSHOT_SOURCES = frozenset({"jma"})  # each report restates the whole event, so a later one replaces its predecessors


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
        self._by_alias: dict[str, str] = {}  # an id USGS has replaced -> the stored quake that names it
        # (hours, min_magnitude) -> when the claim was last confirmed: the store holds every such quake up to then
        self._coverage: dict[tuple[float, float], datetime] = {}
        self.coverage_lapsed = False  # a claim was dropped because the steady feed missed more than its window
        self.warm = False
        self._steady_at: datetime | None = None  # when the steady feed last answered
        self._tsunamis: dict[str, TsunamiAlert] = {}
        self._first_seen: dict[str, datetime] = {}
        self._by_key: dict[str, str] = {}  # dedupe_key -> alert id
        self._alias: dict[str, str] = {}  # id of a merged duplicate -> id it was merged into
        self._sources: dict[str, set[str]] = {}  # alert id -> every source that has confirmed it
        # Alerts the last complete listing left out. Not keyed by source: the only listing source is NWS.
        self._omitted: set[str] = set()
        self._closed: dict[str, str] = {}  # alert id -> "cancelled" | "superseded" | "withdrawn"
        self._gauge_stations: dict[str, Station] = {}
        self._readings: dict[str, dict[datetime, GaugeReading]] = {}
        self._residuals: dict[str, dict[datetime, ndbc.Residual | coops.Residual]] = {}
        self._predictions: dict[str, list[coops.Prediction]] = {}
        self._evaluated: dict[str, datetime] = {}  # station -> newest residual already judged for an anomaly
        self._episode_end: dict[str, datetime] = {}  # station -> latest exceedance of its current anomaly episode
        self.predictions_for: Callable[[str], list[coops.Prediction]] = lambda station_id: []
        self.answered_since: Callable[[str, datetime], bool] = lambda source, when: True  # the scheduler replaces it

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
        self._coverage = {claim: at for claim, at in self._coverage.items() if claim[1] >= min_magnitude}

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

    def _threshold_events(self, olds: list[Earthquake], new: Earthquake) -> list[Event]:
        """Threshold crossings against everything the store held for this event: one that was major (or near home)
        under any of its ids is not announced again."""
        events: list[Event] = []
        if self._is_major(new) and not any(self._is_major(o) for o in olds):
            events.append(QuakeMajor(new))
        km = self._is_near_home(new)
        if km is not None and all(self._is_near_home(o) is None for o in olds):
            events.append(QuakeNearHome(new, km))
        return events

    def merge(self, batch: Batch, clock: Clock) -> list[Event]:
        """Fold a batch into the store and return the events it caused.

        Backfill batches, and everything before the first steady batch has been merged (the store is "cold"),
        update the store without raising events, so launch never produces a flash storm. A quake published after the
        steady feed last answered is the exception: the feed has not delivered it yet and would find it already
        stored, so it is announced here or never.
        """
        steady_at = self._steady_at
        if self._is_steady(batch):
            self._steady_at = batch.generated or batch.fetched_at  # server time, as `updated` is
        if batch.not_modified:
            if batch.coverage and batch.generated:
                self._claim(batch)  # the feed is unchanged since it was made: its claims are current up to then
            return []
        silent = batch.backfill or bool(batch.meta.get("silent")) or not self.warm
        events: list[Event] = []
        for quake in batch.quakes:
            known = self._known(quake)
            if not known and (kept := self._by_alias.get(quake.id)):  # a stale copy under an id USGS has replaced
                events.append(QuakeRenamed(quake.id, self._quakes[kept]))
                continue
            old = known[0] if known else None
            if old is not None and quake.updated and old.updated and quake.updated < old.updated:
                continue
            for other in known:
                if other.id != quake.id:  # USGS moved the event to another preferred id
                    self._drop(other.id)
                    self._relink(other.id, quake.id)
                    events.append(QuakeRenamed(other.id, quake))
            self._put(quake)
            if silent and not (old is None and self._published_since(quake, steady_at)):
                continue
            if old is None:
                events.append(QuakeNew(quake))
                events.extend(self._threshold_events([], quake))
            else:
                changed = changed_fields(old, quake)
                if changed:
                    events.append(QuakeUpdated(quake, changed))
                events.extend(self._threshold_events(known, quake))
        if batch.coverage:
            events.extend(self._reconcile(batch))
            self._claim(batch)
        if batch.backfill:
            self.coverage_lapsed = False  # the backfill that closes a lapsed claim has landed (even if it was partial)
        if self._is_steady(batch):
            self.warm = True
        events.extend(self._merge_alerts(batch, clock))
        events.extend(self._merge_gauges(batch, clock, silent=batch.backfill or bool(batch.meta.get("silent"))))
        self._link_alerts()
        self._prune(clock)
        return events

    @staticmethod
    def _is_steady(batch: Batch) -> bool:
        return not batch.backfill and batch.source not in GAUGE_SOURCES | ALERT_SOURCES

    @staticmethod
    def _published_since(quake: Earthquake, since: datetime | None) -> bool:
        """True for a quake that happened (within the feed's lag) and was published after `since`; a long-past quake
        merely revised since is not new."""
        return since is not None and (quake.updated or quake.time) > since and quake.time > since - FEED_LAG

    def _known(self, quake: Earthquake) -> list[Earthquake]:
        """Every stored version of this event under any id USGS has given it, the one under its own id first."""
        ids = dict.fromkeys((quake.id, *quake.also_ids))
        return [old for quake_id in ids if (old := self._quakes.get(quake_id)) is not None]

    def _put(self, quake: Earthquake) -> None:
        self._drop(quake.id)
        self._quakes[quake.id] = quake
        for alias in quake.also_ids:
            self._by_alias[alias] = quake.id

    def _drop(self, quake_id: str) -> None:
        if (old := self._quakes.pop(quake_id, None)) is not None:
            for alias in old.also_ids:
                if self._by_alias.get(alias) == quake_id:
                    del self._by_alias[alias]

    def _relink(self, old_id: str, new_id: str | None) -> None:
        """Alerts naming a quake that is gone follow it to its new id, or lose the link so `_link` can find it again."""
        for alert_id, alert in self._tsunamis.items():
            if alert.quake_ref == old_id:
                self._tsunamis[alert_id] = replace(alert, quake_ref=new_id)

    def _reconcile(self, batch: Batch) -> list[Event]:
        """A summary feed names every quake of its window and magnitude floor, so a stored quake it leaves out was
        deleted or revised out of the feed. A quake revised below the floor is dropped by design: the feed no longer
        vouches for it. The window is measured from when the feed was made, and its edges are spared (a feed lags and
        its window slides). An empty listing proves nothing and removes nothing."""
        assert batch.coverage is not None
        if not batch.listed or batch.generated is None:
            return []
        hours, floor = batch.coverage
        oldest = batch.generated - timedelta(hours=hours) + FEED_LAG
        newest = batch.generated - FEED_LAG
        gone = [
            q
            for q in self._quakes.values()
            if oldest <= q.time <= newest and q.mag >= floor and batch.listed.isdisjoint((q.id, *q.also_ids))
        ]
        for quake in gone:
            self._drop(quake.id)
            self._relink(quake.id, None)
        return [QuakeRemoved(q.id) for q in gone]

    def _claim(self, batch: Batch) -> None:
        """Record what the batch covers. A steady feed keeps every claim at or above its floor current, but only if it
        saw the claim within its own window; an outage longer than that leaves a hole, so the claim lapses."""
        assert batch.coverage is not None
        hours, floor = batch.coverage
        at = batch.generated or batch.fetched_at
        if not batch.backfill:
            window = timedelta(hours=hours)
            lapsed = [claim for claim, seen in self._coverage.items() if seen < at - window]
            self.coverage_lapsed |= bool(lapsed)
            for claim in lapsed:
                del self._coverage[claim]
            for claim, seen in self._coverage.items():
                if claim[1] >= floor:
                    self._coverage[claim] = max(seen, at)
        self._coverage[batch.coverage] = max(at, self._coverage.get(batch.coverage, at))

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

    def forget_gauges(self, station_ids: Iterable[str]) -> None:
        """Stations that left the selection: their next look is a first look again (see `_judge_gauge`).

        An episode in progress is kept, so a station that comes back mid-episode is not announced a second time.
        """
        for station_id in station_ids:
            self._evaluated.pop(station_id, None)

    def _refresh_residuals(self, station_id: str, kind: str, since: datetime) -> None:
        """Recompute residuals from `since` on; earlier ones cannot change (the fit looks backwards only)."""
        rows = self.gauge_readings(station_id)
        if kind == "dart":
            new = ndbc.detrend(rows, since=since)  # type: ignore[arg-type]
        else:
            self._predictions[station_id] = self.predictions_for(station_id) or self._predictions.get(station_id, [])
            new = coops.remove_baseline(coops.residuals(rows, self._predictions[station_id]))  # type: ignore[arg-type]
            since = datetime.min.replace(tzinfo=UTC)
        kept = {ts: r for ts, r in self._residuals.get(station_id, {}).items() if ts < since}
        kept.update({r.ts: r for r in new})
        floor = rows[0].ts
        self._residuals[station_id] = {ts: r for ts, r in kept.items() if ts >= floor}

    def _judge_gauge(self, station_id: str, kind: str, clock: Clock, silent: bool) -> list[Event]:
        """An anomaly event at the start of each episode; the rest of an episode only extends it.

        Each station remembers the newest residual it judged, so everything after that counts however late it arrives
        (a feed that was down for an hour reports the exceedance in the batch that follows). Only the first look at a
        station, after a restart or `forget_gauges`, reads hours of history at once, and it counts just the last
        GAUGE_FRESH minutes so old exceedances are not replayed.
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
        late = clock.now() - decision.ts > GAUGE_FRESH
        return [GaugeAnomaly(station_id, decision.residual_m, decision.ts, name, decision.threshold_m, late)]

    # --- tsunami alerts -----------------------------------------------------------------------------------------

    def get_alert(self, alert_id: str) -> TsunamiAlert | None:
        return self._tsunamis.get(self._alias.get(alert_id, alert_id))

    def alerts(self) -> tuple[TsunamiAlert, ...]:
        return tuple(self._tsunamis.values())

    def alert_status(self, alert: TsunamiAlert, clock: Clock) -> str:
        """active | overdue | expired | cancelled (by a later product) | superseded (by an update) | cancellation.

        An actionable alert past its expiry stays "overdue", and in force, for LATE_GRACE (a follow-up from a healthy
        feed can run late) and then until its source has answered a poll after that moment: a feed that is down or
        late cannot have told us about a follow-up.
        """
        if alert.level == levels.CANCELLATION:
            return "cancellation"
        if alert.id in self._closed:
            return self._closed[alert.id]
        if self.expiry(alert) > clock.now():
            return "active"
        return "overdue" if self._overdue(alert, clock) else "expired"

    def _overdue(self, alert: TsunamiAlert, clock: Clock, answering: str | None = None) -> bool:
        """Past its expiry, and only if the store held it while it was in force (an alert first merged already
        expired, a restart reading old entries, never was): for LATE_GRACE regardless, then until its source
        completes a poll that began after the expiry, for at most OVERDUE_MAX. `answering` is the source whose
        complete batch is being merged; it has just confirmed what it holds, so past the grace it ends the wait."""
        expiry, now = self.expiry(alert), clock.now()
        seen = self._first_seen.get(alert.id)
        if alert.level not in levels.ACTIONABLE or seen is None or seen >= expiry or not expiry <= now:
            return False
        if now < expiry + LATE_GRACE:  # keep LATE_GRACE below OVERDUE_MAX
            return True
        return (
            now < expiry + OVERDUE_MAX and alert.source != answering and not self.answered_since(alert.source, expiry)
        )

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
            if self.alert_status(a, clock) in IN_FORCE and (not actionable_only or a.level in levels.ACTIONABLE)
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
        referenced = {t.id for ref in alert.references if (t := self.get_alert(ref))}
        for other in self._tsunamis.values():
            if other.source == alert.source or other.id in self._closed or other.level in QUIET:
                continue
            if other.id in referenced:  # a follow-up never repeats the product it updates
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
            references=tuple(dict.fromkeys((*old.references, *new.references))),
        )

    def _merge_alerts(self, batch: Batch, clock: Clock) -> list[Event]:
        """Fold every alert of the batch into the store, then report what is still true at the end of it.

        A feed that keeps old entries replays Watch, Warning and Cancel in one batch after a restart; an alert closed
        later in the same batch raises no Issued or Changed event.
        """
        events: list[Event] = []
        silent = batch.backfill or bool(batch.meta.get("silent"))
        answering = None if batch.meta.get("incomplete") else batch.source  # this batch is itself an answer
        for alert in batch.alerts:
            alert = self._link(alert)
            old = self._find_duplicate(alert)
            if old is not None:
                record = self._merge_duplicate(old, alert)
                self._tsunamis[old.id] = record
                self._alias[alert.id] = old.id
                self._sources[old.id].add(alert.source)
            else:
                record = alert
                self._tsunamis[alert.id] = alert
                self._first_seen[alert.id] = clock.now()
                self._sources[alert.id] = {alert.source}
            self._by_key[alert.dedupe_key] = record.id
            closed = self._close_referenced(record)
            if old is None and self._closed_by_stored(record):
                continue  # arrived after the product that ends it: never in force
            if silent:
                continue
            live = [
                c for c in closed if self._live(c, clock)
            ]  # no `answering` here, deliberately: this batch has not yet confirmed an alert it is closing
            if record.level == levels.CANCELLATION:
                # A cancellation closes its targets even when it carries its own, already-passed expiry.
                events.extend(TsunamiCancelled(c) for c in live)
            elif not self._live(record, clock, answering):
                continue
            elif live:
                # A replacement of a live alert carries the attention over, even at the same level and areas.
                events.append(TsunamiChanged(record, max(live, key=lambda c: levels.RANK[c.level]).level))
            elif old is None:
                events.append(TsunamiIssued(record))
            elif record.level != old.level:
                events.append(TsunamiChanged(record, old.level))
        if batch.source in LISTING_SOURCES and answering and not batch.backfill:
            relisted = self._withdraw_unlisted(batch)
            if not silent:  # its attention went when it was withdrawn: raise it again, unless this batch just did
                told = {self._alias.get(e.alert.id, e.alert.id) for e in events if hasattr(e, "alert")}
                events.extend(
                    TsunamiIssued(r) for r in relisted if r.id not in told and self._live(r, clock, answering)
                )
        return [
            e
            for e in events
            if not isinstance(e, TsunamiIssued | TsunamiChanged) or self._in_force(e.alert, clock, answering)
        ]

    def _withdraw_unlisted(self, batch: Batch) -> list[TsunamiAlert]:
        """Two complete listings in a row that leave out an alert the source held: the source has ended it, quietly.
        One omission is a blip and proves nothing. Returns the alerts that were withdrawn and are listed again."""
        listed = {self._alias.get(a.id, a.id) for a in batch.alerts}
        omitted = self._omitted
        self._omitted = set()
        relisted = []
        for rid in listed:
            if self._closed.get(rid) == "withdrawn":  # listed again: it never ended
                del self._closed[rid]
                relisted.append(self._tsunamis[rid])
        for held in self._tsunamis.values():
            # Only a record this source alone vouches for: one a second source also carries is not its to end.
            if (
                held.id not in listed
                and held.level != levels.CANCELLATION
                and self._sources.get(held.id) == {batch.source}
            ):
                if held.id in omitted:
                    self._closed.setdefault(held.id, "withdrawn")
                self._omitted.add(held.id)
        return relisted

    @staticmethod
    def _ends(closer: TsunamiAlert, target: TsunamiAlert) -> bool:
        """Whether `closer` may end `target`: not when both times are known and it is the older one (a product
        retried late does not end what came after it). One rule for every order the two arrive in."""
        return not (closer.issued and target.issued and closer.issued < target.issued)

    @staticmethod
    def _replaces_nothing(alert: TsunamiAlert) -> bool:
        """A snapshot-source alert with no areas that is not a cancellation: an observation-only report, or the
        fallback built from a list entry. It cannot say what it replaces, and nothing it names replaces it."""
        return alert.source in SNAPSHOT_SOURCES and not alert.areas and alert.level != levels.CANCELLATION

    def _closed_by_stored(self, alert: TsunamiAlert) -> bool:
        """Close `alert` on arrival when a product already held ends it, because it was retried after its own
        cancellation or replacement and found nothing to close then. Two cases, both through `_ends`: a held product
        that names `alert`, or (a snapshot source such as JMA names the earlier reports of an event, not the
        later) a held report among those `alert` names."""
        enders = [self.get_alert(ref) for ref in alert.references if alert.source in SNAPSHOT_SOURCES]
        enders += [o for o in self._tsunamis.values() if any(self.get_alert(ref) is alert for ref in o.references)]
        for other in enders:
            if other is None or other.id == alert.id or self._replaces_nothing(other):
                continue
            if other.level == levels.INFORMATION and other.source not in SNAPSHOT_SOURCES:
                continue  # an INFORMATION statement closes nothing but a JMA report that names areas
            if self._ends(other, alert):
                self._closed[alert.id] = "cancelled" if other.level == levels.CANCELLATION else "superseded"
                return True
        return False

    def _in_force(self, alert: TsunamiAlert, clock: Clock, answering: str | None) -> bool:
        current = self.get_alert(alert.id)
        return current is not None and self._live(current, clock, answering) and current.id not in self._closed

    def _live(self, alert: TsunamiAlert, clock: Clock, answering: str | None = None) -> bool:
        """In force now. Products already expired when first merged (a restart reading old feed entries) raise no
        events, but an overdue one is still in force, so its follow-up is a change and not a new alarm.
        `answering` is the source whose complete batch is being merged: it has just confirmed what it holds."""
        if self.expiry(alert) > clock.now():
            return True
        return self._overdue(alert, clock, answering)

    def _close_referenced(self, alert: TsunamiAlert) -> list[TsunamiAlert]:
        """Close the open alerts this product cancels or supersedes; returns those that were still open.

        An INFORMATION statement closes nothing, except from a snapshot source (JMA) when it names areas (an
        observation-only report names none) and then only reports that are not newer than itself: a lift report.
        A cancel that names no known alert closes only the open, actionable alerts of its own quake and source,
        narrowed to overlapping areas when it states any.
        """
        snapshot = alert.level == levels.INFORMATION and alert.source in SNAPSHOT_SOURCES and bool(alert.areas)
        if alert.level == levels.INFORMATION and not snapshot:
            return []
        if self._replaces_nothing(alert):
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
            if target is None or target.id == alert.id or self._closed.get(target.id, "withdrawn") != "withdrawn":
                continue  # already ended; a withdrawal alone may be overwritten by the product that ends it
            if target.level == levels.CANCELLATION:
                continue
            if not self._ends(alert, target):
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
            self._drop(quake_id)
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
            self._sources.pop(alert.id, None)
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
