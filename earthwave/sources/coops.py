"""NOAA CO-OPS tide gauges: stations, water levels, predictions and the observed-minus-predicted residual.

Pure parsing and math: no network, no UI. Callers must request ``time_zone=gmt`` and a metric
``units``/single ``datum`` so every timestamp here is UTC and every value is metres on one datum.
"""

import json
from datetime import UTC, datetime
from typing import NamedTuple

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
