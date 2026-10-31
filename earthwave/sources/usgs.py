"""USGS earthquake source: summary feeds (steady polling), fdsnws queries (history) and the lazy detail product."""

import logging
import math
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from earthwave.clock import Clock
from earthwave.models import Batch, Earthquake, QuakeDetail
from earthwave.sources.base import SourceHealth

log = logging.getLogger(__name__)

FEED_BASE = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
# Startup backfill, one feed per fetch: 4.5+ for a week, everything for a day, significant for a month.
STARTUP_FEEDS = ("4.5_week", "all_day", "significant_month")
# (feed prefix, lowest magnitude it contains)
TIERS = (("all", -10.0), ("1.0", 1.0), ("2.5", 2.5), ("4.5", 4.5))
PERIOD_HOURS = {"hour": 1, "day": 24, "week": 168, "month": 720}
STARTUP_ATTEMPTS = 2  # tries per optional startup feed before it is skipped so the steady feed can start
DETAIL_CACHE_SIZE = 64
FDSN_LIMIT = 5000


def steady_feed(min_magnitude: float) -> str:
    """The smallest day feed that still contains everything at or above `min_magnitude`."""
    prefix = TIERS[0][0]
    for name, low in TIERS:
        if low <= min_magnitude:
            prefix = name
    return f"{prefix}_day"


def feed_coverage(feed: str) -> tuple[float, float] | None:
    """(hours, min_magnitude) a summary feed is complete for; None for the significant feed."""
    prefix, _, period = feed.rpartition("_")
    for name, low in TIERS:
        if name == prefix:
            return float(PERIOD_HOURS[period]), low
    return None


