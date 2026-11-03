"""Aftershock clustering: group quakes that look like the sequence of a larger one (Gardner and Knopoff, 1974).

A quake is an aftershock of a larger one when it follows it within a time and distance window that grows with the
larger quake's magnitude. Pure and deterministic: the same quakes in any order give the same clusters.
"""

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import floor, isfinite

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


KM_PER_DEGREE = 110.0  # under the true 110.6-111.7 km, so the latitude prefilter never drops a quake in range


def cluster_aftershocks(quakes: Iterable[Earthquake]) -> list[Cluster]:
    """The clusters with at least one aftershock, biggest mainshock first.

    Quakes are taken biggest first (ties: earlier, then id), so a quake already claimed by a bigger one is never a
    mainshock itself and none is in two clusters. A member is strictly smaller than its mainshock and strictly later.
    A quake with a magnitude that is not a number is left out. Quakes are kept per degree of latitude, in time order,
    so a mainshock looks only at the bands its radius reaches and, by bisection, the window after it: a few thousand
    quakes take milliseconds. Longitude is not prefiltered, so the antimeridian and the poles need no special case.
    """
    ordered = sorted((q for q in quakes if isfinite(q.mag)), key=lambda q: (-q.mag, q.time, q.id))
    bands: dict[int, list[Earthquake]] = {}
    for q in sorted(ordered, key=lambda q: (q.time, q.id)):
        bands.setdefault(floor(q.lat), []).append(q)
    stamps = {band: [q.time.timestamp() for q in members] for band, members in bands.items()}
    claimed: set[str] = set()
    clusters: list[Cluster] = []
    for main in ordered:
        if main.id in claimed:
            continue
        start = main.time.timestamp()
        horizon, radius = start + window_days(main.mag) * SECONDS_PER_DAY, window_km(main.mag)
        reach = radius / KM_PER_DEGREE
        members: list[Earthquake] = []
        for band in range(floor(main.lat - reach), floor(main.lat + reach) + 1):
            if band not in bands:
                continue
            times, candidates = stamps[band], bands[band]
            for i in range(bisect_right(times, start), bisect_right(times, horizon)):
                q = candidates[i]
                if (
                    q.id not in claimed
                    and q.mag < main.mag
                    and q.time > main.time
                    and haversine_km(main.lat, main.lon, q.lat, q.lon) <= radius
                ):
                    members.append(q)
        if members:
            claimed.update(q.id for q in members)
            clusters.append(Cluster(main, tuple(sorted(members, key=lambda q: (q.time, q.id)))))
    return clusters


def group_by_mainshock(quakes: Iterable[Earthquake]) -> Mapping[str, tuple[Earthquake, ...]]:
    """Mainshock id -> its aftershocks, for the quakes that have any."""
    return {c.mainshock.id: c.aftershocks for c in cluster_aftershocks(quakes)}
