"""Aftershock clustering: group quakes that look like the sequence of a larger one (Gardner and Knopoff, 1974).

A quake is an aftershock of a larger one when it follows it within a time and distance window that grows with the
larger quake's magnitude. Pure and deterministic: the same quakes in any order give the same clusters.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from earthwave.models import Earthquake
from earthwave.sources.geo import haversine_km

SECONDS_PER_DAY = 86400.0


def window_days(mag: float) -> float:
    """Days after a mainshock of magnitude `mag` in which a smaller quake counts as its aftershock (G-K, 1974)."""
    return 10 ** (0.032 * mag + 2.7389) if mag >= 6.5 else 10 ** (0.5409 * mag - 0.547)


def window_km(mag: float) -> float:
    """Radius in km around a mainshock of magnitude `mag` in which a smaller quake counts as its aftershock."""
    return 10 ** (0.1238 * mag + 0.983)


@dataclass(frozen=True, slots=True)
class Cluster:
    mainshock: Earthquake
    aftershocks: tuple[Earthquake, ...]  # smaller than the mainshock and later than it, in (time, id) order


def cluster_aftershocks(quakes: Iterable[Earthquake]) -> list[Cluster]:
    """The clusters with at least one aftershock, biggest mainshock first.

    Quakes are taken biggest first (ties: earlier, then id), so a quake already claimed by a bigger one is never a
    mainshock itself and none is in two clusters. A member is strictly smaller than its mainshock and strictly later.
    """
    ordered = sorted(quakes, key=lambda q: (-q.mag, q.time, q.id))
    claimed: set[str] = set()
    clusters: list[Cluster] = []
    for i, main in enumerate(ordered):
        if main.id in claimed:
            continue
        horizon, radius = window_days(main.mag) * SECONDS_PER_DAY, window_km(main.mag)
        members = [
            q
            for q in ordered[i + 1 :]
            if q.id not in claimed
            and q.mag < main.mag
            and 0 < (q.time - main.time).total_seconds() <= horizon
            and haversine_km(main.lat, main.lon, q.lat, q.lon) <= radius
        ]
        if members:
            claimed.update(q.id for q in members)
            clusters.append(Cluster(main, tuple(sorted(members, key=lambda q: (q.time, q.id)))))
    return clusters


def group_by_mainshock(quakes: Iterable[Earthquake]) -> Mapping[str, tuple[Earthquake, ...]]:
    """Mainshock id -> its aftershocks, for the quakes that have any."""
    return {c.mainshock.id: c.aftershocks for c in cluster_aftershocks(quakes)}
