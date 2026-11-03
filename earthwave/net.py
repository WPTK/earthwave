"""HTTP client factory plus the record and replay transports."""

import asyncio
import base64
import json
import logging
import math
import sqlite3
import time
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import anysqlite
import hishel
import hishel.httpx as hishel_httpx
import httpx
import stamina

from earthwave import __version__
from earthwave.clock import Clock, ReplayClock, SystemClock
from earthwave.paths import data_dir

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/WPTK/earthwave"
_DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}
_CALLER_VALIDATORS = {"if-none-match", "if-modified-since"}
RETRY_AFTER_DEFAULT = 60.0  # seconds a host is left alone after a 429 that names no wait
RETRY_AFTER_CAP = 300.0  # a few polls of an alert feed: a longer Retry-After is honoured only up to here
CACHE_MAX_BYTES = (
    20 * 1024 * 1024
)  # a larger cache file is deleted at start-up; one that grows past it while running is emptied
CACHE_UNVALIDATED_URLS = 512  # URLs remembered as having no validator
CACHE_TRIM_EVERY = 50  # cached requests between checks that the live size is still under the limit
CACHE_TRIM_MIN_AGE = 60.0  # seconds; younger entries are left alone when the cache is emptied
CACHE_TTL = 7 * 24 * 3600.0  # seconds an entry is kept
RETRY_ATTEMPTS = 3  # tries per request, the first included
RETRY_BUDGET = (
    22.0  # seconds from a request's start by which every retry must be over: ENTRY_LIMIT (25) less ENTRY_MARGIN
)
ENTRY_MARGIN = 3.0  # seconds kept free under an entry's limit, so the entry ends on an httpx error, not the builtin one
RETRY_MIN_TRY = 5.0  # a retry is only started when at least this long is left for it after its pause
RETRY_WAIT_INITIAL = 0.5  # seconds before the second try; stamina doubles it with jitter
RETRY_WAIT_MAX = 4.0
RETRY_WAIT_JITTER = 0.5
RETRY_AFTER_INLINE_DEFAULT = 5.0  # a 429 that names no wait is retried after this, as NwsSource did before
RETRY_INLINE_MAX = 10.0  # a Retry-After up to here is waited out in place; a longer one is left to the scheduler
_RETRY_STATUSES = {500, 502, 503, 504}
_RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)
# When the request being served started, and when the entry it belongs to must be over, both on the client's clock.
# Context variables, because hishel sits between the two transports and rebuilds the request.
_started: ContextVar[float | None] = ContextVar("earthwave_request_started", default=None)
_entry_deadline: ContextVar[float | None] = ContextVar("earthwave_entry_deadline", default=None)


@asynccontextmanager
async def entry_deadline(clock: Clock, seconds: float) -> AsyncIterator[None]:
    """Declare that what is fetched inside this block (one entry of a multi-request source) must be over `seconds`
    from now. Every request in it shares that one deadline: the retry layer starts no retry that would not fit,
    keeping ENTRY_MARGIN free, and cuts a try that is still running just short of it with an httpx timeout (an
    outage, which never counts) rather than leaving the source's own `asyncio.timeout(seconds)` to fire (a stall,
    which does). Does not itself time anything out."""
    mine = clock.monotonic() + seconds
    outer = _entry_deadline.get()
    token = _entry_deadline.set(mine if outer is None else min(outer, mine))
    try:
        yield
    finally:
        _entry_deadline.reset(token)


_TIMEOUT_HEADER = "X-Earthwave-Timeout"  # carries httpx's timeout across hishel, which rebuilds the request without it
_NO_CACHE_MARK = "no-cache"  # sent to hishel to force revalidation; RetryTransport removes it before the origin sees it


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


def _endpoint(url: httpx.URL) -> str:
    return f"{url.scheme}://{url.netloc.decode('ascii')}{url.path}"


