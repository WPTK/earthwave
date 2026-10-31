"""tsunami.gov source: NTWC and PTWC Atom feeds, then each unseen entry's CAP product and plain-text bulletin."""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import levels
from earthwave.sources.base import SourceHealth

log = logging.getLogger(__name__)

FEEDS = ("https://www.tsunami.gov/events/xml/PAAQAtom.xml", "https://www.tsunami.gov/events/xml/PHEBAtom.xml")
ATOM = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
WMO_HEADER = re.compile(r"^\s*([A-Z]{4}\d{2} [A-Z]{4} \d{6})\b", re.MULTILINE)
PRODUCT_URL = re.compile(r"/events/(PAAQ|PHEB)/\d{4}/\d{2}/\d{2}/[^/]+/\d+/([A-Z0-9]+)/")
MAX_ENTRY_FAILURES = 5  # a CAP link that fails this many polls in a row is given up on
SEEN_LIMIT = 500


def parse_time(text: str | None) -> datetime | None:
    if not text or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(node: ET.Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def parse_atom(xml: str) -> list[dict]:
    """Entries of an Atom feed as dicts: id, title, updated, cap_url, bulletin_url."""
    root = ET.fromstring(xml)
    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        cap_url = bulletin_url = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("type") == "application/cap+xml":
                cap_url = link.get("href", "").strip()
            elif link.get("rel") == "alternate":
                bulletin_url = link.get("href", "").strip()
        entries.append(
            {
                "id": _text(entry.find(f"{ATOM}id")),
                "title": _text(entry.find(f"{ATOM}title")),
                "updated": parse_time(_text(entry.find(f"{ATOM}updated"))),
                "cap_url": cap_url,
                "bulletin_url": bulletin_url,
            }
        )
    return entries


def parse_circle(text: str) -> tuple | None:
    """CAP circle 'lat,lon radius_km' to ("circle", lat, lon, radius_km)."""
    try:
        point, _, radius = text.strip().partition(" ")
        lat, lon = (float(v) for v in point.split(","))
        return ("circle", lat, lon, float(radius or 0))
    except ValueError:
        return None


def parse_polygon(text: str) -> tuple | None:
    """CAP polygon 'lat,lon lat,lon ...' to ("polygon", ((lon, lat), ...))."""
    try:
        points = tuple((float(b), float(a)) for a, b in (pair.split(",") for pair in text.split()))
    except ValueError:
        return None
    return ("polygon", points) if len(points) >= 3 else None


def bulletin_header(text: str) -> str | None:
    match = WMO_HEADER.search(text)
    return match.group(1) if match else None


def parse_cap(xml: str, bulletin: str = "", cap_url: str = "") -> TsunamiAlert:
    """A CAP 1.2 product to a TsunamiAlert. The dedupe key is the bulletin's WMO header (shared with the NWS API)."""
    root = ET.fromstring(xml)
    ident = _text(root.find(f"{CAP}identifier"))
    sent = parse_time(_text(root.find(f"{CAP}sent")))
    msg_type = _text(root.find(f"{CAP}msgType"))
    info = root.find(f"{CAP}info")
    if not ident or sent is None or info is None:
        raise ValueError("CAP product lacks identifier, sent or info")
    params = {_text(p.find(f"{CAP}valueName")): _text(p.find(f"{CAP}value")) for p in info.findall(f"{CAP}parameter")}
    event = _text(info.find(f"{CAP}event"))
    level = levels.CANCELLATION if msg_type.lower() == "cancel" else levels.level_from_text(event)
    level = level or levels.INFORMATION
    areas, geometry = [], None
    for area in info.findall(f"{CAP}area"):
        if desc := _text(area.find(f"{CAP}areaDesc")):
            areas.append(desc)
        if geometry is None:
            polygon = area.find(f"{CAP}polygon")
            if polygon is not None:
                geometry = parse_polygon(polygon.text or "")
            circle = area.find(f"{CAP}circle")
            if geometry is None and circle is not None:
                geometry = parse_circle(circle.text or "")
    origin = None
    origin_time = parse_time(params.get("EventOriginTime"))
    try:
        lat, lon = (float(v) for v in params.get("EventLatLon", "").split()[0].split(","))
        if origin_time is not None:
            origin = (origin_time, lat, lon)
    except (ValueError, IndexError):
        pass
    place = params.get("EventLocationName", "").title() or (areas[0].title() if areas else "")
    mag = params.get("EventPreliminaryMagnitude")
    headline = event or "Tsunami product"
    if place:
        headline += f": {place}"
    if mag:
        headline += f" M{mag}"
    description = " ".join(filter(None, (_text(info.find(f"{CAP}description")), _text(info.find(f"{CAP}instruction")))))
    description = description.removesuffix("N/A").strip()  # an instruction of "N/A" is filler
    header = bulletin_header(bulletin)
    if header is None:
        match = PRODUCT_URL.search(cap_url or _text(info.find(f"{CAP}web")))
        center, product = (match.group(1), match.group(2)) if match else ("TSUN", "XXXX")
        header = f"{product} {center} {sent:%d%H%M}"
    references = tuple(
        part.split(",")[1] for part in _text(root.find(f"{CAP}references")).split() if part.count(",") == 2
    )
    return TsunamiAlert(
        id=ident,
        dedupe_key=f"{header}|{sent:%Y-%m}",
        source="tsunamigov",
        level=level,
        headline=headline,
        areas=tuple(areas),
        issued=sent,
        expires=parse_time(_text(info.find(f"{CAP}expires"))),
        updated=sent,
        geometry=geometry,
        text=bulletin.strip() or description,
        references=references,
        origin=origin,
    )


class TsunamiGovSource:
    name = "tsunamigov"
    min_interval = 60.0

    def __init__(self, clock: Clock, interval_seconds: float = 60):
        self._clock = clock
        self._interval = max(self.min_interval, float(interval_seconds))
        self._seen: dict[str, None] = {}
        self._failures: dict[str, int] = {}

    def interval(self, health: SourceHealth) -> float:
        return self._interval

    async def _get_text(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        alerts: list[TsunamiAlert] = []
        failures: list[Exception] = []
        for feed in FEEDS:
            try:
                entries = parse_atom(await self._get_text(client, feed))
            except (httpx.HTTPError, ET.ParseError) as exc:
                log.warning("tsunamigov: %s unavailable: %r", feed, exc)
                failures.append(exc)
                continue
            # Oldest first, so an update is merged after the product it supersedes.
            for entry in reversed(entries):
                if entry["id"] in self._seen or not entry["cap_url"]:
                    continue
                try:
                    cap = await self._get_text(client, entry["cap_url"])
                    bulletin = ""
                    if entry["bulletin_url"]:
                        try:
                            bulletin = await self._get_text(client, entry["bulletin_url"])
                        except httpx.HTTPError as exc:
                            log.warning("tsunamigov: bulletin %s unavailable: %r", entry["bulletin_url"], exc)
                    alerts.append(parse_cap(cap, bulletin, entry["cap_url"]))
                except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                    count = self._failures.get(entry["id"], 0) + 1
                    self._failures[entry["id"]] = count
                    if count < MAX_ENTRY_FAILURES:
                        log.debug("tsunamigov: %s failed (%d): %r", entry["cap_url"], count, exc)
                        continue
                    log.warning("tsunamigov: giving up on %s after %d attempts: %r", entry["cap_url"], count, exc)
                self._failures.pop(entry["id"], None)
                self._seen[entry["id"]] = None
                while len(self._seen) > SEEN_LIMIT:
                    del self._seen[next(iter(self._seen))]
        if len(failures) == len(FEEDS):
            raise failures[0]
        log.debug("tsunamigov: %d new products", len(alerts))
        alerts.sort(key=lambda a: a.issued or datetime.min.replace(tzinfo=UTC))
        return Batch(self.name, self._clock.now(), alerts=tuple(alerts))
