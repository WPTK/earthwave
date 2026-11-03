"""#251 --save-home keeps the home in the config file; #252 --help names the default log path and --debug --selftest logs."""

import subprocess
import sys

import pytest

from earthwave import paths
from earthwave.__main__ import main
from earthwave.config import TEMPLATE, load_config, save_home


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
