"""The HTTP layer: bounded retries (stamina), the on-disk conditional-request cache (hishel), and what they must not hide."""

import logging
import sqlite3

import httpx
import pytest
from conftest import T0

import earthwave.net as net
from earthwave.net import RETRY_ATTEMPTS, PoliteTransport, RetryTransport, build_client, cache_path


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


async def test_retries_stay_inside_the_poll_deadline():
    from earthwave import poll

    worst = net.RETRY_WINDOW + 2 * net.RETRY_INLINE_MAX + 30.0  # last try may start at the window's end and read 30 s
    assert worst < poll.FETCH_DEADLINE
    assert RetryTransport is not None


async def test_a_cache_over_its_limit_is_emptied_of_old_entries(clock, tmp_path, monkeypatch):
    path = tmp_path / "h.sqlite"
    client, seen = _client(clock, lambda r, n: httpx.Response(200, text="x" * 5000, headers={"ETag": '"a"'}), path)
    async with client:
        await client.get("https://example.test/f")
        await client.get("https://example.test/f")
        assert "if-none-match" in seen[1].headers
        monkeypatch.setattr(net, "CACHE_TRIM_MIN_AGE", -1.0)  # everything counts as old
        net._trim(path)  # under the limit: nothing happens
        await client.get("https://example.test/f")
        assert "if-none-match" in seen[2].headers
        monkeypatch.setattr(net, "CACHE_MAX_BYTES", 1)
        net._trim(path)
        await client.get("https://example.test/f")
    assert "if-none-match" not in seen[3].headers


async def test_trimming_runs_every_so_many_cached_requests(clock, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(net, "_trim", calls.append)
    monkeypatch.setattr(net, "CACHE_TRIM_EVERY", 2)
    client, _ = _client(
        clock, lambda r, n: httpx.Response(200, text="x", headers={"ETag": '"a"'}), tmp_path / "h.sqlite"
    )
    async with client:
        for _ in range(5):
            await client.get("https://example.test/f")
    assert calls == [tmp_path / "h.sqlite"] * 2


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
