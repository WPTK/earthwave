"""Great-circle math, longitude wrapping and compass text."""

import math
from collections.abc import Callable, Collection, Iterable, Sequence

from earthwave.models import Station

EARTH_RADIUS_KM = 6371.0088
_COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def wrap_lon(lon: float) -> float:
    """Wrap a longitude into [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def lon_delta(lon_a: float, lon_b: float) -> float:
    """Signed shortest longitude difference b - a in degrees, across the dateline."""
    return wrap_lon(lon_b - lon_a)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon_delta(lon1, lon2))
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north in [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon_delta(lon1, lon2))
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return math.degrees(math.atan2(y, x)) % 360.0


def compass(bearing: float) -> str:
    """16-point compass name for a bearing in degrees."""
    return _COMPASS[int((bearing % 360.0) / 22.5 + 0.5) % 16]


def distance_text(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """e.g. '1234 km NNE' from point 1 to point 2."""
    km = haversine_km(lat1, lon1, lat2, lon2)
    return f"{km:.0f} km {compass(bearing_deg(lat1, lon1, lat2, lon2))}"


def nearest_n[T](
    items: Iterable[T],
    lat: float,
    lon: float,
    n: int,
    max_km: float | None = None,
    key: Callable[[T], tuple[float, float]] = lambda item: (item.lat, item.lon),  # type: ignore[attr-defined]
) -> list[tuple[float, T]]:
    """The n items closest to (lat, lon) as (distance_km, item), nearest first, optionally within max_km."""
    ranked = []
    for item in items:
        item_lat, item_lon = key(item)
        km = haversine_km(lat, lon, item_lat, item_lon)
        if max_km is None or km <= max_km:
            ranked.append((km, item))
    ranked.sort(key=lambda pair: pair[0])
    return ranked[:n]


DART_COUNT = 6
DART_MAX_KM = 3000.0
COOPS_COUNT = 4
MAX_GEOMETRY_POINTS = 48  # polygon vertices ranked against; enough to find the nearest coast without a slow scan

Point = tuple[float, float]  # (lat, lon)


def epicentre(*candidates: Point | None) -> Point | None:
    """The first known source-quake position, in preference order (CAP EventLatLon, then the linked USGS quake)."""
    return next((c for c in candidates if c is not None), None)


def geometry_points(geometry: tuple | None, quake: Point | None = None) -> list[Point]:
    """Points that stand for an alert's footprint: NWS polygon vertices, else the CAP circle centre, else the quake.

    `geometry` is `TsunamiAlert.geometry`; polygon vertices are (lon, lat) and are thinned to MAX_GEOMETRY_POINTS.
    """
    if geometry:
        if geometry[0] == "circle":
            return [(float(geometry[1]), float(geometry[2]))]
        if geometry[0] == "polygon" and geometry[1]:
            ring = geometry[1]
            step = max(1, math.ceil(len(ring) / MAX_GEOMETRY_POINTS))
            return [(float(lat), float(lon)) for lon, lat in ring[::step]]
    return [quake] if quake is not None else []


def select_dart(
    stations: Iterable[Station],
    source: Point,
    n: int = DART_COUNT,
    max_km: float = DART_MAX_KM,
    exclude: Collection[str] = (),
) -> list[tuple[float, Station]]:
    """The n DART stations nearest the source quake within max_km, as (distance_km, station), nearest first.

    `exclude` drops stations known not to answer (the 404s in activestations.xml).
    """
    live = (s for s in stations if s.kind == "dart" and s.id not in exclude)
    return nearest_n(live, source[0], source[1], n, max_km)


def select_coops(
    stations: Iterable[Station], points: Sequence[Point], n: int = COOPS_COUNT
) -> list[tuple[float, Station]]:
    """The n tide gauges nearest any of `points` (see geometry_points), as (distance_km, station), nearest first."""
    if not points:
        return []
    best: list[tuple[float, Station]] = []
    for s in stations:
        if s.kind != "coops":
            continue
        best.append((min(haversine_km(lat, lon, s.lat, s.lon) for lat, lon in points), s))
    best.sort(key=lambda pair: pair[0])
    return best[:n]
