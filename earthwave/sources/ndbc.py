"""NDBC DART buoys: station list, ``.dart`` rows, event-mode detection and cubic detrend.

Pure parsing and math: no network, no UI. The records mirror the plan's ``Station`` and
``GaugeReading`` so a later step can adapt them to the shared models.
"""

import itertools
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

STATIONS_URL = "https://www.ndbc.noaa.gov/activestations.xml"
DART_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.dart"

# The ``T`` column of a .dart row: 1 = standard 15-minute, 2 = event 1-minute, 3 = event 15-second.
MODES = {1: "15m", 2: "1m", 3: "15s"}
MISSING = 99.999
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

    Comment and blank lines, short or malformed rows, unknown ``T`` codes and the 99.999
    missing-value marker are skipped. Timestamps are UTC.
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
        if t not in MODES or height == MISSING:
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
