"""NDBC DART buoys: station list, ``.dart`` rows, event-mode detection, cubic detrend and the polling source.

The parsers and math are pure. ``DartSource`` is the network half: it adapts the NamedTuples here to the
shared models and picks its own cadence.
"""

import itertools
import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import NamedTuple

import httpx

from earthwave import models
from earthwave.clock import Clock
from earthwave.config import PollConfig
from earthwave.sources.base import SourceHealth

log = logging.getLogger(__name__)

STATIONS_URL = "https://www.ndbc.noaa.gov/activestations.xml"
DART_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.dart"

# The ``T`` column of a .dart row: 1 = standard 15-minute, 2 = event 1-minute, 3 = event 15-second.
MODES = {1: "15m", 2: "1m", 3: "15s"}
# NDBC marks a missing value with a run of 9s whose width depends on the field. The short ones fall inside the
# physical range, so they are listed; 9999.000 and the like are caught by the range, which also drops zero, negative
# and NaN heights. DART buoys moor in water from a few hundred to about 7,000 m, so 9,000 m leaves margin and stays
# below the 9999 marker.
MISSING = frozenset({99.999, 999.999})
MIN_HEIGHT_M = 1.0
MAX_HEIGHT_M = 9000.0
DETREND_WINDOW = timedelta(hours=3)
DETREND_DEGREE = 3
MIN_FIT_POINTS = 8
MAX_GAP = timedelta(minutes=30)
MIN_SPAN = DETREND_WINDOW / 2
EVENT_WINDOW = timedelta(minutes=30)


class Station(NamedTuple):
    id: str
    kind: str
    name: str
    lat: float
    lon: float


class GaugeReading(NamedTuple):
    station_id: str
    kind: str
    ts: datetime
    value_m: float
    mode: str


class Residual(NamedTuple):
    station_id: str
    ts: datetime
    value_m: float
    trend_m: float
    residual_m: float


def parse_active_stations(xml_text: str | bytes) -> list[Station]:
    """DART rows (``type="dart"``) of ``activestations.xml``; rows without coordinates are skipped."""
    out: list[Station] = []
    for el in ET.fromstring(xml_text).iter("station"):
        if el.get("type") != "dart":
            continue
        try:
            lat, lon = float(el.get("lat", "")), float(el.get("lon", ""))
        except ValueError:
            continue
        out.append(Station(el.get("id", "").strip(), "dart", (el.get("name") or "").strip(), lat, lon))
    return out


def parse_dart(text: str, station_id: str) -> list[GaugeReading]:
    """Parse a ``.dart`` file into readings, oldest first.

    Comment and blank lines, short or malformed rows, unknown ``T`` codes and the missing-value
    markers and heights outside ``MIN_HEIGHT_M``..``MAX_HEIGHT_M`` are skipped. Timestamps are UTC.
    """
    rows: list[GaugeReading] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 8 or line.startswith("#"):
            continue
        try:
            yy, mo, dd, hh, mi, ss, t = (int(p) for p in parts[:7])
            height = float(parts[7])
            ts = datetime(yy, mo, dd, hh, mi, ss, tzinfo=UTC)
        except ValueError:
            continue
        if t not in MODES or height in MISSING or not MIN_HEIGHT_M <= height <= MAX_HEIGHT_M:
            continue
        rows.append(GaugeReading(station_id, "dart", ts, height, MODES[t]))
    rows.sort(key=lambda r: r.ts)
    return rows


def in_event_mode(readings: list[GaugeReading], window: timedelta = EVENT_WINDOW) -> bool:
    """True when any reading within ``window`` of the newest one is 1-minute or 15-second data."""
    if not readings:
        return False
    newest = max(r.ts for r in readings)
    return any(r.mode != "15m" and r.ts >= newest - window for r in readings)


