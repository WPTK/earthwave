"""JMA tsunami source (undocumented bosai endpoint): event list, then the detail file of each unseen report."""

import logging
from dataclasses import replace
from datetime import timedelta

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import levels
from earthwave.sources.base import SourceHealth
from earthwave.sources.tsunamigov import parse_time

log = logging.getLogger(__name__)

BASE = "https://www.jma.go.jp/bosai/tsunami/data/"
MAX_AGE = timedelta(hours=48)  # older reports are history, not alerts
SEEN_LIMIT = 200

# Category.Kind.Code from JMA's published XML format specification (tsunami warning/advisory/forecast element codes).
KIND_LEVELS = {
    "51": levels.WARNING,  # tsunami warning
    "52": levels.WARNING,  # major tsunami warning (special warning)
    "53": levels.WARNING,  # major tsunami warning
    "62": levels.ADVISORY,  # tsunami advisory
    "71": levels.INFORMATION,  # forecast: slight sea-level change
    "72": levels.INFORMATION,  # forecast: tsunami influence remains
    "73": levels.INFORMATION,  # forecast: sea-level change continues
    "50": levels.CANCELLATION,  # warning lifted
    "60": levels.CANCELLATION,  # advisory lifted
}
CANCEL_INFO_TYPE = "\u53d6\u6d88"  # InfoType of a report that cancels an earlier one


def parse_coordinate(text: str) -> tuple[float, float] | None:
    """JMA ISO 6709 style '+07.5-080.8/' (optionally with depth) to (lat, lon)."""
    body = text.split("/")[0]
    for i in range(1, len(body)):
        if body[i] in "+-":
            try:
                return float(body[:i]), float(body[i:])
            except ValueError:
                return None
    return None


def parse_detail(detail: dict, entry: dict) -> TsunamiAlert:
    """One detail file (with its list entry for the English title and file name) to a TsunamiAlert."""
    head, body = detail.get("Head", {}), detail.get("Body", {})
    items = (body.get("Tsunami") or {}).get("Forecast", {}).get("Item") or []
    codes = [i["Category"]["Kind"]["Code"] for i in items]
    ranked = [KIND_LEVELS[c] for c in codes if c in KIND_LEVELS]
    level = max(ranked, key=levels.RANK.__getitem__) if ranked else levels.INFORMATION
    if head.get("InfoType") == CANCEL_INFO_TYPE:
        level = levels.CANCELLATION
    reported = parse_time(head.get("ReportDateTime") or entry.get("rdt"))
    quake = (body.get("Earthquake") or [{}])[0]
    origin = None
    coord = parse_coordinate(((quake.get("Hypocenter") or {}).get("Area") or {}).get("Coordinate", ""))
    origin_time = parse_time(quake.get("OriginTime"))
    if coord and origin_time:
        origin = (origin_time, coord[0], coord[1])
    areas = tuple(
        i["Area"].get("enName") or i["Area"].get("Name", "")
        for i in items
        if i["Category"]["Kind"]["Code"] in KIND_LEVELS
    )
    comment = (body.get("Comments") or {}).get("WarningComment") or {}
    title = entry.get("en_ttl") or head.get("enTitle") or "Tsunami report"
    place = entry.get("en_anm") or ""
    mag = entry.get("mag")
    headline = f"JMA {title}" + (f": {place}" if place else "") + (f" M{mag}" if mag else "")
    return TsunamiAlert(
        id=f"jma:{entry['json']}",
        dedupe_key=f"jma:{entry['json']}",
        source="jma",
        level=level,
        headline=headline,
        areas=areas,
        issued=reported,
        expires=parse_time(head.get("ValidDateTime")),
        updated=reported,
        geometry=("circle", coord[0], coord[1], 0.0) if coord else None,
        text=(comment.get("enText") or comment.get("Text") or "").strip(),
        origin=origin,
    )


class JmaSource:
    name = "jma"
    min_interval = 60.0

    def __init__(self, clock: Clock, interval_seconds: float = 60):
        self._clock = clock
        self._interval = max(self.min_interval, float(interval_seconds))
        self._seen: dict[str, None] = {}
        self._ids_by_event: dict[str, list[str]] = {}

    def interval(self, health: SourceHealth) -> float:
        return self._interval

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        response = await client.get(f"{BASE}list.json")
        response.raise_for_status()
        entries = response.json()
        now = self._clock.now()
        alerts: list[TsunamiAlert] = []
        # Oldest first, so a later report supersedes or cancels the earlier ones of the same event.
        for entry in sorted(entries, key=lambda e: e.get("ctt", "")):
            name = entry.get("json")
            reported = parse_time(entry.get("rdt"))
            if not name or name in self._seen or reported is None or now - reported > MAX_AGE:
                continue
            try:
                detail = await client.get(f"{BASE}{name}")
                detail.raise_for_status()
                alert = parse_detail(detail.json(), entry)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
                log.warning("jma: skipping %s: %r", name, exc)
                continue
            earlier = self._ids_by_event.setdefault(entry["eid"], [])
            alert = replace(alert, references=tuple(earlier))
            earlier.append(alert.id)
            self._seen[name] = None
            while len(self._seen) > SEEN_LIMIT:
                del self._seen[next(iter(self._seen))]
            alerts.append(alert)
        log.debug("jma: %d new reports", len(alerts))
        return Batch(self.name, now, alerts=tuple(alerts))
