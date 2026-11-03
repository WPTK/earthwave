"""The HTTP layer: bounded retries (stamina), the on-disk conditional-request cache (hishel), and what they must not hide."""

import logging
import sqlite3

import httpx
import pytest
from conftest import T0

import earthwave.net as net
from earthwave.net import RETRY_ATTEMPTS, PoliteTransport, build_client, cache_path


def _slept(clock) -> list[float]:
    """Record the clock's sleeps instead of sleeping them."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    clock.sleep = sleep
    return sleeps


def _client(clock, handler, cache=None):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request, len(seen))

    transport = PoliteTransport(httpx.MockTransport(wrapped), clock, cache)
    return httpx.AsyncClient(transport=transport), seen


async def test_a_5xx_is_asked_again_and_the_later_answer_is_used(clock):
    client, seen = _client(clock, lambda r, n: httpx.Response(503 if n < 3 else 200, text="ok"))
    async with client:
        got = await client.get("https://example.test/a")
    assert (got.status_code, got.text, len(seen)) == (200, "ok", 3)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_a_source_that_stays_down_still_fails_after_bounded_tries(clock, status):
    client, seen = _client(clock, lambda r, n: httpx.Response(status))
    async with client:
        got = await client.get("https://example.test/a")
    assert got.status_code == status and len(seen) == RETRY_ATTEMPTS
    with pytest.raises(httpx.HTTPStatusError):
        got.raise_for_status()


async def test_connect_errors_and_timeouts_are_retried_then_raised(clock):
    def handler(request, n):
        raise httpx.ConnectError("down") if n % 2 else httpx.ReadTimeout("slow")

    client, seen = _client(clock, handler)
    async with client:
        with pytest.raises(httpx.TransportError):
            await client.get("https://example.test/a")
    assert len(seen) == RETRY_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 501])
async def test_other_answers_are_final(clock, status):
    client, seen = _client(clock, lambda r, n: httpx.Response(status))
    async with client:
        got = await client.get("https://example.test/a")
    assert got.status_code == status and len(seen) == 1


async def test_only_get_and_head_are_retried(clock):
    client, seen = _client(clock, lambda r, n: httpx.Response(503))
    async with client:
        got = await client.post("https://example.test/a", content=b"x")
    assert got.status_code == 503 and len(seen) == 1


async def test_a_short_retry_after_is_waited_out_on_the_injected_clock(clock):
    sleeps = _slept(clock)
    client, seen = _client(
        clock, lambda r, n: httpx.Response(429, headers={"Retry-After": "5"}) if n == 1 else httpx.Response(200)
    )
    async with client:
        got = await client.get("https://example.test/a?q=1")
    assert got.status_code == 200 and len(seen) == 2 and sleeps == [5.0]


async def test_a_long_retry_after_is_not_waited_in_place_but_leaves_the_endpoint_alone(clock):
    client, seen = _client(clock, lambda r, n: httpx.Response(429, headers={"Retry-After": "120"}))
    async with client:
        first = await client.get("https://example.test/a")
        again = await client.get("https://example.test/a")
    assert first.status_code == again.status_code == 429 and len(seen) == 1 and clock.now() == T0
    assert again.headers["retry-after"] == "120"


async def test_a_503_with_retry_after_is_waited_for_when_short(clock):
    sleeps = _slept(clock)
    client, seen = _client(
        clock, lambda r, n: httpx.Response(503, headers={"Retry-After": "2"}) if n == 1 else httpx.Response(200)
    )
    async with client:
        assert (await client.get("https://example.test/a")).status_code == 200
    assert len(seen) == 2 and sleeps == [2.0]


async def test_the_cache_hint_is_not_sent_to_the_origin(clock):
    client, seen = _client(clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}))
    async with client:
        await client.get("https://example.test/a")
        await client.get("https://example.test/a")
    assert all("cache-control" not in r.headers for r in seen)


async def test_a_304_with_no_validator_on_the_request_is_an_error_not_a_confirmation(clock):
    client, _ = _client(clock, lambda r, n: httpx.Response(200, text="x") if n == 1 else httpx.Response(304))
    async with client:
        await client.get("https://example.test/a")
        with pytest.raises(httpx.ProtocolError, match="304"):
            await client.get("https://example.test/a")


async def test_a_callers_304_reaches_the_caller(clock):
    client, _ = _client(clock, lambda r, n: httpx.Response(304))
    async with client:
        got = await client.get("https://example.test/a", headers={"If-Modified-Since": "x"})
    assert got.status_code == 304


async def test_the_cache_survives_a_restart_and_still_asks_the_origin(clock, tmp_path):
    path = tmp_path / "cache" / "http.sqlite"
    first, _ = _client(clock, lambda r, n: httpx.Response(200, text="body", headers={"ETag": '"v1"'}), path)
    async with first:
        assert (await first.get("https://example.test/f")).text == "body"

    second, seen = _client(clock, lambda r, n: httpx.Response(304), path)
    async with second:
        got = await second.get("https://example.test/f")
    assert (got.status_code, got.text) == (200, "body")
    assert seen[0].headers["if-none-match"] == '"v1"'  # revalidated, not answered unasked


async def test_a_corrupt_cache_file_is_replaced_not_fatal(clock, tmp_path, caplog):
    path = tmp_path / "http.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    with caplog.at_level(logging.WARNING, logger="earthwave.net"):
        client, seen = _client(clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}), path)
        async with client:
            assert (await client.get("https://example.test/f")).text == "x"
    assert "discarding the http cache" in caplog.text and len(seen) == 1
    with sqlite3.connect(path) as db:  # a database again
        db.execute("PRAGMA quick_check").fetchall()


async def test_an_oversized_cache_file_is_discarded(clock, tmp_path, monkeypatch):
    path = tmp_path / "http.sqlite"
    first, _ = _client(clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}), path)
    async with first:
        await first.get("https://example.test/f")
    monkeypatch.setattr(net, "CACHE_MAX_BYTES", 1)
    second, seen = _client(clock, lambda r, n: httpx.Response(200, text="y", headers={"ETag": '"b"'}), path)
    async with second:
        got = await second.get("https://example.test/f")
    assert got.text == "y" and "if-none-match" not in seen[0].headers


async def test_a_cache_that_fails_while_running_is_dropped_and_the_request_still_goes(clock, tmp_path, caplog):
    client, seen = _client(clock, lambda r, n: httpx.Response(200, text="x"), tmp_path / "http.sqlite")
    transport = client._transport
    assert isinstance(transport, PoliteTransport) and transport._cached is not None

    async def broken(request):
        raise sqlite3.DatabaseError("database disk image is malformed")

    transport._cached.handle_async_request = broken  # type: ignore[method-assign]
    async with client:
        with caplog.at_level(logging.WARNING, logger="earthwave.net"):
            assert (await client.get("https://example.test/f")).text == "x"
        assert (await client.get("https://example.test/f")).text == "x"
    assert caplog.text.count("http cache failed") == 1 and len(seen) == 2


async def test_a_network_error_through_the_cache_is_still_the_networks_error(clock, tmp_path):
    def handler(request, n):
        raise httpx.ConnectError("down")

    client, _ = _client(clock, handler, tmp_path / "http.sqlite")
    async with client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://example.test/f")
    assert client._transport._cached is not None  # not mistaken for a broken cache


def test_the_cache_lives_in_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("EARTHWAVE_DATA_DIR", str(tmp_path))
    assert cache_path().parent.parent == tmp_path


async def test_an_explicit_transport_gets_no_disk_cache(clock, tmp_path, monkeypatch):
    monkeypatch.setenv("EARTHWAVE_DATA_DIR", str(tmp_path))
    async with build_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"ETag": '"a"'})), clock=clock
    ) as c:
        await c.get("https://example.test/f")
    assert not (tmp_path / "cache").exists()


async def test_a_client_without_the_cache_still_retries(clock):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503 if len(seen) < 2 else 200, headers={"ETag": '"a"'})

    async with build_client(transport=httpx.MockTransport(handler), clock=clock, cache=False) as client:
        assert (await client.get("https://example.test/a")).status_code == 200
        await client.get("https://example.test/a")
    assert len(seen) == 3 and all("if-none-match" not in r.headers for r in seen)


async def test_a_url_that_never_offers_a_validator_stops_going_through_the_cache(clock, tmp_path):
    client, seen = _client(clock, lambda r, n: httpx.Response(200, text="x"), tmp_path / "h.sqlite")
    transport = client._transport
    calls = []
    original = transport._cached.handle_async_request

    async def spy(request):
        calls.append(request.url)
        return await original(request)

    transport._cached.handle_async_request = spy
    async with client:
        for _ in range(4):
            await client.get("https://example.test/plain")
    assert len(seen) == 4 and len(calls) == 1


def _ticking(clock) -> list[float]:
    """The clock's sleeps advance it, so elapsed time can be measured without waiting."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    clock.sleep = sleep
    return sleeps


