"""Normalized data shapes shared by every source, the store and the UI. All timestamps are aware UTC datetimes."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Earthquake:
    id: str  # canonical id: the USGS event id
    time: datetime
    lat: float
    lon: float
    mag: float
    depth_km: float = 0.0
    source: str = "usgs"
    mag_type: str = ""
    place: str = ""
    status: str = "automatic"  # automatic | reviewed
    tsunami_flag: bool = False
    pager_alert: str | None = None  # green | yellow | orange | red
    sig: int = 0
    felt: int | None = None
    cdi: float | None = None
    mmi: float | None = None
    updated: datetime | None = None
    url: str = ""
    detail_url: str = ""
    also_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuakeDetail:
    """Extra fields from the lazily fetched USGS detail product list."""

    id: str
    pager_alert: str | None = None
    pager_max_mmi: float | None = None
    shakemap_max_mmi: float | None = None
    shakemap_max_pga: float | None = None  # %g
    shakemap_max_pgv: float | None = None  # cm/s
    dyfi_max_mmi: float | None = None
    dyfi_responses: int | None = None
    nearby_cities: tuple[str, ...] = ()
    products: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TsunamiAlert:
    id: str
    dedupe_key: str
    source: str
    level: str  # WARNING | ADVISORY | WATCH | THREAT | INFORMATION | CANCELLATION
    headline: str
    areas: tuple[str, ...] = ()
    issued: datetime | None = None
    expires: datetime | None = None
    updated: datetime | None = None
    geometry: tuple | None = None  # ("circle", lat, lon, radius_km) | ("polygon", ((lon, lat), ...)) | None
    text: str = ""
    quake_ref: str | None = None
    references: tuple[str, ...] = ()  # ids of the alerts this one updates or cancels
    origin: tuple[datetime, float, float] | None = None  # (time, lat, lon) of the quake the product names


@dataclass(frozen=True, slots=True)
class Station:
    id: str
    kind: str  # dart | coops
    name: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class GaugeReading:
    station_id: str
    kind: str  # dart | coops
    ts: datetime
    value_m: float
    mode: str  # 15m | 1m | 15s | 6m


@dataclass(frozen=True, slots=True)
class Batch:
    """What one fetch returns. `cursor` is opaque to the scheduler and handed back to the same source's next fetch.

    `backfill` batches (startup feeds, filter queries) merge silently. `coverage` is (hours, min_magnitude): the
    window of quakes this batch is known to contain completely.
    """

    source: str
    fetched_at: datetime
    quakes: tuple[Earthquake, ...] = ()
    alerts: tuple[TsunamiAlert, ...] = ()
    readings: tuple[GaugeReading, ...] = ()
    stations: tuple[Station, ...] = ()
    not_modified: bool = False
    backfill: bool = False
    cursor: str | None = None
    coverage: tuple[float, float] | None = None
    meta: dict = field(default_factory=dict)
