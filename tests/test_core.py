"""clock, net (record/replay), config, geo."""

import tomllib
from dataclasses import asdict
from datetime import timedelta

import httpx
import pytest
from conftest import T0

from earthwave import __version__
from earthwave.clock import ReplayClock, SystemClock
from earthwave.config import (
    TEMPLATE,
    Config,
    ConfigError,
    HomeConfig,
    load_config,
    parse_config,
    parse_home,
    with_overrides,
)
from earthwave.logging_setup import setup_logging
from earthwave.net import (
    RecordingTransport,
    ReplayTransport,
    build_client,
    recording_start,
    retry_after_of,
    user_agent,
)
from earthwave.sources import geo

# --- clock ------------------------------------------------------------------------------------------------------


def test_replay_clock_moves_only_when_told(clock):
    assert clock.now() == T0
    assert clock.monotonic() == 0.0
    clock.advance(90)
    assert clock.now() == T0 + timedelta(seconds=90)
    assert clock.monotonic() == 90.0


def test_replay_clock_advance_to_never_goes_back(clock):
    clock.advance_to(T0 + timedelta(minutes=2))
    clock.advance_to(T0 + timedelta(minutes=1))
    assert clock.monotonic() == 120.0


def test_replay_clock_speed_scales_real_time():
    real = [0.0]
    clock = ReplayClock(T0, speed=20, real=lambda: real[0])
    real[0] = 3.0
    assert clock.monotonic() == 60.0
    with pytest.raises(ValueError):
        ReplayClock(T0, speed=0)


def test_system_clock_is_aware_utc():
    assert SystemClock().now().tzinfo is not None


# --- net --------------------------------------------------------------------------------------------------------


def test_user_agent_names_app_and_contact():
    assert user_agent() == f"earthwave/{__version__} (+https://github.com/WPTK/earthwave)"
    assert user_agent("me@example.com").endswith("; me@example.com)")


async def test_client_sends_user_agent_and_gzip():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text="ok")

    async with build_client("a@b.c", httpx.MockTransport(handler)) as client:
        await client.get("https://example.test/")
    assert seen["user-agent"].startswith("earthwave/") and "a@b.c" in seen["user-agent"]
    assert "gzip" in seen["accept-encoding"]


def _polite(clock, handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request, len(seen))

    return build_client(transport=httpx.MockTransport(wrapped), clock=clock), seen


async def test_etag_makes_the_next_request_conditional_and_a_304_serves_the_kept_body(clock):
    def handler(request, n):
        if n == 1:
            return httpx.Response(
                200, text="body", headers={"ETag": '"v1"', "Last-Modified": "Sat, 10 Oct 2026 12:00:00 GMT"}
            )
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["if-modified-since"] == "Sat, 10 Oct 2026 12:00:00 GMT"
        return httpx.Response(304)

    client, seen = _polite(clock, handler)
    async with client:
        first = await client.get("https://example.test/feed")
        second = await client.get("https://example.test/feed")
    assert (first.status_code, second.status_code, second.text) == (200, 200, "body") and len(seen) == 2


async def test_max_age_answers_from_cache_until_it_lapses(clock):
    def handler(request, n):
        return httpx.Response(200, text=f"v{n}", headers={"Cache-Control": "public, max-age=60"})

    client, seen = _polite(clock, handler)
    async with client:
        assert (await client.get("https://example.test/a")).text == "v1"
        clock.advance(30)
        assert (await client.get("https://example.test/a")).text == "v1" and len(seen) == 1
        clock.advance(31)
        assert (await client.get("https://example.test/a")).text == "v2" and len(seen) == 2


async def test_no_store_and_unvalidated_responses_are_never_kept(clock):
    def handler(request, n):
        return httpx.Response(
            200, text=f"v{n}", headers={"Cache-Control": "no-store", "ETag": '"x"'} if "s" in request.url.path else {}
        )

    client, seen = _polite(clock, handler)
    async with client:
        for path in ("/s", "/s", "/plain", "/plain"):
            await client.get(f"https://example.test{path}")
    assert len(seen) == 4 and all("if-none-match" not in r.headers for r in seen)