async def test_the_clients_timeout_reaches_the_origin_through_the_cache(clock, tmp_path):
    client, seen = _client(
        clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}), tmp_path / "h.sqlite"
    )
    async with client:
        await client.get(
            "https://example.test/f", extensions={"timeout": {"connect": 3.0, "read": 7.0, "write": None, "pool": 2.0}}
        )
        await client.get(
            "https://example.test/f", extensions={"timeout": {"connect": 3.0, "read": 7.0, "write": None, "pool": 2.0}}
        )
    assert all(r.extensions["timeout"] == {"connect": 3.0, "read": 7.0, "write": None, "pool": 2.0} for r in seen)
    assert all("x-earthwave-timeout" not in r.headers for r in seen)


async def test_repeated_connect_timeouts_end_inside_the_entry_limit_as_an_httpx_error(clock):
    from earthwave.sources.base import ENTRY_LIMIT

    ticks = []

    def handler(request, n):
        clock.advance(10.0)  # a connect timeout takes its 10 s
        ticks.append(clock.monotonic())
        raise httpx.ConnectTimeout("slow", request=request)

    _ticking(clock)
    client, seen = _client(clock, handler)
    start = clock.monotonic()
    async with client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://example.test/a")
    assert clock.monotonic() - start < ENTRY_LIMIT and net.RETRY_BUDGET < ENTRY_LIMIT
    assert 1 < len(seen) < RETRY_ATTEMPTS + 1