def _polyfit_at_zero(xs: list[float], ys: list[float], degree: int) -> float:
    """Least-squares polynomial fit through (xs, ys), evaluated at x = 0 (the constant term)."""
    n = degree + 1
    # Normal equations A c = b with A[i][j] = sum x^(i+j); xs are small, centred values.
    pw = [0.0] * (2 * degree + 1)
    b = [0.0] * n
    for x, y in zip(xs, ys, strict=True):
        p = 1.0
        for k in range(2 * degree + 1):
            pw[k] += p
            if k < n:
                b[k] += p * y
            p *= x
    a = [[pw[i + j] for j in range(n)] + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[piv] = a[piv], a[col]
        if a[col][col] == 0.0:
            raise ValueError("singular fit")
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    coef = [0.0] * n
    for i in range(n - 1, -1, -1):
        coef[i] = (a[i][n] - sum(a[i][j] * coef[j] for j in range(i + 1, n))) / a[i][i]
    return coef[0]


def detrend(
    readings: list[GaugeReading],
    window: timedelta = DETREND_WINDOW,
    degree: int = DETREND_DEGREE,
    since: datetime | None = None,
) -> list[Residual]:
    """Residual of each reading against a cubic fitted to the prior ``window`` of readings.

    This is NOAA's detiding approach: the fit uses only readings strictly before the point,
    extrapolated to it. A point gets no residual unless the prior window holds at least
    ``MIN_FIT_POINTS`` distinct timestamps spanning at least ``MIN_SPAN`` with no hole longer
    than ``MAX_GAP`` anywhere in it, including between the newest prior reading and the point
    (extrapolating across a batch hole gives centimetre-scale errors). Rows sharing a timestamp
    are collapsed to the last one. ``since`` limits output (not the fit) to readings at or
    after that time. Mixed 15-minute and 1-minute data is fine; the fit is over whatever rows exist.
    """
    rows = sorted({r.ts: r for r in readings}.values(), key=lambda r: r.ts)
    span = window.total_seconds()
    out: list[Residual] = []
    lo = 0
    for i, r in enumerate(rows):
        if since is not None and r.ts < since:
            continue
        while rows[lo].ts < r.ts - window:
            lo += 1
        prior = [p for p in rows[lo:i] if p.ts < r.ts]
        if len(prior) < max(MIN_FIT_POINTS, degree + 1) or r.ts - prior[0].ts < MIN_SPAN:
            continue  # too little history to fit
        times = [p.ts for p in prior] + [r.ts]
        if any(b - a > MAX_GAP for a, b in itertools.pairwise(times)):
            continue  # a data hole inside the window
        xs = [(p.ts - r.ts).total_seconds() / span for p in prior]
        try:
            trend = _polyfit_at_zero(xs, [p.value_m for p in prior], degree)
        except ValueError:
            continue
        out.append(Residual(r.station_id, r.ts, r.value_m, trend, r.value_m - trend))
    return out


# --- polling source ---------------------------------------------------------------------------------------------

STATIONS_TTL = 3600.0  # activestations.xml is refetched hourly
STATIONS_RETRY = 600.0  # after a failed refresh, keep the known list and retry in ten minutes
UNAVAILABLE_RETRY = 600.0  # a buoy whose file answered 404 is left out of the selection this long, then tried again
UNAVAILABLE_MAX = 6 * 3600.0  # the wait doubles with every further 404 in a row, up to this
HISTORY = timedelta(hours=12)  # readings kept per station per batch: the 3 h fit plus room to draw


def load_bundled_stations() -> list[models.Station]:
    """The bundled snapshot of NDBC's DART stations (``data/dart_stations.json``)."""
    data = json.loads(resources.files("earthwave").joinpath("data/dart_stations.json").read_text(encoding="utf-8"))
    return [models.Station(s["id"], "dart", s["name"], float(s["lat"]), float(s["lon"])) for s in data["stations"]]


def to_station(s: Station) -> models.Station:
    return models.Station(s.id, s.kind, s.name, s.lat, s.lon)


def to_reading(r: GaugeReading) -> models.GaugeReading:
    return models.GaugeReading(r.station_id, r.kind, r.ts, r.value_m, r.mode)


class DartSource:
    """Polls ``.dart`` files for the selected stations only (see ``set_selection``).

    Cadence: ``dart_seconds`` normally, ``alert_gauge_seconds`` while ``set_alert_active(True)`` or while a selected
    buoy's newest reading is 1-minute/15-second data no older than ``EVENT_WINDOW`` on the injected clock. A buoy
    that fails to answer keeps its last event state. The batch cursor and ``since`` are not used by design: each
    poll reads the whole file and the store merges duplicates.
    """

    name = "dart"
    min_interval = 30.0  # alert_gauge_seconds may be configured as low as 30

    def __init__(self, clock: Clock, poll: PollConfig | None = None):
        self._clock = clock
        self._poll = poll or PollConfig()
        self._selected: tuple[str, ...] = ()
        self._alert_active = False
        self._stations = load_bundled_stations()
        self._stations_due: float | None = None  # clock.monotonic() when activestations.xml is next refetched
        self._strikes: dict[str, int] = {}  # consecutive 404s per station
        self._unavailable: dict[str, float] = {}  # station -> clock.monotonic() when it is tried again
        self._event_until: dict[str, datetime] = {}  # per station; only a fresh answer from that station changes it
        self._unsent: tuple[models.Station, ...] = ()

    def set_selection(self, station_ids: Iterable[str]) -> None:
        self._selected = tuple(station_ids)
        self._event_until = {k: v for k, v in self._event_until.items() if k in self._selected}

    def set_alert_active(self, active: bool) -> None:
        """Hint from the alert store: a Warning, Advisory or Watch is active (poll at ``alert_gauge_seconds``)."""
        self._alert_active = active

    @property
    def stations(self) -> tuple[models.Station, ...]:
        """The station list as last fetched, or the bundled snapshot before the first fetch."""
        return tuple(self._stations)

    def mark_unavailable(self, station_id: str) -> None:
        """The file answered 404: leave the buoy out of the selection, twice as long for each 404 in a row."""
        strikes = self._strikes[station_id] = self._strikes.get(station_id, 0) + 1
        wait = min(UNAVAILABLE_RETRY * 2 ** (strikes - 1), UNAVAILABLE_MAX)
        self._unavailable[station_id] = self._clock.monotonic() + wait

    @property
    def unavailable(self) -> frozenset[str]:
        """Stations whose file answered 404 less than ``UNAVAILABLE_RETRY`` ago; selection should skip them."""
        now = self._clock.monotonic()
        return frozenset(k for k, retry in self._unavailable.items() if retry > now)

    def event_mode(self) -> bool:
        now = self._clock.now()
        return any(now < until for until in self._event_until.values())

    def interval(self, health: SourceHealth) -> float:
        fast = self._alert_active or self.event_mode()
        return float(self._poll.alert_gauge_seconds if fast else self._poll.dart_seconds)

    async def _refresh_stations(self, client: httpx.AsyncClient) -> None:
        """Refetch the list when due; the new list waits in `_unsent` until a batch carrying it is returned."""
        if self._stations_due is not None and self._clock.monotonic() < self._stations_due:
            return
        try:
            response = await client.get(STATIONS_URL)
            response.raise_for_status()
            fresh = [to_station(s) for s in parse_active_stations(response.content)]
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("dart: station list unavailable, keeping %d known (%r)", len(self._stations), exc)
            fresh = []
        if not fresh:
            self._stations_due = self._clock.monotonic() + STATIONS_RETRY
            return
        self._stations = fresh
        self._unsent = tuple(fresh)
        self._stations_due = self._clock.monotonic() + STATIONS_TTL

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> models.Batch:
        await self._refresh_stations(client)
        readings: list[models.GaugeReading] = []
        failures: list[Exception] = []
        ok = 0
        for station_id in self._selected:
            try:
                response = await client.get(DART_URL.format(station_id=station_id))
                if response.status_code == 404:
                    self.mark_unavailable(station_id)
                    self._event_until.pop(station_id, None)
                    continue
                response.raise_for_status()
            except httpx.HTTPError as exc:
                failures.append(exc)
                continue
            ok += 1
            self._unavailable.pop(station_id, None)
            self._strikes.pop(station_id, None)
            rows = parse_dart(response.text, station_id)
            self._event_until.pop(station_id, None)
            if rows:
                newest = rows[-1].ts
                rows = [r for r in rows if r.ts >= newest - HISTORY]
                if in_event_mode(rows):
                    self._event_until[station_id] = newest + EVENT_WINDOW
            readings.extend(to_reading(r) for r in rows)
        if failures and not ok:
            raise failures[-1]
        stations, self._unsent = self._unsent, ()  # a fetch that raised above keeps them for the next batch
        return models.Batch(self.name, self._clock.now(), readings=tuple(readings), stations=stations)
