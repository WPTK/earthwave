"""tsunami.gov source: NTWC and PTWC Atom feeds, then each unseen entry's CAP product and plain-text bulletin."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, TsunamiAlert
from earthwave.net import retry_after_of
from earthwave.sources import levels
from earthwave.sources.base import ENTRY_BUDGET, ENTRY_LIMIT, SourceHealth

log = logging.getLogger(__name__)

FEEDS = ("https://www.tsunami.gov/events/xml/PAAQAtom.xml", "https://www.tsunami.gov/events/xml/PHEBAtom.xml")
ATOM = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
WMO_HEADER = re.compile(r"^\s*([A-Z]{4}\d{2} [A-Z]{4} \d{6})\b", re.MULTILINE)
PRODUCT_URL = re.compile(r"/events/(PAAQ|PHEB)/\d{4}/\d{2}/\d{2}/[^/]+/\d+/([A-Z0-9]+)/")
MAX_ENTRY_FAILURES = 5  # a CAP link answering 4xx or unparseable this many polls in a row is given up on
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


def category(entry: ET.Element) -> str:
    """The 'Category:' line of an entry's summary (Information, Watch, Advisory, Warning ...); titles are regions."""
    summary = entry.find(f"{ATOM}summary")
    match = re.search(r"Category:\s*([A-Za-z]+)", " ".join(summary.itertext())) if summary is not None else None
    return match[1] if match else ""


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
                "category": category(entry),
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


def parse_cap(xml: str, bulletin: str = "", cap_url: str = "") -> TsunamiAlert | None:
    """A CAP 1.2 product to a TsunamiAlert, or None for a Test, Exercise, System or Draft product (never shown as an
    alert, and it cannot close one). The dedupe key is the bulletin's WMO header (shared with the NWS API)."""
    root = ET.fromstring(xml)
    ident = _text(root.find(f"{CAP}identifier"))
    sent = parse_time(_text(root.find(f"{CAP}sent")))
    msg_type = _text(root.find(f"{CAP}msgType"))
    info = root.find(f"{CAP}info")
    if not ident or sent is None or info is None:
        raise ValueError("CAP product lacks identifier, sent or info")
    if (status := _text(root.find(f"{CAP}status")).lower()) and status != "actual":
        return None  # Test, Exercise, System or Draft; an unreadable status is shown, never dropped
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
        dedupe_key=dedupe_key(header, sent),
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


def dedupe_key(header: str, sent: datetime) -> str:
    """The WMO header plus the month the product was issued in (the header's DDHHMM repeats every month).

    The month is the one (last, this or next, relative to `sent`) in which the header's day and time land closest
    to `sent`: two feeds stamp the same product a few seconds apart, and a stamp just either side of any midnight
    must not give two keys.
    """
    match = re.search(r"(\d{2})(\d{2})(\d{2})$", header)
    this_month = sent.replace(day=1)
    if not match:
        return f"{header}|{this_month:%Y-%m}"
    day, hour, minute = (int(g) for g in match.groups())
    best: tuple[float, datetime] | None = None
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    next_month = (this_month + timedelta(days=31)).replace(day=1)
    for month in (last_month, this_month, next_month):
        try:
            issued = month.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:  # that month has no such day or time
            continue
        gap = abs((issued - sent).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, month)
    return f"{header}|{(best[1] if best else this_month):%Y-%m}"


def fallback_alert(entry: dict) -> TsunamiAlert:
    """An alert built from an Atom entry alone, for a product whose CAP can never be read: better a headline than
    a Warning nobody sees. The level is the summary's Category line (INFORMATION when it names none) and the time
    is the entry's `updated`. The key is the one a CAP-built alert without a bulletin gets, so the same product
    from another feed still merges. It carries no quake link: the entry has the epicentre but not the origin time."""
    when = entry["updated"]
    region = entry["title"] or "Tsunami product"
    kind = entry["category"]
    match = PRODUCT_URL.search(entry["cap_url"])
    center, product = (match.group(1), match.group(2)) if match else ("TSUN", "XXXX")
    return TsunamiAlert(
        id=entry["id"],
        dedupe_key=dedupe_key(f"{product} {center} {when:%d%H%M}", when) if when else f"{entry['id']}|atom",
        source="tsunamigov",
        level=levels.level_from_text(kind) or levels.INFORMATION,
        headline=f"Tsunami {kind.lower()} {region}" if kind else f"Tsunami product {region}",
        issued=when,
        updated=when,
        text=f"Details unavailable: the product could not be read. {entry['bulletin_url']}".strip(),
    )