async def test_a_repeated_ten_second_retry_after_ends_inside_the_entry_limit_and_does_not_sleep_after_the_last_try(
    clock,
):
    from earthwave.sources.base import ENTRY_LIMIT

    sleeps = _ticking(clock)
    client, seen = _client(clock, lambda r, n: httpx.Response(429, headers={"Retry-After": "10"}))
    start = clock.monotonic()
    async with client:
        got = await client.get("https://example.test/a")
    assert got.status_code == 429 and clock.monotonic() - start < ENTRY_LIMIT
    assert len(sleeps) == len(seen) - 1  # nothing slept after the answer that was returned


async def test_a_429_without_retry_after_is_retried_once_after_five_seconds_then_blocks_a_minute_from_after_that(clock):
    sleeps = _ticking(clock)
    client, seen = _client(clock, lambda r, n: httpx.Response(429))
    async with client:
        first = await client.get("https://example.test/a")
        again = await client.get("https://example.test/a")
    assert first.status_code == 429 and sleeps[0] == 5.0
    assert again.headers["retry-after"] == "60" and len(seen) == len(sleeps) + 1  # the second call never left


class _BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise sqlite3.OperationalError("database or disk is full")
        yield b""  # pragma: no cover


async def test_a_cache_failure_while_the_body_streams_is_not_a_failed_poll(clock, tmp_path, caplog):
    client, seen = _client(
        clock, lambda r, n: httpx.Response(200, text="fine", headers={"ETag": '"a"'}), tmp_path / "h.sqlite"
    )
    transport = client._transport

    async def broken(request):
        return httpx.Response(200, stream=_BrokenBody(), request=request)

    transport._cached.handle_async_request = broken
    async with client:
        with caplog.at_level(logging.WARNING, logger="earthwave.net"):
            got = await client.get("https://example.test/f")
    assert got.text == "fine" and len(seen) == 1 and "http cache failed" in caplog.text


async def test_the_cache_is_not_trimmed_while_a_read_is_in_flight(clock, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(net, "_trim", calls.append)
    monkeypatch.setattr(net, "CACHE_TRIM_EVERY", 1)
    client, _ = _client(
        clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}), tmp_path / "h.sqlite"
    )
    transport = client._transport
    async with client:
        transport._reading = 1
        await client.get("https://example.test/f")
        assert calls == []
        transport._reading = 0
        await client.get("https://example.test/f")
    assert len(calls) == 1