class _Transient(Exception):
    """A response worth asking for again; carries it, so the last one can still be returned when the tries run out."""

    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _Final(Exception):
    """Wraps the error of a try after which no retry fits, so stamina does not schedule one."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


_TIMEOUT_KEYS = ("connect", "read", "write", "pool")


def _pack_timeout(timeout: object) -> str | None:
    if not isinstance(timeout, dict):
        return None
    return ",".join("-" if timeout.get(k) is None else repr(float(timeout[k])) for k in _TIMEOUT_KEYS)


def _unpack_timeout(value: str) -> dict[str, float | None]:
    return {k: None if v == "-" else float(v) for k, v in zip(_TIMEOUT_KEYS, value.split(","), strict=True)}


class RetryTransport(httpx.AsyncBaseTransport):
    """Asks again after a failure that is likely to pass, a few times and for a short while, then lets it stand.

    Retried: connect errors, timeouts and dropped connections; 500, 502, 503 and 504; 429 and 503 when the Retry-After
    they carry (5 s when a 429 names none) is at most RETRY_INLINE_MAX seconds; a longer wait is the scheduler's to
    honour, via PoliteTransport. Only GET and HEAD. When the tries run out the last exception is raised or the last
    response returned, so a source that is really down still fails exactly as it did without retries, with an httpx
    error. A 304 to a request that carried no validator is an error (the cache above would otherwise take it as
    confirming whatever it holds).

    Time: counted from when the request reached PoliteTransport (the cache lookup and its waits included), every
    retry ends within RETRY_BUDGET seconds, which is ENTRY_MARGIN below ENTRY_LIMIT, the time one tsunami or JMA entry
    may take. Inside an `entry_deadline` block the entry's remaining time is the budget when that is shorter, so the
    requests of one entry share it. A retry is started only when its pause, a worst-case backoff and RETRY_MIN_TRY
    fit; a try, its body included, is cut with an httpx timeout just short of the limit; nothing is slept after the
    last try. Without an `entry_deadline` the first try is bounded only by httpx's timeouts. The pauses are
    stamina's backoff; a rate-limit hint is slept on the injected clock.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, clock: Clock):
        self._inner = inner
        self._clock = clock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Cache-Control") == _NO_CACHE_MARK:  # ours, for the cache above; not for the origin
            del request.headers["Cache-Control"]
        packed = request.headers.pop(_TIMEOUT_HEADER, None)
        if packed and "timeout" not in request.extensions:  # hishel rebuilt the request without the client's timeout
            request.extensions["timeout"] = _unpack_timeout(packed)
        if request.method not in ("GET", "HEAD"):
            return await self._inner.handle_async_request(request)
        started = _started.get()
        deadline = (self._clock.monotonic() if started is None else started) + RETRY_BUDGET
        cut = deadline + ENTRY_MARGIN - 1.0  # a try still running here is ended, just inside the limit
        if (entry := _entry_deadline.get()) is not None:
            deadline, cut = min(deadline, entry - ENTRY_MARGIN), min(cut, entry - 1.0)
        try:
            async for attempt in stamina.retry_context(
                on=(_Transient, *_RETRY_EXCEPTIONS),
                attempts=RETRY_ATTEMPTS,
                wait_initial=RETRY_WAIT_INITIAL,
                wait_max=RETRY_WAIT_MAX,
                wait_jitter=RETRY_WAIT_JITTER,
            ):
                with attempt:
                    try:
                        response = await self._send(request, cut if attempt.num > 1 or entry is not None else None)
                    except _RETRY_EXCEPTIONS as exc:
                        if not self._room_for_retry(attempt.num, deadline, 0.0):
                            raise _Final(exc) from exc
                        raise
                    if response.status_code == 304 and not _CALLER_VALIDATORS & {k.lower() for k in request.headers}:
                        await response.aclose()
                        raise httpx.ProtocolError(f"{request.url}: 304 Not Modified to a request with no validator")
                    wait = self._retry_wait(response, attempt.num)
                    if wait is None or not self._room_for_retry(attempt.num, deadline, wait):
                        return response
                    await response.aread()
                    await response.aclose()
                    if wait:
                        await self._clock.sleep(wait)
                    raise _Transient(response)
        except _Final as final:
            raise final.error from None
        except _Transient as last:
            log.debug("retry: %s still answers %d, giving up", request.url, last.response.status_code)
            return last.response
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send(self, request: httpx.Request, cut: float | None) -> httpx.Response:
        """One try. With a cut the whole answer, body included, must arrive before it: the body is read here and handed
        up as bytes, so a slow one ends as an httpx timeout (an outage) and not as the caller's builtin stall."""
        if cut is None:
            return await self._inner.handle_async_request(request)
        left = cut - self._clock.monotonic()
        if left <= 0:
            raise httpx.ReadTimeout("no time left", request=request)
        try:
            async with asyncio.timeout(left):
                response = await self._inner.handle_async_request(request)
                body = await response.aread()
                await response.aclose()
        except TimeoutError:  # the retry budget, not the builtin kind that the entry limits use to mean a stall
            raise httpx.ReadTimeout("retry budget spent", request=request) from None
        headers = httpx.Headers(response.headers)
        for name in ("content-encoding", "content-length", "transfer-encoding"):  # the body is decoded and whole now
            headers.pop(name, None)
        return httpx.Response(
            response.status_code, headers=headers, content=body, extensions=response.extensions, request=request
        )

    def _room_for_retry(self, attempt: int, deadline: float, pause: float) -> bool:
        left = deadline - self._clock.monotonic() - pause - RETRY_WAIT_MAX - RETRY_WAIT_JITTER
        return attempt < RETRY_ATTEMPTS and left >= RETRY_MIN_TRY

    def _retry_wait(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to sleep before asking again (0 for the backoff alone), or None when the answer stands."""
        status = response.status_code
        header = response.headers.get("Retry-After")
        if status == 429 or (status == 503 and header):
            if status == 429 and not header and attempt > 1:  # a bare 429 is asked about once more, as NWS always was
                return None
            wait = retry_after(header, self._clock.now(), RETRY_AFTER_INLINE_DEFAULT)
            return wait if wait <= RETRY_INLINE_MAX else None
        return 0.0 if status in _RETRY_STATUSES else None

    async def aclose(self) -> None:
        await self._inner.aclose()


class PoliteTransport(httpx.AsyncBaseTransport):
    """The one place that makes every source a considerate client.

    - Requests are retried as RetryTransport says.
    - A GET without a query string and without validators of its own goes through an HTTP cache (hishel) in the user's
      data directory, size-bounded and expiring. It is asked to revalidate every time, so nothing is answered without
      asking the origin: the next request for the URL is conditional (ETag, Last-Modified), and a 304 from the origin
      is answered to the caller as the kept 200. A cache that cannot be read or written is dropped for the session
      with one warning; the request goes on without it.
    - After a 429 (or a 503 with Retry-After) that endpoint (scheme, host and path; the query is ignored, since the
      dated queries of fdsnws and CO-OPS change on every request) is left alone for the time it named, up to
      RETRY_AFTER_CAP: requests for it meanwhile get a synthetic 429 carrying the wait that remains, which the
      scheduler turns into the next poll time. Other paths, even on the same host, are unaffected.

    A request that already carries If-None-Match or If-Modified-Since (the USGS cursor) bypasses the cache, so a 304
    reaches its caller as a 304.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport, clock: Clock, cache_path: Path | None = None, cache: bool = True
    ):
        self._retrying = RetryTransport(inner, clock)
        self._clock = clock
        self._blocked: dict[str, float] = {}  # scheme://host/path -> monotonic time it may be asked again
        self._cached: httpx.AsyncBaseTransport | None = None
        self._cached_requests = 0
        self._unvalidated: set[str] = set()  # URLs whose last 200 had no validator: nothing to store, so skip the cache
        self._cache_path = cache_path
        self._storage: hishel.AsyncSqliteStorage | None = None
        self._connect_lock = asyncio.Lock()  # the first parallel requests must share one in-memory connection
        self._reading = 0  # cached requests in flight: the cache is trimmed only when there are none
        self._trimming = False
        self._trim_done = asyncio.Event()
        if not cache:
            return
        try:
            # No path: a database in memory, so a replay never reads an earlier run's and tests touch no disk.
            self._storage = (
                _open_storage(cache_path) if cache_path else hishel.AsyncSqliteStorage(default_ttl=CACHE_TTL)
            )
            self._cached = hishel_httpx.AsyncCacheTransport(
                self._retrying,
                storage=self._storage,
                policy=hishel.SpecificationPolicy(hishel.CacheOptions(shared=False)),
            )
        except Exception as exc:  # an unusable cache costs the cache, not the app
            log.warning("http cache unavailable (%s: %s); continuing without it", type(exc).__name__, exc)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        token = _started.set(self._clock.monotonic())
        try:
            return await self._serve(request)
        finally:
            _started.reset(token)

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        now = self._clock.monotonic()
        endpoint = _endpoint(request.url)
        self._blocked = {k: until for k, until in self._blocked.items() if until > now}
        if (remaining := self._blocked.get(endpoint, 0.0) - now) > 0:
            log.debug("polite: %s asked us to wait, %.0fs left", endpoint, remaining)
            return httpx.Response(429, headers={"Retry-After": str(math.ceil(remaining))}, request=request)
        use_cache = (
            self._cached is not None
            and request.method == "GET"
            and not request.url.query
            and not _CALLER_VALIDATORS & {k.lower() for k in request.headers}
        )
        key = str(request.url)
        if use_cache and key not in self._unvalidated:
            response = await self._through_cache(request)
            if response.status_code == 200 and not _has_validator(response):
                self._remember_unvalidated(key)
        else:
            response = await self._retrying.handle_async_request(request)
            if use_cache and _has_validator(response):  # it offers validators now: the cache has a use for it
                self._unvalidated.discard(key)
        if response.status_code in (429, 503):
            self._note_rate_limit(endpoint, response, self._clock.monotonic())  # counted from after the retries
        return response

    def _remember_unvalidated(self, key: str) -> None:
        if len(self._unvalidated) >= CACHE_UNVALIDATED_URLS:
            self._unvalidated.clear()
        self._unvalidated.add(key)

    async def _through_cache(self, request: httpx.Request) -> httpx.Response:
        assert self._cached is not None
        request.headers["Cache-Control"] = _NO_CACHE_MARK  # revalidate, never answer from the cache unasked
        if (packed := _pack_timeout(request.extensions.get("timeout"))) is not None:
            request.headers[_TIMEOUT_HEADER] = packed
        response: httpx.Response | None = None
        try:
            await self._wait_for_trim()
            self._reading += 1
            try:
                if self._storage and self._storage.connection is None and not self._cache_path:
                    async with self._connect_lock:
                        if self._storage.connection is None:
                            self._storage.connection = await anysqlite.connect(":memory:")
                response = await self._cached.handle_async_request(request)
                await self._read_body(response, request)  # failures of the cache's own saving belong in this try
                return response
            finally:
                self._reading -= 1
        except (httpx.HTTPError, asyncio.CancelledError):
            raise
        except Exception as exc:  # a corrupt or locked cache file: not the feed's fault, so never a failed poll
            log.warning("http cache failed (%s: %s); continuing without it", type(exc).__name__, exc)
            self._cached = None
            if response is not None:
                await response.aclose()
            return await self._retrying.handle_async_request(request)

    async def _read_body(self, response: httpx.Response, request: httpx.Request) -> None:
        """Read the body the cache is saving, inside the entry's deadline when there is one (see RetryTransport)."""
        entry = _entry_deadline.get()
        if entry is None:
            await response.aread()
            return
        left = entry - 1.0 - self._clock.monotonic()
        if left <= 0:
            raise httpx.ReadTimeout("no time left", request=request)
        try:
            async with asyncio.timeout(left):
                await response.aread()
        except TimeoutError:
            raise httpx.ReadTimeout("entry deadline spent reading the body", request=request) from None

    async def _wait_for_trim(self) -> None:
        """Trim the cache when it is due and nothing is reading it; a request arriving meanwhile waits."""
        while self._trimming:
            await self._trim_done.wait()
        self._cached_requests += 1
        if self._cached_requests % CACHE_TRIM_EVERY or not self._cache_path or self._reading:
            return
        self._trimming = True
        self._trim_done.clear()
        try:
            await asyncio.to_thread(_trim, self._cache_path)
        finally:
            self._trimming = False
            self._trim_done.set()

    def _note_rate_limit(self, key: str, response: httpx.Response, now: float) -> None:
        header = response.headers.get("Retry-After")
        if not header and response.status_code != 429:
            return
        wait = min(retry_after(header, self._clock.now(), RETRY_AFTER_DEFAULT), RETRY_AFTER_CAP)
        self._blocked[key] = now + wait
        log.warning("%s answered %d; leaving it alone for %.0fs", key, response.status_code, wait)

    async def aclose(self) -> None:
        try:
            if self._cached is not None:
                await self._cached.aclose()
                return
        except Exception as exc:
            log.debug("closing the http cache failed: %s", exc)
        await self._retrying.aclose()


