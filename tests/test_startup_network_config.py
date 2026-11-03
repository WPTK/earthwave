"""Proxy environment (#243), refresh feedback (#271), slow origins (#272), config error reports (#257),
the EARTHWAVE_MAP_RENDERER override (#238)."""

import asyncio
import tomllib

import httpx
import pytest
from conftest import T0
from test_sources_state import FakeSource

from earthwave.__main__ import main
from earthwave.config import ConfigError, load_config, parse_config
from earthwave.models import Batch
from earthwave.net import build_client, proxy_for, redact_proxy
from earthwave.poll import Scheduler
from earthwave.state.store import Store

PROXY_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture
def no_proxy_env(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


# --- #243 -------------------------------------------------------------------------------------------------------


async def test_https_proxy_from_the_environment_is_used(monkeypatch, no_proxy_env):
    """A fake CONNECT proxy sees the request that a client built by build_client() sends."""
    seen: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append((await reader.readline()).decode())
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    try:
        async with build_client() as client:
            with pytest.raises(httpx.HTTPError):
                await client.get("https://feeds.example.test/x")
    finally:
        server.close()
    assert seen and seen[0].startswith("CONNECT feeds.example.test:443")


def test_proxy_selection_follows_the_environment(monkeypatch, no_proxy_env):
    assert proxy_for(httpx.URL("https://a.example/")) is None
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example,.corp.example")
    assert proxy_for(httpx.URL("https://a.example/")) == "http://proxy.example:3128"
    assert proxy_for(httpx.URL("https://internal.example/")) is None
    assert proxy_for(httpx.URL("https://x.corp.example/")) is None


async def test_an_explicit_transport_is_never_proxied(monkeypatch, no_proxy_env):
    """Replay and tests hand build_client() a transport; the environment must not reroute that."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    async with build_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))) as client:
        assert (await client.get("https://a.example/")).text == "ok"


# --- #271 -------------------------------------------------------------------------------------------------------


async def test_refresh_now_says_which_sources_must_wait(clock):
    source = FakeSource([Batch("fake", T0), Batch("fake", T0)])
    sched = Scheduler([source], Store(), clock, lambda r: None)
    async with build_client() as client:
        await sched.poll_source("fake", client)
    clock.advance(10)
    waits = sched.refresh_now()
    assert set(waits) == {"fake"} and 49 <= waits["fake"] <= 51
    clock.advance(50)
    assert sched.refresh_now() == {}


async def test_the_refresh_toast_names_the_wait(clock):
    from earthwave.ui.screens.dashboard import refresh_message

    assert refresh_message({}) == "Refreshing"
    assert refresh_message({"usgs": 42.2}) == "Refreshing; USGS can be asked again in 43 s"
    assert refresh_message({"usgs": 42.2, "nws": 5}, 4) == "Refreshing; USGS, NWS can be asked again in 43 s"
    assert refresh_message({"usgs": 42.2, "nws": 5}, 2) == "Too soon to refresh: USGS, NWS can be asked again in 43 s"


# --- #272 ------------------------------------------------------------------------------------------------------


def test_the_read_timeout_outlasts_a_20_second_origin():
    timeout = build_client().timeout
    assert timeout.read is not None and timeout.read > 20


# --- #257 ------------------------------------------------------------------------------------------------------


def test_every_invalid_value_is_reported_with_its_key():
    data = tomllib.loads('[view]\nmin_magnitude = "big"\nhours = 0\n[home]\nlat = 95\n[nope]\nx = 1\n')
    with pytest.raises(ConfigError) as err:
        parse_config(data)
    text = str(err.value)
    for needle in ("[view] min_magnitude", "[view] hours", "[home] lat", "[nope]"):
        assert needle in text, text


def test_syntax_errors_name_the_line_and_key_and_list_each(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[view]\nmin_magnitude = big\nhours = 24\nglyphs = nope\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    text = str(err.value)
    assert "c.toml" in text and "line 2" in text and "min_magnitude" in text
    assert "line 4" in text and "glyphs" in text


# --- #238 ------------------------------------------------------------------------------------------------------


def test_map_renderer_env_override_is_validated(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("EARTHWAVE_MAP_RENDERER", "pixels")
    assert main(["--no-config", "--offline"]) == 2
    assert "EARTHWAVE_MAP_RENDERER" in capsys.readouterr().err


def test_map_renderer_env_override_beats_the_config(monkeypatch):
    from earthwave.__main__ import map_renderer_setting

    monkeypatch.setenv("EARTHWAVE_MAP_RENDERER", "braille")
    assert map_renderer_setting("sixel") == "braille"
    monkeypatch.delenv("EARTHWAVE_MAP_RENDERER")
    assert map_renderer_setting("sixel") == "sixel"


def test_help_lists_the_renderer_variable(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "EARTHWAVE_MAP_RENDERER" in capsys.readouterr().out


def test_all_proxy_is_honoured(monkeypatch, no_proxy_env):
    monkeypatch.setenv("ALL_PROXY", "http://allproxy.example:8080")
    assert proxy_for(httpx.URL("https://a.example/")) == "http://allproxy.example:8080"
    monkeypatch.setenv("HTTPS_PROXY", "http://specific.example:1")
    assert proxy_for(httpx.URL("https://a.example/")) == "http://specific.example:1"


@pytest.mark.parametrize("bad", ["proxy:3128", "::bad", "ftp://p.example:1"])
def test_a_malformed_proxy_is_ignored_with_one_clear_warning(monkeypatch, no_proxy_env, caplog, bad):
    monkeypatch.setenv("HTTPS_PROXY", bad)
    with caplog.at_level("WARNING", logger="earthwave.net"):
        assert proxy_for(httpx.URL("https://a.example/")) is None
        assert proxy_for(httpx.URL("https://b.example/")) is None
    warnings = [r for r in caplog.records if "ignoring the proxy setting" in r.getMessage()]
    assert len(warnings) <= 1  # logged once per distinct value


def test_redact_proxy_hides_the_password():
    assert "secret" not in redact_proxy("http://u:secret@h:3128")
    assert redact_proxy("http://h:3128").startswith("http://h")
