"""NWS API source: active tsunami warnings, advisories and watches for US areas."""

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import levels
from earthwave.sources.base import SourceHealth
from earthwave.sources.tsunamigov import dedupe_key, parse_time

log = logging.getLogger(__name__)

URL = "https://api.weather.gov/alerts/active"
EVENTS = "Tsunami Warning,Tsunami Advisory,Tsunami Watch"
RETRY_AFTER_CAP = 10.0  # seconds; a 429 is retried once after the server's hint (about 5 s), capped here


def retry_after(header: str | None, now: datetime, default: float = 5.0) -> float:
    """Seconds to wait from a Retry-After header: a number of seconds or an HTTP date."""
    if not header:
        return default
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return default
    return max(0.0, (when - now).total_seconds()) if when.tzinfo else default


def parse_geometry(geometry: dict | None) -> tuple | None:
    """First ring of a GeoJSON Polygon (or of the first MultiPolygon member) as ("polygon", ((lon, lat), ...))."""
    if not geometry:
        return None
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiPolygon" and coords:
        coords = coords[0]
    if geometry.get("type") in ("Polygon", "MultiPolygon") and coords and coords[0]:
        return ("polygon", tuple((float(p[0]), float(p[1])) for p in coords[0]))
    return None


def parse_feature(feature: dict) -> TsunamiAlert | None:
    props = feature["properties"]
    if str(props.get("status") or "Actual").lower() != "actual":  # Test, Exercise, System, Draft
        return None
    event = props.get("event") or ""
    level = levels.level_from_text(event)
    if str(props.get("messageType", "")).lower() == "cancel" or "cancellation" in (props.get("headline") or "").lower():
        level = levels.CANCELLATION
    if level is None:
        return None
    sent = parse_time(props.get("sent"))
    wmo = (props.get("parameters") or {}).get("WMOidentifier") or []
    ident = props.get("id") or feature.get("id") or ""
    header = " ".join(str(wmo[0]).split()) if wmo else ident
    areas = tuple(a.strip() for a in (props.get("areaDesc") or "").split(";") if a.strip())
    expires = parse_time(props.get("ends")) or parse_time(props.get("expires"))
    text = "\n\n".join(filter(None, (props.get("description"), props.get("instruction"))))
    return TsunamiAlert(
        id=ident,
        dedupe_key=dedupe_key(header, sent) if sent else header,
        source="nws",
        level=level,
        headline=props.get("headline") or event,
        areas=areas,
        issued=parse_time(props.get("effective")) or sent,
        expires=expires,
        updated=sent,
        geometry=parse_geometry(feature.get("geometry")),
        text=text,
        references=tuple(r["identifier"] for r in props.get("references") or [] if r.get("identifier")),
    )


def parse_features(data: dict) -> tuple[tuple[TsunamiAlert, ...], int]:
    """The alerts of a /alerts/active reply and how many features could not be read. Raises when there is no list."""
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise ValueError("nws: reply has no features list")
    alerts, skipped = [], 0
    for feature in features:
        try:
            alert = parse_feature(feature)
        except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            log.warning("nws: skipping malformed alert: %r", exc)
            skipped += 1
            continue
        if alert is not None:
            alerts.append(alert)
    alerts.sort(key=lambda a: a.updated or a.issued or datetime.min.replace(tzinfo=UTC))
    return tuple(alerts), skipped


def parse_alerts(data: dict) -> tuple[TsunamiAlert, ...]:
    return parse_features(data)[0]


class NwsSource:
    name = "nws"
    min_interval = 60.0

    def __init__(self, clock: Clock, interval_seconds: float = 60):
        self._clock = clock
        self._interval = max(self.min_interval, float(interval_seconds))

    def interval(self, health: SourceHealth) -> float:
        return self._interval

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        params = {"event": EVENTS, "status": "actual"}
        headers = {"Accept": "application/geo+json"}
        response = await client.get(URL, params=params, headers=headers)
        if response.status_code == 429:
            wait = min(retry_after(response.headers.get("Retry-After"), self._clock.now()), RETRY_AFTER_CAP)
            log.debug("nws: 429, retrying in %.0fs", wait)
            await self._clock.sleep(wait)
            response = await client.get(URL, params=params, headers=headers)
        response.raise_for_status()
        alerts, skipped = parse_features(response.json())
        log.debug("nws: %d alerts, %d unreadable", len(alerts), skipped)
        # A listing with unreadable entries is not a full answer: an alert missing from it may only be unreadable.
        return Batch(self.name, self._clock.now(), alerts=alerts, meta={"incomplete": bool(skipped)})