async def test_a_caller_supplied_validator_passes_straight_through(clock):
    def handler(request, n):
        return httpx.Response(304) if n > 1 else httpx.Response(200, text="x", headers={"ETag": '"a"'})

    client, seen = _polite(clock, handler)
    async with client:
        await client.get("https://example.test/f")
        got = await client.get("https://example.test/f", headers={"If-Modified-Since": "Sat, 10 Oct 2026 12:00:00 GMT"})
    assert got.status_code == 304 and "if-none-match" not in seen[1].headers


async def test_429_leaves_the_host_alone_for_retry_after_without_touching_others(clock):
    def handler(request, n):
        return httpx.Response(429, headers={"Retry-After": "30"}) if n == 1 else httpx.Response(200, text="ok")

    client, seen = _polite(clock, handler)
    async with client:
        assert (await client.get("https://a.test/x")).status_code == 429
        clock.advance(10)
        held = await client.get("https://a.test/y")
        assert held.status_code == 429 and held.headers["retry-after"] == "20" and len(seen) == 1
        assert (await client.get("https://b.test/x")).status_code == 200
        clock.advance(20)
        assert (await client.get("https://a.test/y")).status_code == 200


async def test_a_429_without_a_wait_backs_off_a_minute_and_a_bare_503_does_not_block(clock):
    def handler(request, n):
        return httpx.Response(429 if request.url.host == "a.test" else 503)

    client, seen = _polite(clock, handler)
    async with client:
        await client.get("https://a.test/")
        await client.get("https://b.test/")
        await client.get("https://b.test/")
        held = await client.get("https://a.test/")
    assert held.headers["retry-after"] == "60" and len(seen) == 3


def test_retry_after_of_reads_only_rate_limit_answers(clock):
    def status_error(code, headers=None):
        request = httpx.Request("GET", "https://example.test/")
        return httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(code, headers=headers, request=request)
        )

    now = clock.now()
    assert retry_after_of(status_error(429, {"Retry-After": "90"}), now) == 90.0
    assert retry_after_of(status_error(503, {"Retry-After": "7"}), now) == 7.0
    assert retry_after_of(status_error(429), now) == 60.0
    assert retry_after_of(status_error(429, {"Retry-After": "999999"}), now) == 3600.0
    assert retry_after_of(status_error(503), now) is None and retry_after_of(status_error(500), now) is None
    assert retry_after_of(httpx.ConnectError("down"), now) is None


