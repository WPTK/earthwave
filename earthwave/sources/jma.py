"""JMA tsunami source (undocumented bosai endpoint): event list, then the detail file of each unseen report."""

import asyncio
import logging
import re
from dataclasses import replace
from datetime import timedelta

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import levels
from earthwave.sources.base import ENTRY_BUDGET, SourceHealth
from earthwave.sources.tsunamigov import MAX_ENTRY_FAILURES, parse_time, permanent

log = logging.getLogger(__name__)

BASE = "https://www.jma.go.jp/bosai/tsunami/data/"
MAX_AGE = timedelta(hours=48)  # older reports are history, not alerts
SEEN_LIMIT = 200

# Category.Kind.Code values of a tsunami report, from the code table in JMA's XML dissemination format documentation
# ("Kind" of the tsunami forecast element). A code missing from this table contributes no level and no area.
KIND_LEVELS = {
    "00": levels.INFORMATION,  # no tsunami expected
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


COORDINATE = re.compile(r"(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$")


def parse_coordinate(text: str) -> tuple[float, float] | None:
    """JMA ISO 6709 style '+07.5-080.8/' (optionally with depth) to (lat, lon)."""
    match = COORDINATE.match(text.strip())
    return (float(match["lat"]), float(match["lon"])) if match else None


def fallback_alert(entry: dict) -> TsunamiAlert:
    """An alert from the list entry alone, for a report whose detail file can never be read: better a headline
    than a Warning nobody sees. The level is the entry's English title (INFORMATION when it names none)."""
    title = entry.get("en_ttl") or "Tsunami report"
    place, mag = entry.get("en_anm") or "", entry.get("mag")
    reported = parse_time(entry.get("rdt"))
    return TsunamiAlert(
        id=f"jma:{entry['json']}",
        dedupe_key=f"jma:{entry['json']}",
        source="jma",
        level=levels.level_from_text(title) or levels.INFORMATION,
        headline=f"JMA {title}" + (f": {place}" if place else "") + (f" M{mag}" if mag else ""),
        issued=reported,
        updated=reported,
        text="Details unavailable: the report could not be read.",
    )


def parse_detail(detail: dict, entry: dict) -> TsunamiAlert:
    """One detail file (with its list entry for the English title and file name) to a TsunamiAlert."""
    head, body = detail.get("Head", {}), detail.get("Body", {})
    items = (body.get("Tsunami") or {}).get("Forecast", {}).get("Item") or []
    if isinstance(items, dict):
        items = [items]
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
    # Only the areas under an actionable level: a lifted or forecast-only area is not covered by the warning. A
    # report with none (a lift, an all-forecast report) keeps every coded area.
    coded = [i for i in items if i["Category"]["Kind"]["Code"] in KIND_LEVELS]
    live = [i for i in coded if KIND_LEVELS[i["Category"]["Kind"]["Code"]] in levels.ACTIONABLE]
    areas = tuple(i["Area"].get("enName") or i["Area"].get("Name", "") for i in live or coded)
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
        self._failures: dict[str, int] = {}

    def interval(self, health: SourceHealth) -> float:
        return self._interval

    def _commit(self, names: list[str], events: dict[str, list[str]]) -> None:
        """Take over what a poll learned, newest SEEN_LIMIT of each. Nothing awaits in here, and nothing above calls
        it before the batch is complete, so a poll cancelled part-way never leaves this state ahead of the store."""
        for name in names:
            self._failures.pop(name, None)
            self._seen[name] = None
        while len(self._seen) > SEEN_LIMIT:
            del self._seen[next(iter(self._seen))]
        self._ids_by_event = events
        while len(self._ids_by_event) > SEEN_LIMIT:
            del self._ids_by_event[next(iter(self._ids_by_event))]

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        response = await client.get(f"{BASE}list.json")
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list):
            raise ValueError("jma: list.json is not a list")
        now = self._clock.now()
        alerts: list[TsunamiAlert] = []
        skipped = False  # a report that could not be fetched: this poll is not a full answer
        done: list[str] = []  # reports finished this poll
        events = dict(self._ids_by_event)  # event -> report ids; replaced, never edited, until the poll is whole
        # Oldest first, so a later report supersedes or cancels the earlier ones of the same event.
        try:
            async with asyncio.timeout(ENTRY_BUDGET):  # below poll.FETCH_DEADLINE: what was read is still returned
                for entry in sorted((e for e in entries if isinstance(e, dict)), key=lambda e: str(e.get("ctt", ""))):
                    if not await self._report(client, entry, now, events, alerts, done):
                        skipped = True
        except TimeoutError:
            log.warning("jma: out of time, returning %d reports read so far", len(alerts))
            skipped = True
        self._commit(done, events)
        log.debug("jma: %d new reports", len(alerts))
        return Batch(self.name, now, alerts=tuple(alerts), meta={"incomplete": skipped})

    async def _report(self, client, entry: dict, now, events: dict, alerts: list, done: list[str]) -> bool:
        """Fetch one list entry into `alerts`. False when the report could not be fetched and is left for next poll."""
        try:
            name = entry.get("json")
            reported = parse_time(entry.get("rdt"))
            if not name or name in self._seen or name in done or reported is None or now - reported > MAX_AGE:
                return True
        except (TypeError, AttributeError, ValueError, OverflowError):  # a malformed list entry: not this one
            return True
        try:
            detail = await client.get(f"{BASE}{name}")
            detail.raise_for_status()
            payload = detail.json()
            if not isinstance(payload, dict):
                raise ValueError("detail is not an object")
            alert = parse_detail(payload, entry)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            count = self._failures.get(name, 0)
            if permanent(exc):  # outages never count: a live report must not be dropped
                count = self._failures[name] = count + 1
            if not permanent(exc) or count < MAX_ENTRY_FAILURES:
                log.debug("jma: skipping %s: %r", name, exc)
                return False
            log.warning("jma: giving up on %s after %d attempts: %r", name, count, exc)
            alert = fallback_alert(entry)
        event = str(entry.get("eid") or name)
        earlier = events.pop(event, [])  # re-inserted below, so an active event stays newest
        events[event] = [*earlier, alert.id]
        alerts.append(replace(alert, references=tuple(earlier)))
        done.append(name)
        return True