async def test_requests_wait_while_the_cache_is_trimmed(clock, tmp_path, monkeypatch):
    import asyncio
    import threading

    gate = threading.Event()
    order = []

    def slow_trim(path):
        order.append("trim start")
        gate.wait(5)
        order.append("trim end")

    monkeypatch.setattr(net, "_trim", slow_trim)
    monkeypatch.setattr(net, "CACHE_TRIM_EVERY", 1)

    def handler(request, n):
        order.append("origin")
        return httpx.Response(200, text="x", headers={"ETag": '"a"'})

    client, _ = _client(clock, handler, tmp_path / "h.sqlite")
    async with client:
        first = asyncio.create_task(client.get("https://example.test/a"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(client.get("https://example.test/b"))
        await asyncio.sleep(0.05)
        assert order == ["trim start"]  # neither request has reached the origin
        gate.set()
        await asyncio.gather(first, second)
    assert order[:2] == ["trim start", "trim end"]


async def test_a_database_that_fails_its_integrity_check_is_replaced(clock, tmp_path, caplog):
    path = tmp_path / "h.sqlite"
    first, _ = _client(clock, lambda r, n: httpx.Response(200, text="x" * 20000, headers={"ETag": '"a"'}), path)
    async with first:
        for i in range(6):
            await first.get(f"https://example.test/{i}")
    data = bytearray(path.read_bytes())
    for at in range(4096 * 2, len(data) - 100, 997):  # damage the pages after the header
        data[at : at + 40] = bytes([255]) * 40
    path.write_bytes(bytes(data))
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"h.sqlite{suffix}").unlink(missing_ok=True)
    with caplog.at_level(logging.WARNING, logger="earthwave.net"):
        second, seen = _client(clock, lambda r, n: httpx.Response(200, text="y", headers={"ETag": '"b"'}), path)
        async with second:
            assert (await second.get("https://example.test/0")).text == "y"
    assert "discarding the http cache" in caplog.text and "if-none-match" not in seen[0].headers


async def test_parallel_first_requests_share_one_in_memory_connection(clock, caplog):
    import asyncio

    client, _seen = _client(clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}))
    async with client:
        with caplog.at_level(logging.WARNING, logger="earthwave.net"):
            got = await asyncio.gather(*(client.get(f"https://example.test/{i}") for i in range(8)))
    assert [g.status_code for g in got] == [200] * 8 and "http cache failed" not in caplog.text


# --- one entry, several requests, one deadline ---------------------------------------------------------------------

CAP_URL = "https://www.tsunami.gov/events/PAAQ/2026/10/25/tnfxzn/1/WEAK53/PAAQCAP.xml"
BULLETIN_URL = CAP_URL.replace("PAAQCAP.xml", "WEAK53.txt")


def _tsunami_entry():
    return {"id": "PAAQ-1-tnfxzn", "cap_url": CAP_URL, "bulletin_url": BULLETIN_URL}


def _entry_world(clock, handler):
    """A client whose answers take simulated time, and the source that reads one entry through it."""
    from conftest import FIXTURES

    from earthwave.sources import tsunamigov

    sleeps = _ticking(clock)
    cap = (FIXTURES / "tsunamigov" / "PAAQ_0.cap.xml").read_text(encoding="utf-8")
    bulletin = (FIXTURES / "tsunamigov" / "PAAQ_0.txt").read_text(encoding="utf-8")
    tries: dict[str, int] = {}

    def wrapped(request, n):
        url = str(request.url)
        tries[url] = tries.get(url, 0) + 1
        return handler(request, url, tries[url], cap, bulletin)

    client, _ = _client(clock, wrapped)
    return client, tsunamigov.TsunamiGovSource(clock), tries, sleeps


