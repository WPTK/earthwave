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
from earthwave.net import RecordingTransport, ReplayTransport, build_client, recording_start, user_agent
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


def test_first_run_writes_template_then_reads_it(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    assert load_config(path) == Config()
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
    assert cli.main(["--no-config"]) == 1
