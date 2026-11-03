"""#251 --save-home keeps the home in the config file; #252 --help names the default log path and --debug --selftest logs."""

import subprocess
import sys

import pytest

from earthwave import paths
from earthwave.__main__ import main
from earthwave.config import TEMPLATE, ConfigError, load_config, save_home


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    return tmp_path


def test_save_home_edits_the_values_and_keeps_the_rest_of_the_file(dirs):
    path = dirs / "c.toml"
    path.write_text(TEMPLATE.replace("radius_km = 500", "radius_km = 250  # mine"), encoding="utf-8")
    save_home(path, "New York", 40.7, -74.0)
    text = path.read_text(encoding="utf-8")
    assert "radius_km = 250  # mine" in text and text.count("[home]") == 1
    home = load_config(path).home
    assert (home.name, home.lat, home.lon, home.radius_km) == ("New York", 40.7, -74.0, 250)
    assert len(text.splitlines()) == len(TEMPLATE.replace("radius_km = 500", "radius_km = 250  # mine").splitlines())


def test_save_home_adds_a_missing_section(dirs):
    path = dirs / "c.toml"
    path.write_text("[view]\nhours = 48\n", encoding="utf-8")
    save_home(path, "Oslo", 59.9, 10.7)
    config = load_config(path)
    assert config.view.hours == 48 and (config.home.name, config.home.lat) == ("Oslo", 59.9)


def test_cli_save_home_writes_the_default_config_and_exits(dirs, capsys):
    assert main(["--home", "40.7,-74.0", "--home-name", "New York", "--save-home"]) == 0
    assert "saved" in capsys.readouterr().out
    home = load_config().home
    assert (home.name, home.lat, home.lon) == ("New York", 40.7, -74.0)


def test_save_home_needs_a_home(dirs, capsys):
    assert main(["--save-home"]) == 2
    assert "--home" in capsys.readouterr().err


def test_save_home_never_clobbers_a_broken_config(dirs, capsys):
    path = dirs / "cfg" / "config.toml"
    path.parent.mkdir()
    path.write_text("[view\n", encoding="utf-8")
    assert main(["--home", "1,2", "--save-home"]) == 2
    assert path.read_text(encoding="utf-8") == "[view\n"


def test_help_names_the_default_log_path(dirs, capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "earthwave.log" in out


def test_debug_selftest_writes_a_log(dirs):
    done = subprocess.run(
        [sys.executable, "-m", "earthwave", "--debug", "--selftest"], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    log = dirs / "data" / "logs" / "earthwave.log"
    assert log.is_file() and "selftest" in log.read_text(encoding="utf-8")
    assert "earthwave.log" in done.stdout


def _save(tmp_path, text, name="Oslo", lat=59.9, lon=10.7, newline=None):
    path = tmp_path / "c.toml"
    path.write_bytes(text.encode("utf-8")) if newline is None else path.write_bytes(
        text.replace("\n", newline).encode()
    )
    save_home(path, name, lat, lon)
    return path


def test_save_home_fills_a_partial_table_and_keeps_comments(dirs):
    path = _save(dirs, '[home]\nname = "a # b"  # mine\nradius_km = 100\n\n[view]\nhours = 48\n')
    text = path.read_text(encoding="utf-8")
    assert 'name = "Oslo"  # mine' in text and "a # b" not in text
    assert text.index("lat = 59.9") < text.index("[view]")
    home = load_config(path).home
    assert (home.name, home.lat, home.lon, home.radius_km) == ("Oslo", 59.9, 10.7, 100)


def test_save_home_value_with_hash_and_a_trailing_comment(dirs):
    path = _save(dirs, '[home]\nname = "x"\nlat = 1.0   # lat\nlon = 2\n')
    text = path.read_text(encoding="utf-8")
    assert "lat = 59.9   # lat" in text and "lon = 10.7" in text


def test_save_home_last_table_without_a_newline(dirs):
    path = _save(dirs, '[view]\nhours = 48\n\n[home]\nname = "x"')
    home = load_config(path).home
    assert (home.name, home.lat, home.lon) == ("Oslo", 59.9, 10.7)
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize(
    "text",
    [
        'home = {name = "x", lat = 1, lon = 2}\n',
        '["home"]\nname = "x"\n',
        'home.name = "x"\nhome.lat = 1\nhome.lon = 2\n',
    ],
)
def test_save_home_refuses_a_home_it_cannot_edit_and_leaves_the_file(dirs, text):
    path = dirs / "c.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="by hand"):
        save_home(path, "Oslo", 59.9, 10.7)
    assert path.read_text(encoding="utf-8") == text
    assert not list(dirs.glob("c.toml.*"))


def test_save_home_keeps_crlf_and_bom(dirs):
    path = dirs / "c.toml"
    path.write_bytes(b'\xef\xbb\xbf[view]\r\nhours = 48\r\n\r\n[home]\r\nname = "x"\r\n')
    save_home(path, "Oslo", 59.9, 10.7)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert load_config(path).home.lat == 59.9


def test_save_home_write_failure_leaves_the_original(dirs, monkeypatch):
    path = dirs / "c.toml"
    path.write_text("[view]\nhours = 48\n", encoding="utf-8")

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr("earthwave.config.os.replace", boom)
    with pytest.raises(ConfigError, match="disk full"):
        save_home(path, "Oslo", 59.9, 10.7)
    assert path.read_text(encoding="utf-8") == "[view]\nhours = 48\n"
    assert [p.name for p in dirs.glob("c.toml*")] == ["c.toml"]


def test_a_bad_home_does_not_create_the_config(dirs, capsys):
    assert main(["--home", "95,0", "--save-home"]) == 2
    err = capsys.readouterr().err
    assert "--home" in err and "[home]" in err
    assert not (dirs / "cfg").exists()


@pytest.mark.parametrize(
    "extra",
    [["--no-config"], ["--min-mag", "5"], ["--hours", "5"], ["--glyphs", "ascii"], ["--selftest"], ["--offline"]],
)
def test_save_home_rejects_flags_it_would_ignore(dirs, capsys, extra):
    assert main(["--home", "1,2", "--save-home", *extra]) == 2
    assert extra[0] in capsys.readouterr().err
    assert not (dirs / "cfg").exists()


def test_home_name_needs_home(dirs, capsys):
    assert main(["--home-name", "x"]) == 2
    assert "--home-name" in capsys.readouterr().err


def test_log_alone_selftest_lines_reach_the_file(dirs):
    log = dirs / "my.log"
    done = subprocess.run(
        [sys.executable, "-m", "earthwave", "--log", str(log), "--selftest"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "selftest: exit 0" in log.read_text(encoding="utf-8")