def _ms(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def _ids(value: str | None) -> tuple[str, ...]:
    return tuple(part for part in (value or "").split(",") if part)


def parse_feature(feature: dict) -> Earthquake | None:
    """One GeoJSON feature to an Earthquake; None for non-earthquakes (blasts, ice quakes) and unmagnituded events."""
    props = feature["properties"]
    if props.get("type", "earthquake") != "earthquake" or props.get("mag") is None:
        return None
    lon, lat, *rest = feature["geometry"]["coordinates"]
    quake_id = feature["id"]
    time = _ms(props["time"])
    assert time is not None
    return Earthquake(
        id=quake_id,
        time=time,
        lat=float(lat),
        lon=float(lon),
        mag=float(props["mag"]),
        depth_km=float(rest[0]) if rest and rest[0] is not None else 0.0,
        source="usgs",
        mag_type=props.get("magType") or "",
        place=props.get("place") or "",
        status=props.get("status") or "automatic",
        tsunami_flag=bool(props.get("tsunami")),
        pager_alert=props.get("alert"),
        sig=int(props.get("sig") or 0),
        felt=props.get("felt"),
        cdi=props.get("cdi"),
        mmi=props.get("mmi"),
        updated=_ms(props.get("updated")) or time,
        url=props.get("url") or "",
        detail_url=props.get("detail") or "",
        also_ids=tuple(i for i in _ids(props.get("ids")) if i != quake_id),
    )


def parse_collection(data: dict) -> tuple[Earthquake, ...]:
    """Parse a feed or fdsnws FeatureCollection, skipping (and logging) malformed features."""
    quakes = []
    for feature in data.get("features", []):
        try:
            quake = parse_feature(feature)
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            log.warning(
                "usgs: skipping malformed feature %r: %r", feature.get("id") if isinstance(feature, dict) else "?", exc
            )
            continue
        if quake is not None:
            quakes.append(quake)
    return tuple(quakes)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _prop(products: dict, name: str, *keys: str) -> float | None:
    """First finite numeric property among `keys` of the newest product `name` (all USGS values are strings)."""
    items = products.get(name) or []
    if not items:
        return None
    props = items[0].get("properties", {})
    for key in keys:
        number = _num(props.get(key))
        if number is not None:
            return number
    return None


def parse_detail(data: dict) -> QuakeDetail:
    products = data.get("properties", {}).get("products", {})
    pager = products.get("losspager") or []
    pager_alert = pager[0].get("properties", {}).get("alertlevel") if pager else None
    responses = _prop(products, "dyfi", "num-responses")
    return QuakeDetail(
        id=data.get("id", ""),
        pager_alert=pager_alert,
        pager_max_mmi=_prop(products, "losspager", "maxmmi"),
        shakemap_max_mmi=_prop(products, "shakemap", "maxmmi", "maxmmi-grid"),
        shakemap_max_pga=_prop(products, "shakemap", "maxpga", "maxpga-grid"),
        shakemap_max_pgv=_prop(products, "shakemap", "maxpgv", "maxpgv-grid"),
        dyfi_max_mmi=_prop(products, "dyfi", "maxmmi"),
        dyfi_responses=int(responses) if responses is not None else None,
        products=tuple(sorted(products)),
    )


def nearby_cities_url(data: dict) -> str | None:
    items = data.get("properties", {}).get("products", {}).get("nearby-cities") or []
    if not items:
        return None
    return ((items[0].get("contents") or {}).get("nearby-cities.json") or {}).get("url")


def parse_nearby_cities(rows: list[dict]) -> tuple[str, ...]:
    return tuple(
        f"{row['distance']} km {row['direction']} of {row['name']}"
        for row in rows
        if {"distance", "direction", "name"} <= row.keys()
    )


class UsgsSource:
    name = "usgs"
    min_interval = 60.0

    def __init__(
        self, clock: Clock, interval_seconds: float = 60, min_magnitude: float = 2.5, alert_floor: float = math.inf
    ):
        self._clock = clock
        self._alert_floor = alert_floor
        self._startup_failures = 0
        self._interval = max(self.min_interval, float(interval_seconds))
        self._startup = list(STARTUP_FEEDS)
        self.steady = steady_feed(min(min_magnitude, alert_floor))
        self._details: OrderedDict[str, tuple[datetime | None, QuakeDetail]] = OrderedDict()

    @property
    def starting(self) -> bool:
        return bool(self._startup)

    def set_min_magnitude(self, min_magnitude: float) -> None:
        """Pick the steady feed for a new view filter (the next fetch is unconditional if the feed changed).

        The feed also has to hold every quake that can raise an alert, whatever the display filter hides."""
        self.steady = steady_feed(min(min_magnitude, self._alert_floor))

    def interval(self, health: SourceHealth) -> float:
        return 0.0 if self._startup else self._interval

    async def fetch(self, client: httpx.AsyncClient, since: str | None) -> Batch:
        backfill = bool(self._startup)
        feed = self._startup[0] if backfill else self.steady
        headers = {}
        same_feed = bool(since) and since.partition("|")[0] == feed
        if same_feed and since.partition("|")[2]:
            headers["If-Modified-Since"] = since.partition("|")[2]
        # The first batch from a feed we have no cursor for may hold many quakes the store lacks (feed switch, or a
        # failed backfill), so it merges silently instead of flashing them all as new.
        silent = not backfill and not same_feed
        started = self._clock.monotonic()
        try:
            response = await client.get(f"{FEED_BASE}{feed}.geojson", headers=headers)
            elapsed = self._clock.monotonic() - started
            if response.status_code == 304:
                self._done(feed, backfill)
                log.debug("usgs %s: 304 not modified (%.2fs)", feed, elapsed)
                return Batch(self.name, self._clock.now(), not_modified=True, backfill=backfill, cursor=since)
            response.raise_for_status()
            quakes = parse_collection(response.json())
        except Exception:
            if backfill:
                self._startup_failures += 1
                if self._startup_failures >= STARTUP_ATTEMPTS:
                    log.warning("usgs: skipping startup feed %s after %d failures", feed, self._startup_failures)
                    self._done(feed, backfill)
            raise
        self._done(feed, backfill)
        log.debug(
            "usgs %s: %s, %d bytes, %d quakes (%.2fs)",
            feed,
            response.status_code,
            len(response.content),
            len(quakes),
            elapsed,
        )
        modified = response.headers.get("Last-Modified")
        return Batch(
            self.name,
            self._clock.now(),
            quakes=quakes,
            backfill=backfill,
            cursor=f"{feed}|{modified or ''}",
            coverage=feed_coverage(feed),
            meta={"feed": feed, "silent": silent},
        )

    def _done(self, feed: str, backfill: bool) -> None:
        """A startup feed is finished once it fetched, or once it has failed STARTUP_ATTEMPTS times."""
        if backfill and self._startup and self._startup[0] == feed:
            self._startup.pop(0)
            self._startup_failures = 0

    async def fetch_range(
        self, client: httpx.AsyncClient, start: datetime, end: datetime, min_magnitude: float
    ) -> Batch:
        """fdsnws query for history beyond what the feeds cover; the batch is a silent backfill."""
        params = {
            "format": "geojson",
            "starttime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "endtime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": f"{min_magnitude:g}",
            "orderby": "time",
            "limit": str(FDSN_LIMIT),
        }
        response = await client.get(FDSN_URL, params=params)
        response.raise_for_status()
        quakes = parse_collection(response.json())
        log.debug("usgs fdsnws: %d quakes for %s", len(quakes), params)
        hours = math.ceil((end - start) / timedelta(hours=1))
        # A result that hit the limit is only partial, so claim no coverage for it.
        coverage = None if len(quakes) >= FDSN_LIMIT else (float(hours), min_magnitude)
        return Batch(
            self.name, self._clock.now(), quakes=quakes, backfill=True, coverage=coverage, meta={"feed": "fdsnws"}
        )

    async def fetch_detail(self, client: httpx.AsyncClient, quake: Earthquake) -> QuakeDetail:
        """Detail product for one quake, LRU-cached by id and refreshed when the quake's `updated` moves."""
        cached = self._details.get(quake.id)
        if cached and cached[0] == quake.updated:
            self._details.move_to_end(quake.id)
            return cached[1]
        url = quake.detail_url or f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{quake.id}.geojson"
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
        detail = parse_detail(data)
        cities_url = nearby_cities_url(data)
        if cities_url:
            try:
                cities = await client.get(cities_url)
                cities.raise_for_status()
                detail = replace(detail, nearby_cities=parse_nearby_cities(cities.json()))
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("usgs: nearby cities for %s unavailable: %r", quake.id, exc)
        self._details[quake.id] = (quake.updated, detail)
        while len(self._details) > DETAIL_CACHE_SIZE:
            self._details.popitem(last=False)
        return detail
