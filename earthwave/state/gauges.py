"""Which gauge stations to watch: station selection from the alerts in force and a quake of interest."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from earthwave.clock import Clock
from earthwave.models import Earthquake, Station
from earthwave.sources import geo
from earthwave.state.store import Store


@dataclass(frozen=True, slots=True)
class GaugeSelection:
    """The stations to poll: (distance_km, station) per source, nearest first."""

    dart: tuple[tuple[float, Station], ...] = ()
    coops: tuple[tuple[float, Station], ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(s.id for _, s in (*self.dart, *self.coops))

    @property
    def dart_ids(self) -> tuple[str, ...]:
        return tuple(s.id for _, s in self.dart)

    @property
    def coops_ids(self) -> tuple[str, ...]:
        return tuple(s.id for _, s in self.coops)


def _merge(found: Iterable[tuple[float, Station]], into: dict[str, tuple[float, Station]]) -> None:
    for km, station in found:
        if station.id not in into or km < into[station.id][0]:
            into[station.id] = (km, station)


def _nearest_first(found: dict[str, tuple[float, Station]]) -> tuple[tuple[float, Station], ...]:
    return tuple(sorted(found.values(), key=lambda pair: pair[0]))


def choose_stations(
    store: Store,
    clock: Clock,
    dart_stations: Iterable[Station],
    coops_stations: Iterable[Station],
    unavailable: Collection[str] = (),
    quake: Earthquake | None = None,
) -> GaugeSelection:
    """Nearest DART buoys to each source quake and nearest tide gauges to each alert's footprint.

    Every alert in force contributes: the epicentre is the CAP origin, else the linked USGS quake; the footprint is
    the NWS geometry, else the CAP circle, else the epicentre (geo.geometry_points). `quake` is the one the user
    asked about with `o`. Stations are merged across sources of interest, keeping the shortest distance.
    """
    dart_list, coops_list = list(dart_stations), list(coops_stations)
    dart: dict[str, tuple[float, Station]] = {}
    coops: dict[str, tuple[float, Station]] = {}
    targets: list[tuple[geo.Point | None, list[geo.Point], float]] = []
    for alert in store.active_alerts(clock):
        linked = store.quake_for(alert)
        epi = geo.epicentre(
            (alert.origin[1], alert.origin[2]) if alert.origin else None,
            (linked.lat, linked.lon) if linked else None,
        )
        targets.append((epi, geo.geometry_points(alert.geometry, epi), geo.footprint_radius_km(alert.geometry)))
    if quake is not None:
        here = (quake.lat, quake.lon)
        targets.append((here, [here], 0.0))
    for epi, points, radius in targets:
        if epi is not None:
            _merge(geo.select_dart(dart_list, epi, exclude=unavailable), dart)
        _merge(geo.select_coops(coops_list, points, radius_km=radius), coops)
    return GaugeSelection(_nearest_first(dart), _nearest_first(coops))
