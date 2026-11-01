"""HTTP client factory plus the record and replay transports."""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

from earthwave import __version__
from earthwave.clock import Clock, ReplayClock

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/WPTK/earthwave"
_DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def user_agent(contact: str = "") -> str:
    suffix = f"; {contact}" if contact else ""
    return f"earthwave/{__version__} (+{REPO_URL}{suffix})"


def build_client(contact: str = "", transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The one shared client: identifying User-Agent, gzip, 10 s connect and 20 s read timeouts."""
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(contact), "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        transport=transport,
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