async def test_record_then_replay_round_trip(tmp_path):
    path = tmp_path / "rec.jsonl"
    calls = iter(
        [httpx.Response(200, text="one"), httpx.Response(304), httpx.Response(200, content=b"\xff\xfe\x00bin")]
    )
    rec_clock = ReplayClock(T0, real=lambda: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        rec_clock.advance(60)
        return next(calls)

    recorder = RecordingTransport(httpx.MockTransport(handler), path, rec_clock)
    async with build_client(transport=recorder) as client:
        first = await client.get("https://example.test/feed")
        second = await client.get("https://example.test/feed", headers={"If-Modified-Since": "x"})
        third = await client.get("https://example.test/bin")
    assert (first.text, second.status_code, third.content) == ("one", 304, b"\xff\xfe\x00bin")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    assert recording_start(path) == T0 + timedelta(seconds=60)

    play_clock = ReplayClock(recording_start(path), real=lambda: 0.0)
    async with build_client(transport=ReplayTransport(path, play_clock)) as client:
        a = await client.get("https://example.test/feed")
        assert play_clock.now() == T0 + timedelta(seconds=60)
        b = await client.get("https://example.test/feed")
        assert play_clock.now() == T0 + timedelta(seconds=120)
        c = await client.get("https://example.test/bin")
        d = await client.get("https://example.test/feed")  # exhausted: the last record repeats
        missing = await client.get("https://example.test/nope")
    assert (a.text, b.status_code, c.content, d.status_code, missing.status_code) == (
        "one",
        304,
        b"\xff\xfe\x00bin",
        304,
        404,
    )


def test_recording_start_rejects_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        recording_start(path)


# --- config -----------------------------------------------------------------------------------------------------


def test_template_parses_to_the_defaults():
    config = parse_config(tomllib.loads(TEMPLATE))
    assert config == Config()
    raw = tomllib.loads(TEMPLATE)
    assert raw["poll"]["usgs_seconds"] == asdict(Config().poll)["usgs_seconds"]


def test_first_run_writes_template_then_reads_it(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "config.toml"
    monkeypatch.setattr("earthwave.config.default_config_path", lambda: path)
    assert load_config() == Config()
    assert path.read_text(encoding="utf-8") == TEMPLATE
    path.write_text(
        '[view]\nmin_magnitude = 4.5\nhours = 6.5\n[home]\nname = "Home"\nlat = 40\nlon = -74\n', encoding="utf-8"
    )
    config = load_config(path)
    assert (config.view.min_magnitude, config.view.hours) == (4.5, 6.5)
    assert config.home.enabled and config.home.lat == 40


@pytest.mark.parametrize(
    "toml",
    [
        "[poll]\nusgs_seconds = 30",
        '[view]\nglyphs = "fancy"',
        '[view]\nmin_magnitude = "big"',
        "[home]\nlat = 95",
        '[view]\nmap = "yes"',
        "poll = 3",
    ],
)
def test_invalid_values_raise(tmp_path, toml):
    with pytest.raises(ConfigError):
        parse_config(tomllib.loads(toml))


def test_a_missing_explicit_config_path_is_an_error_and_creates_nothing(tmp_path):
    path = tmp_path / "earhtwave.toml"
    with pytest.raises(ConfigError, match="not found"):
        load_config(path)
    assert not path.exists()


def test_broken_toml_names_the_file(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[view\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"c.toml"):
        load_config(path)


def test_overrides_and_home_parsing():
    config = with_overrides(Config(), 4.0, 48, parse_home("40.7,-74"), "ascii")
    assert (config.view.min_magnitude, config.view.hours, config.view.glyphs) == (4.0, 48, "ascii")
    assert config.home == HomeConfig("home", 40.7, -74.0, 500)
    for bad in ("1", "a,b", "100,0"):
        with pytest.raises(ConfigError):
            parse_home(bad)


def test_logging_writes_only_when_asked(tmp_path):
    assert setup_logging(None, False) is None
    path = setup_logging(tmp_path / "log" / "e.log", debug=True)
    import logging

    logging.getLogger("earthwave.test").debug("hello")
    for handler in logging.getLogger("earthwave").handlers:
        handler.flush()
    assert "hello" in path.read_text(encoding="utf-8")
    setup_logging(None, False)


# --- geo --------------------------------------------------------------------------------------------------------


def test_haversine_known_distances():
    assert geo.haversine_km(0, 0, 0, 0) == 0
    assert geo.haversine_km(51.5, -0.1278, 48.8566, 2.3522) == pytest.approx(343.6, abs=1.5)  # London - Paris
    assert geo.haversine_km(0, 0, 0, 180) == pytest.approx(20015, abs=5)


def test_haversine_crosses_the_dateline():
    assert geo.haversine_km(0, 179.5, 0, -179.5) == pytest.approx(111.2, abs=0.5)
    assert geo.lon_delta(179.5, -179.5) == pytest.approx(1.0)
    assert geo.wrap_lon(190) == pytest.approx(-170) and geo.wrap_lon(-180) == -180


@pytest.mark.parametrize(
    ("target", "bearing", "name"),
    [((1, 0), 0, "N"), ((0, 1), 90, "E"), ((-1, 0), 180, "S"), ((0, -1), 270, "W"), ((1, 1), 45, "NE")],
)
def test_bearing_and_compass(target, bearing, name):
    got = geo.bearing_deg(0, 0, *target)
    assert got == pytest.approx(bearing, abs=0.6)
    assert geo.compass(got) == name


def test_compass_wraps():
    assert geo.compass(359) == "N"
    assert geo.compass(-20) == "NNW"


def test_nearest_n_with_radius():
    class P:
        def __init__(self, lat, lon):
            self.lat, self.lon = lat, lon

    points = [P(0, 5), P(0, 1), P(0, 50)]
    near = geo.nearest_n(points, 0, 0, 2, max_km=1000)
    assert [p.lon for _, p in near] == [1, 5]
    assert geo.distance_text(0, 0, 0, 1).endswith("km E")


# --- command line -----------------------------------------------------------------------------------------------


def test_cli_rejects_bad_arguments(tmp_path, capsys):
    from earthwave.__main__ import main

    assert main(["--no-config", "--record", "a.jsonl", "--replay", "b.jsonl"]) == 2
    assert main(["--no-config", "--home", "nonsense"]) == 2
    assert main(["--no-config", "--replay", str(tmp_path / "missing.jsonl")]) == 2
    bad = tmp_path / "c.toml"
    bad.write_text("[poll]\nusgs_seconds = 5\n", encoding="utf-8")
    assert main(["--config", str(bad)]) == 2
    assert "usgs_seconds" in capsys.readouterr().err


def test_cli_validates_ranges_and_unreadable_config(tmp_path, capsys):
    from earthwave.__main__ import main

    assert main(["--no-config", "--hours", "0"]) == 2
    assert "--hours" in capsys.readouterr().err
    assert main(["--no-config", "--min-mag", "99"]) == 2
    assert "--min-mag" in capsys.readouterr().err
    binary = tmp_path / "bin.toml"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert main(["--config", str(binary)]) == 2
    assert main(["--config", str(tmp_path)]) == 2  # a directory is an OSError, not a traceback


def test_home_at_null_island_counts_as_unset():
    from earthwave.config import HomeConfig

    assert not HomeConfig("Home", 0.0, 0.0).enabled and HomeConfig("Home", 1.0, 0.0).enabled


@pytest.mark.parametrize(
    "toml",
    [
        "[poll]\ntsunami_seconds = inf",
        "[poll]\ndart_seconds = nan",
        "[poll]\ntsunami_seconds = 86400",
        "[home]\nlat = nan",
        "[home]\nradius_km = inf",
        "[alerts]\ngauge_anomaly_dart_m = 0",
        "[alerts]\ngauge_anomaly_gauge_m = -1.0",
        "[alerts]\ngauge_anomaly_gauge_m = nan",
        "[alerts]\ngauge_anomaly_dart_m = 1e9",
        '[notify]\ncontact = "a\\u000ab"',
        "[alerts]\nmajor_magnitde = 5.0",
        "[home]\nradius = 200",
        "[extra]\nx = 1",
        '[view]\n__init__ = "x"',
    ],
)
def test_non_finite_huge_and_unknown_config_values_raise(toml):
    with pytest.raises(ConfigError):
        parse_config(tomllib.loads(toml))


def test_home_accepts_a_negative_latitude_without_equals(monkeypatch):
    from earthwave import __main__ as cli

    seen = {}
    monkeypatch.setattr(cli, "run", lambda args: seen.update(home=args.home) or 0)
    assert cli.main(["--home", "-33.87,151.21", "--no-config"]) == 0
    assert seen["home"] == "-33.87,151.21"
    assert parse_home(seen["home"]) == (-33.87, 151.21)


def test_a_bad_log_path_is_a_clean_message(tmp_path, capsys):
    from earthwave import __main__ as cli

    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert cli.main(["--no-config", "--log", str(blocker / "e.log")]) == 2
    assert "cannot open the log file" in capsys.readouterr().err


def test_a_crashed_app_exits_non_zero(monkeypatch):
    from earthwave import __main__ as cli

    class Crashed:
        return_code = 1

        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            pass

    monkeypatch.setattr(cli, "EarthwaveApp", Crashed)
    monkeypatch.setattr(cli, "open_history", lambda config, clock: (None, None))
    assert cli.main(["--no-config"]) == 1


def test_disabling_logging_resets_a_debug_level():
    import logging

    setup_logging(None, True)
    setup_logging(None, False)
    assert logging.getLogger("earthwave").level == logging.WARNING