def trusted_url(url: str) -> str | None:
    """Feed-supplied links are fetched with our User-Agent, so only tsunami.gov is followed, and only over https:
    an http link (the CAP products carry some) is upgraded. Anything else, or another port, gives None."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or port not in (None, 443):
        return None
    if host != "tsunami.gov" and not host.endswith(".tsunami.gov"):
        return None
    return parts._replace(scheme="https", netloc=host).geturl()


def permanent(exc: Exception) -> bool:
    """A failure that retrying is unlikely to fix: a 4xx answer or a CAP that does not parse. Not a rate limit."""
    if isinstance(exc, TimeoutError):  # our own per-entry limit: a stall is an outage
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429 or "Retry-After" in exc.response.headers:
            return False
        return 400 <= exc.response.status_code < 500
    return not isinstance(exc, httpx.HTTPError)


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
        if trusted_url(str(response.url)) is None:  # redirected off tsunami.gov: the answer is not ours to use
            raise ValueError(f"redirected to {response.url.host}")
        return response.text

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        alerts: list[TsunamiAlert] = []
        failures: list[Exception] = []
        done: list[str] = []  # entries finished this poll; committed to _seen only once the batch is returned
        skipped = False  # something the feeds list could not be fetched: this poll is not a full answer
        read_feeds = 0

        async def collect() -> None:
            nonlocal skipped, read_feeds
            for feed in FEEDS:
                try:
                    entries = parse_atom(await self._get_text(client, feed))
                except (httpx.HTTPError, ET.ParseError, ValueError) as exc:  # ValueError: redirected off-site
                    log.warning("tsunamigov: %s unavailable: %r", feed, exc)
                    failures.append(exc)
                    skipped = True
                    continue
                read_feeds += 1
                # Newest first, so a stalled old product cannot keep a new Warning out of the poll; the store copes
                # with a product arriving before the one it supersedes.
                for entry in entries:
                    if entry["id"] in self._seen or entry["id"] in done or not entry["cap_url"]:
                        continue
                    try:
                        async with asyncio.timeout(ENTRY_LIMIT):
                            finished = await self._entry(client, entry, alerts)
                    except TimeoutError:  # one product trickling: the next entries still get their turn
                        log.warning("tsunamigov: %s timed out", entry["cap_url"])
                        finished = False
                    if finished:
                        done.append(entry["id"])
                    else:
                        skipped = True

        try:
            async with asyncio.timeout(ENTRY_BUDGET):  # below poll.FETCH_DEADLINE: what was read is still returned
                await collect()
        except TimeoutError:
            log.warning("tsunamigov: out of time, returning %d products read so far", len(alerts))
            skipped = True
        if not read_feeds:  # nothing was learned: an outage, not an empty feed
            now = self._clock.now()
            raise (
                max(failures, key=lambda exc: retry_after_of(exc, now) or 0.0)
                if failures
                else TimeoutError("no tsunami.gov feed read in full")
            )
        for entry_id in done:  # nothing above awaits from here on, so a cancelled poll never leaves _seen ahead
            self._failures.pop(entry_id, None)
            self._seen[entry_id] = None
        while len(self._seen) > SEEN_LIMIT:
            del self._seen[next(iter(self._seen))]
        log.debug("tsunamigov: %d new products", len(alerts))
        alerts.sort(key=lambda a: a.issued or datetime.min.replace(tzinfo=UTC))
        return Batch(self.name, self._clock.now(), alerts=tuple(alerts), meta={"incomplete": skipped})

    async def _entry(self, client: httpx.AsyncClient, entry: dict, alerts: list[TsunamiAlert]) -> bool:
        """Fetch one entry's product into `alerts`. True when the entry is finished and need not be fetched again.

        An alert in hand is always appended: a bulletin outage shows the CAP text now and leaves the entry open, so
        a later poll adds the bulletin to the same alert (the CAP identifier is the store's first dedupe key).
        """
        try:
            cap_url = trusted_url(entry["cap_url"])
            if cap_url is None:
                log.warning("tsunamigov: not fetching %s (not tsunami.gov)", entry["cap_url"])
                alerts.append(fallback_alert(entry))
                return True
            bulletin_url = trusted_url(entry["bulletin_url"]) if entry["bulletin_url"] else None
            cap = await self._get_text(client, cap_url)
            # The alert in hand goes out before the bulletin is asked for: whatever happens to that request, the
            # CAP's alert is already in `alerts`, and the bulletin's version replaces it.
            alert = parse_cap(cap, "", cap_url)
            slot = None
            if alert is not None:
                alerts.append(alert)
                slot = len(alerts) - 1
            outage = False
            if bulletin_url:
                try:
                    bulletin = await self._get_text(client, bulletin_url)
                    if slot is not None:
                        alerts[slot] = parse_cap(cap, bulletin, cap_url)  # type: ignore[assignment]
                except (httpx.HTTPError, ValueError) as exc:
                    outage = not permanent(exc)  # an outage: the CAP's alert stands, the bulletin comes next poll
                    log.warning("tsunamigov: bulletin %s unavailable: %r", bulletin_url, exc)
            return not outage
        except Exception as exc:
            if not permanent(exc):
                # Outages (connect errors, timeouts, 5xx) never count: a live product must not be dropped.
                log.debug("tsunamigov: %s unavailable: %r", entry["cap_url"], exc)
                return False
            count = self._failures.get(entry["id"], 0) + 1
            self._failures[entry["id"]] = count
            if count < MAX_ENTRY_FAILURES:
                log.debug("tsunamigov: %s failed (%d): %r", entry["cap_url"], count, exc)
                return False
            log.warning("tsunamigov: giving up on %s after %d attempts: %r", entry["cap_url"], count, exc)
            try:
                alerts.append(fallback_alert(entry))
            except Exception:
                log.warning("tsunamigov: no alert can be built from %s", entry["id"], exc_info=True)
            return True