async def test_a_tsunami_entry_ends_on_an_httpx_error_inside_the_limit_and_never_counts(clock):
    from earthwave.sources.base import ENTRY_LIMIT
    from earthwave.sources.tsunamigov import permanent, stalled

    def handler(request, url, n, cap, bulletin):
        if url == CAP_URL:
            if n == 1:
                clock.advance(10.0)  # the first connect times out after its 10 s
                raise httpx.ConnectTimeout("slow", request=request)
            return httpx.Response(200, text=cap)
        clock.advance(10.0)  # the bulletin never connects
        raise httpx.ConnectTimeout("slow", request=request)

    client, source, tries, _ = _entry_world(clock, handler)
    alerts: list = []
    start = clock.monotonic()
    async with client:
        assert await source._entry(client, _tsunami_entry(), alerts) is False
    assert clock.monotonic() - start < ENTRY_LIMIT
    assert tries[CAP_URL] == 2 and tries[BULLETIN_URL] == 1  # the bulletin had no room left for a retry
    assert len(alerts) == 1 and source._failures == {}  # the CAP's alert is shown; nothing counted toward giving up
    assert not stalled(httpx.ConnectTimeout("x")) and not permanent(httpx.ConnectTimeout("x"))


async def test_a_tsunami_entry_with_ten_second_retry_afters_ends_inside_the_limit_and_never_counts(clock):
    from earthwave.sources.base import ENTRY_LIMIT

    def handler(request, url, n, cap, bulletin):
        if url == CAP_URL:
            return httpx.Response(429, headers={"Retry-After": "10"}) if n < 3 else httpx.Response(200, text=cap)
        return httpx.Response(503, headers={"Retry-After": "10"})

    client, source, _tries, sleeps = _entry_world(clock, handler)
    alerts: list = []
    start = clock.monotonic()
    async with client:
        assert await source._entry(client, _tsunami_entry(), alerts) is False
    assert clock.monotonic() - start < ENTRY_LIMIT and sum(sleeps) < ENTRY_LIMIT - net.ENTRY_MARGIN
    assert source._failures == {}


async def test_the_first_try_of_the_second_request_is_cut_inside_the_entry_limit(clock):
    """A request that hangs is ended with an httpx timeout before the entry's own limit fires as a builtin one."""
    import asyncio

    from earthwave.sources.base import ENTRY_LIMIT

    def handler(request, url, n, cap, bulletin):
        raise AssertionError("not reached")

    client, _source, _, _ = _entry_world(clock, handler)

    async def hang(request):
        await asyncio.sleep(3600)

    client._transport._retrying._inner = httpx.MockTransport(hang)
    clock.monotonic = lambda: 0.0  # real time passes in the wait; the deadline is read off this frozen clock
    async with client, net.entry_deadline(clock, 0.3):
        with pytest.raises(httpx.TimeoutException):
            async with asyncio.timeout(5):
                await client.get(CAP_URL)
    assert ENTRY_LIMIT > net.ENTRY_MARGIN


async def test_the_refetch_after_a_cache_failure_uses_what_is_left_of_the_budget(clock, tmp_path):
    ticks = _ticking(clock)

    def handler(request, n):
        raise httpx.ConnectTimeout("slow", request=request)

    client, seen = _client(clock, handler, tmp_path / "h.sqlite")
    transport = client._transport

    async def broken(request):
        clock.advance(20.0)  # the cached try used most of the budget, then the cache itself failed
        raise sqlite3.OperationalError("disk I/O error")

    transport._cached.handle_async_request = broken
    async with client:
        with pytest.raises(httpx.TimeoutException):
            await client.get("https://example.test/f")
    assert len(seen) <= 1 and sum(ticks) == 0  # no fresh 22 s: at most the first try, and no retry


async def test_a_jma_report_that_times_out_is_an_outage_and_never_counts(clock):
    from earthwave.sources import jma
    from earthwave.sources.base import ENTRY_LIMIT

    sleeps = _ticking(clock)

    def handler(request, n):
        clock.advance(10.0)
        raise httpx.ConnectTimeout("slow", request=request)

    client, seen = _client(clock, handler)
    source = jma.JmaSource(clock)
    entry = {"json": "20261101_x.json", "rdt": clock.now().isoformat()}
    start = clock.monotonic()
    async with client:
        for _ in range(jma.MAX_ENTRY_FAILURES + 2):
            clock.advance(0.0)
            assert await source._report(client, entry, clock.now(), {}, [], []) is False
    assert source._failures == {} and clock.monotonic() - start < ENTRY_LIMIT * (jma.MAX_ENTRY_FAILURES + 2)
    assert sleeps is not None and len(seen) >= jma.MAX_ENTRY_FAILURES
