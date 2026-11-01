"""HTTP client factory plus the record and replay transports."""

import base64
import json
import logging
import math
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from earthwave import __version__
from earthwave.clock import Clock, ReplayClock, SystemClock

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/WPTK/earthwave"
_DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}
_CALLER_VALIDATORS = {"if-none-match", "if-modified-since"}
RETRY_AFTER_DEFAULT = 60.0  # seconds a host is left alone after a 429 that names no wait
RETRY_AFTER_CAP = 300.0  # a few polls of an alert feed: a longer Retry-After is honoured only up to here
CACHE_ENTRIES = 64


def user_agent(contact: str = "") -> str:
    suffix = f"; {contact}" if contact else ""
    return f"earthwave/{__version__} (+{REPO_URL}{suffix})"


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


def retry_after_of(exc: BaseException, now: datetime) -> float | None:
    """The wait a 429 or 503 asked for (capped), or None when `exc` is not a rate-limit answer."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
        header = exc.response.headers.get("Retry-After")
        if header or exc.response.status_code == 429:
            return min(retry_after(header, now, RETRY_AFTER_DEFAULT), RETRY_AFTER_CAP)
    return None


class _Entry:
    __slots__ = ("body", "etag", "headers", "last_modified", "status")

    def __init__(self, response: httpx.Response, body: bytes):
        self.etag = response.headers.get("ETag")
        self.last_modified = response.headers.get("Last-Modified")
        self.status = response.status_code
        self.headers = {k: v for k, v in response.headers.items() if k.lower() not in _DROPPED_HEADERS}
        self.body = body

    def response(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, headers=self.headers, content=self.body, request=request)


class PoliteTransport(httpx.AsyncBaseTransport):
    """The one place that makes every source a considerate client.

    - A GET without a query string whose response carries an ETag or Last-Modified is kept; the next request for the URL
      is conditional, and a 304 from the origin is answered to the caller as the kept 200. Nothing is ever answered
      without asking the origin, so a cached copy can never pass for a fresh poll.
    - After a 429 (or a 503 with Retry-After) that URL is left alone for the time it named, up to RETRY_AFTER_CAP:
      requests for it meanwhile get a synthetic 429 carrying the wait that remains, which the scheduler turns into the
      next poll time. Other URLs, even on the same host, are unaffected.

    A request that already carries If-None-Match or If-Modified-Since (the USGS cursor) passes through untouched.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, clock: Clock):
        self._inner = inner
        self._clock = clock
        self._cache: dict[str, _Entry] = {}  # least recently used first
        self._blocked: dict[str, float] = {}  # url -> monotonic time it may be asked again

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key, now = str(request.url), self._clock.monotonic()
        remaining = self._blocked.get(key, 0.0) - now
        if remaining > 0:
            log.debug("polite: %s asked us to wait, %.0fs left", key, remaining)
            return httpx.Response(429, headers={"Retry-After": str(math.ceil(remaining))}, request=request)
        self._blocked.pop(key, None)
        if request.method != "GET" or _CALLER_VALIDATORS & {k.lower() for k in request.headers}:
            return await self._inner.handle_async_request(request)
        entry = self._cache.pop(key, None) if not request.url.query else None
        if entry:
            self._cache[key] = entry
            if entry.etag:
                request.headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                request.headers["If-Modified-Since"] = entry.last_modified
        response = await self._inner.handle_async_request(request)
        if response.status_code in (429, 503):
            self._note_rate_limit(key, response, now)
        elif response.status_code == 304 and entry:
            await response.aclose()
            return entry.response(request)
        elif response.status_code == 200 and not request.url.query:
            return await self._remember(key, request, response)
        return response

    def _note_rate_limit(self, key: str, response: httpx.Response, now: float) -> None:
        header = response.headers.get("Retry-After")
        if not header and response.status_code != 429:
            return
        wait = min(retry_after(header, self._clock.now(), RETRY_AFTER_DEFAULT), RETRY_AFTER_CAP)
        self._blocked[key] = now + wait
        log.warning("%s answered %d; leaving it alone for %.0fs", key, response.status_code, wait)

    async def _remember(self, key: str, request: httpx.Request, response: httpx.Response) -> httpx.Response:
        self._cache.pop(key, None)
        if "no-store" in response.headers.get("Cache-Control", "").lower() or not (
            response.headers.get("ETag") or response.headers.get("Last-Modified")
        ):
            return response
        body = await response.aread()
        await response.aclose()
        entry = self._cache[key] = _Entry(response, body)
        while len(self._cache) > CACHE_ENTRIES:
            del self._cache[next(iter(self._cache))]
        return entry.response(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_client(
    contact: str = "", transport: httpx.AsyncBaseTransport | None = None, clock: Clock | None = None
) -> httpx.AsyncClient:
    """The one shared client: identifying User-Agent, gzip, 10 s connect and 20 s read timeouts, polite transport."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact), "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        transport=PoliteTransport(transport or httpx.AsyncHTTPTransport(), clock or SystemClock()),
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport and appends every exchange to a JSONL file as {ts, method, url, status, headers, body}."""

    def __init__(self, inner: httpx.AsyncBaseTransport, path: Path, clock: Clock):
        self._inner = inner
        self._path = Path(path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        headers = {k: v for k, v in response.headers.items() if k.lower() not in _DROPPED_HEADERS}
        record: dict = {
            "ts": self._clock.now().isoformat(),
            "method": request.method,
            "url": str(request.url),
            "status": response.status_code,
            "headers": headers,
        }
        try:
            record["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            record["body_b64"] = base64.b64encode(body).decode("ascii")
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if self._path.exists() and self._path.stat().st_size:
            with self._path.open("rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":  # an earlier run was cut off mid-line: end it so this record stands alone
                    line = "\n" + line
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        await response.aclose()
        return httpx.Response(response.status_code, headers=headers, content=body, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def load_recording(path: Path) -> list[dict]:
    """The recorded exchanges; a line that is not valid JSON (a run cut off mid-write) is skipped with a warning."""
    records = []
    with Path(path).open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("%s: line %d is not valid JSON, skipped", path, number)
    return records


def recording_start(path: Path) -> datetime:
    """Timestamp of the first recorded exchange (the replay clock starts here)."""
    records = load_recording(path)
    if not records:
        raise ValueError(f"{path}: empty recording")
    return datetime.fromisoformat(records[0]["ts"])


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded responses by URL, n-th request for n-th record, and moves the clock to each record's time.

    Once a URL's records run out the last one repeats; an unknown URL answers 404.
    """

    def __init__(self, path: Path, clock: ReplayClock):
        self._clock = clock
        self._by_url: dict[str, list[dict]] = {}
        for record in load_recording(path):
            self._by_url.setdefault(record["url"], []).append(record)
        self._seen: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        records = self._by_url.get(url)
        if not records:
            log.warning("replay: no recording for %s", url)
            return httpx.Response(404, content=b"no recording", request=request)
        n = self._seen.get(url, 0)
        self._seen[url] = n + 1
        record = records[min(n, len(records) - 1)]
        self._clock.advance_to(datetime.fromisoformat(record["ts"]))
        if "body_b64" in record:
            content = base64.b64decode(record["body_b64"])
        else:
            content = record.get("body", "").encode("utf-8")
        return httpx.Response(record["status"], headers=record.get("headers", {}), content=content, request=request)
