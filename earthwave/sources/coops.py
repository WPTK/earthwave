"""NOAA CO-OPS tide gauges: stations, water levels, predictions, the observed-minus-predicted residual and the source.

The parsers and math are pure. Callers must request ``time_zone=gmt`` and a metric ``units``/single ``datum`` so
every timestamp here is UTC and every value is metres on one datum; ``CoopsSource`` does.
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import httpx

from earthwave import models
from earthwave.clock import Clock
from earthwave.config import PollConfig
from earthwave.sources.base import SourceHealth

log = logging.getLogger(__name__)

STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=waterlevels"
DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


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


class Prediction(NamedTuple):
    station_id: str
    ts: datetime
    value_m: float


class Residual(NamedTuple):
    station_id: str
    ts: datetime
    observed_m: float
    predicted_m: float
    residual_m: float


class CoopsError(ValueError):
    """CO-OPS answered 200 with an ``{"error": {"message": ...}}`` payload (no data, bad station, bad range)."""


def _load(payload: str | bytes | dict) -> dict:
    data = payload if isinstance(payload, dict) else json.loads(payload)
    error = data.get("error")
    if error:
        raise CoopsError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
    return data


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def parse_stations(payload: str | bytes | dict) -> list[Station]:
    """``mdapi`` stations.json; entries without an id or coordinates are skipped."""
    out: list[Station] = []
    for s in _load(payload).get("stations", []):
        try:
            out.append(Station(str(s["id"]), "coops", (s.get("name") or "").strip(), float(s["lat"]), float(s["lng"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _rows(payload: str | bytes | dict, key: str) -> tuple[str, list[dict]]:
    data = _load(payload)
    station_id = str((data.get("metadata") or {}).get("id", ""))
    return station_id, data.get(key) or []


def _readings(rows: list[dict], station_id: str, mode: str, qualities: frozenset[str] | None) -> list[GaugeReading]:
    out: list[GaugeReading] = []
    for row in rows:
        if qualities is not None and row.get("q") not in qualities:
            continue
        try:
            out.append(GaugeReading(station_id, "coops", _ts(row["t"]), float(row["v"]), mode))
        except (KeyError, TypeError, ValueError):
            continue  # empty "v" is how CO-OPS marks a missing sample
    out.sort(key=lambda r: r.ts)
    return out


def parse_water_level(payload: str | bytes | dict, station_id: str | None = None) -> list[GaugeReading]:
    """6-minute ``water_level`` rows ``{t v s f q}``, mode ``6m``.

    Rows with no value or a quality other than ``p`` (preliminary) or ``v`` (verified) are dropped.
    The ``s`` sigma and ``f`` flag columns are not used.
    """
    meta_id, rows = _rows(payload, "data")
    return _readings(rows, station_id or meta_id, "6m", frozenset("pv"))


def parse_one_minute_water_level(payload: str | bytes | dict, station_id: str | None = None) -> list[GaugeReading]:
    """``one_minute_water_level`` rows ``{t v}`` (no quality column), mode ``1m``."""
    meta_id, rows = _rows(payload, "data")
    return _readings(rows, station_id or meta_id, "1m", None)


def parse_predictions(payload: str | bytes | dict, station_id: str) -> list[Prediction]:
    """``predictions`` rows ``{t v}``; the response has no metadata, so the id is passed in."""
    out: list[Prediction] = []
    for row in _load(payload).get("predictions") or []:
        try:
            out.append(Prediction(station_id, _ts(row["t"]), float(row["v"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda p: p.ts)
    return out


def residuals(observed: list[GaugeReading], predictions: list[Prediction]) -> list[Residual]:
    """Observed minus prediction at identical timestamps; observations with no prediction are omitted."""
    pred = {p.ts: p.value_m for p in predictions}
    return [
        Residual(o.station_id, o.ts, o.value_m, pred[o.ts], o.value_m - pred[o.ts])
        for o in sorted(observed, key=lambda o: o.ts)
        if o.ts in pred
    ]


# --- polling source ---------------------------------------------------------------------------------------------

STATIONS_TTL = 86400.0  # stations.json is cached for a day
STATIONS_RETRY = 600.0
HISTORY = timedelta(hours=6)  # observations requested per poll
PREDICTION_AHEAD = timedelta(hours=6)
PREDICTION_TTL = timedelta(hours=3)  # predictions are refetched when older than this
_DATE = "%Y%m%d %H:%M"


def _params(product: str, station_id: str, begin: datetime, end: datetime, interval: str | None = None) -> dict:
    params = {
        "product": product,
        "station": station_id,
        "begin_date": begin.strftime(_DATE),
        "end_date": end.strftime(_DATE),
        "datum": "MLLW",
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "application": "earthwave",
    }
    if interval:
        params["interval"] = interval
    return params


def to_station(s: Station) -> models.Station:
    return models.Station(s.id, s.kind, s.name, s.lat, s.lon)


def to_reading(r: GaugeReading) -> models.GaugeReading:
    return models.GaugeReading(r.station_id, r.kind, r.ts, r.value_m, r.mode)


class CoopsSource:
    """Polls tide gauges selected with ``set_selection``.

    Cadence: 6-minute ``water_level`` every ``coops_seconds``; while ``set_alert_active(True)``,
    ``one_minute_water_level`` every ``alert_gauge_seconds`` (a gauge without 1-minute data falls back to 6-minute).
    Predictions for the residual are fetched per station at the matching interval and kept in the source; read them
    with ``predictions()``.
    """

    name = "coops"
    min_interval = 30.0

    def __init__(self, clock: Clock, poll: PollConfig | None = None):
        self._clock = clock
        self._poll = poll or PollConfig()
        self._selected: tuple[str, ...] = ()
        self._alert_active = False
        self._stations: list[models.Station] = []
        self._stations_due: float | None = None
        self._predictions: dict[str, tuple[str, datetime, list[Prediction]]] = {}

    def set_selection(self, station_ids: Iterable[str]) -> None:
        self._selected = tuple(station_ids)

    def set_alert_active(self, active: bool) -> None:
        self._alert_active = active

    @property
    def stations(self) -> tuple[models.Station, ...]:
        """Empty until the first successful station fetch."""
        return tuple(self._stations)

    def predictions(self, station_id: str) -> list[Prediction]:
        """The cached predictions matching the interval last fetched for this station."""
        return list(self._predictions.get(station_id, ("", self._clock.now(), []))[2])

    def interval(self, health: SourceHealth) -> float:
        return float(self._poll.alert_gauge_seconds if self._alert_active else self._poll.coops_seconds)

    async def _get(self, client: httpx.AsyncClient, params: dict) -> dict:
        response = await client.get(DATA_URL, params=params)
        response.raise_for_status()
        return _load(response.content)

    async def _refresh_stations(self, client: httpx.AsyncClient) -> tuple[models.Station, ...]:
        if self._stations_due is not None and self._clock.monotonic() < self._stations_due:
            return ()
        try:
            response = await client.get(STATIONS_URL)
            response.raise_for_status()
            fresh = [to_station(s) for s in parse_stations(response.content)]
        except (httpx.HTTPError, ValueError) as exc:
            if not self._stations:
                raise
            log.warning("coops: station list unavailable, keeping %d known (%r)", len(self._stations), exc)
            fresh = []
        if not fresh:
            self._stations_due = self._clock.monotonic() + STATIONS_RETRY
            return ()
        self._stations = fresh
        self._stations_due = self._clock.monotonic() + STATIONS_TTL
        return tuple(fresh)

    async def _predictions_for(self, client: httpx.AsyncClient, station_id: str, interval: str, now: datetime) -> None:
        cached = self._predictions.get(station_id)
        if cached and cached[0] == interval and now - cached[1] < PREDICTION_TTL:
            return
        params = _params("predictions", station_id, now - HISTORY, now + PREDICTION_AHEAD, interval)
        try:
            preds = parse_predictions(await self._get(client, params), station_id)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("coops: no predictions for %s (%r)", station_id, exc)
            return
        self._predictions[station_id] = (interval, now, preds)

    async def _observe(self, client: httpx.AsyncClient, station_id: str, now: datetime) -> list[GaugeReading]:
        begin = now - HISTORY
        if self._alert_active:
            try:
                data = await self._get(client, _params("one_minute_water_level", station_id, begin, now))
                await self._predictions_for(client, station_id, "1", now)
                return parse_one_minute_water_level(data, station_id)
            except CoopsError:
                pass  # no 1-minute data at this gauge: use the 6-minute product
        data = await self._get(client, _params("water_level", station_id, begin, now))
        await self._predictions_for(client, station_id, "6", now)
        return parse_water_level(data, station_id)

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> models.Batch:
        stations = await self._refresh_stations(client)
        now = self._clock.now()
        readings: list[models.GaugeReading] = []
        failures: list[Exception] = []
        ok = 0
        for station_id in self._selected:
            try:
                rows = await self._observe(client, station_id, now)
            except (httpx.HTTPError, ValueError) as exc:  # CoopsError is a ValueError: a gauge with no data is skipped
                failures.append(exc)
                continue
            ok += 1
            readings.extend(to_reading(r) for r in rows)
        if failures and not ok:
            raise failures[-1]
        return models.Batch(self.name, now, readings=tuple(readings), stations=stations)