def _has_validator(response: httpx.Response) -> bool:
    return "ETag" in response.headers or "Last-Modified" in response.headers


def _trim(path: Path) -> None:
    """Empty the cache when what it holds (free pages not counted) is over CACHE_MAX_BYTES: one-shot URLs such as quake
    details would otherwise fill it for the whole TTL. A failure only means the next check tries again."""
    try:
        with closing(sqlite3.connect(path, timeout=1.0)) as db:
            pages = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
            if pages * db.execute("PRAGMA page_size").fetchone()[0] > CACHE_MAX_BYTES:
                log.info("http cache over %d bytes: emptied", CACHE_MAX_BYTES)
                db.execute("PRAGMA foreign_keys = ON")  # a deleted entry takes its stored body with it
                with db:  # not the youngest entries: a response still being read is writing its body there
                    db.execute("DELETE FROM entries WHERE created_at < ?", (time.time() - CACHE_TRIM_MIN_AGE,))
    except sqlite3.Error as exc:
        log.debug("http cache size check failed: %s", exc)


def _open_storage(path: Path) -> hishel.AsyncSqliteStorage:
    """The cache database at `path`. A file that is over CACHE_MAX_BYTES or not a readable database is deleted first;
    entries older than CACHE_TTL are not served."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.stat().st_size > CACHE_MAX_BYTES:
                raise OSError("over the size limit")
            with closing(sqlite3.connect(path)) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("integrity check failed")
        except (OSError, sqlite3.Error) as exc:
            log.warning("discarding the http cache %s (%s)", path, exc)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
    return hishel.AsyncSqliteStorage(database_path=path, default_ttl=CACHE_TTL)


_SEEN_PROXIES: dict[str, bool] = {}  # setting -> usable; each bad one is logged once


def redact_proxy(value: str) -> str:
    """A proxy URL with any password replaced, safe to log or print."""
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        return "<unparseable>"
    if url.password:
        return str(url.copy_with(userinfo=f"{url.username}:***".encode()))
    return str(url)


def _usable(value: str) -> bool:
    if value not in _SEEN_PROXIES:
        try:
            httpx.Proxy(value)
            _SEEN_PROXIES[value] = True
        except (httpx.InvalidURL, ValueError) as exc:
            _SEEN_PROXIES[value] = False
            log.warning("ignoring the proxy setting %s (%s); using a direct connection", redact_proxy(value), exc)
    return _SEEN_PROXIES[value]


def proxy_for(url: httpx.URL) -> str | None:
    """The proxy the environment names for this URL (HTTPS_PROXY, HTTP_PROXY, ALL_PROXY, NO_PROXY, and on Windows the
    system setting), or None. httpx only reads these when it builds the transport itself, and the polite transport
    below is passed in, so the choice is made here. A setting httpx cannot use is logged once and skipped, so a typo in
    a proxy variable costs the proxy, not every feed."""
    if urllib.request.proxy_bypass(url.host):  # type: ignore[attr-defined]
        return None
    proxies = urllib.request.getproxies()
    value = proxies.get(url.scheme) or proxies.get("all")
    return value if value and _usable(value) else None


class EnvProxyTransport(httpx.AsyncBaseTransport):
    """Sends each request direct or through the proxy the environment names for it; one connection pool per proxy."""

    def __init__(self) -> None:
        self._direct = httpx.AsyncHTTPTransport()
        self._proxied: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        proxy = proxy_for(request.url)
        if proxy is None:
            return await self._direct.handle_async_request(request)
        if proxy not in self._proxied:
            self._proxied[proxy] = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy))
        return await self._proxied[proxy].handle_async_request(request)

    async def aclose(self) -> None:
        for transport in (self._direct, *self._proxied.values()):
            await transport.aclose()


def cache_path() -> Path:
    return data_dir() / "cache" / "http.sqlite"


def build_client(
    contact: str = "", transport: httpx.AsyncBaseTransport | None = None, clock: Clock | None = None, cache: bool = True
) -> httpx.AsyncClient:
    """The one shared client: identifying User-Agent, gzip, 10 s connect and 30 s read timeouts, polite transport.

    Without an explicit transport requests honour the proxy environment and the HTTP cache is a file in the data
    directory. Recording and replay pass their own transport; their cache lives in memory, so a replay
    never reads what an earlier run left on disk. `cache=False` leaves the cache out; retries and politeness stay."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact), "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        transport=PoliteTransport(
            transport or EnvProxyTransport(), clock or SystemClock(), None if transport else cache_path(), cache
        ),
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport and appends every exchange to a JSONL file as {ts, method, url, status, headers, body}."""

    def __init__(self, inner: httpx.AsyncBaseTransport, path: Path, clock: Clock):
        self._inner = inner
        self._path = Path(path)
        self._clock = clock
        self._failed = False  # a write failed: recording stopped, the exchange itself carries on
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
        if not self._failed:
            try:
                self._append(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:  # a full disk or a locked file must not turn every poll into a failure
                self._failed = True
                log.error("recording to %s failed and is stopped: %s", self._path, exc)
        await response.aclose()
        return httpx.Response(response.status_code, headers=headers, content=body, request=request)

    def _append(self, line: str) -> None:
        if self._path.exists() and self._path.stat().st_size:
            with self._path.open("rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":  # an earlier run was cut off mid-line: end it so this record stands alone
                    line = "\n" + line
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)

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
