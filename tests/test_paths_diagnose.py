"""Relocatable config and data dirs (#230), `--diagnose` (#235), `--selftest` in the binary (#259), upgrade from 0.1.0 (#239)."""

import asyncio
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from earthwave import paths
from earthwave.__main__ import main, selftest
from earthwave.clock import ReplayClock
from earthwave.config import default_config_path, load_config
from earthwave.diagnose import FEEDS, redact, report
from earthwave.logging_setup import default_log_path
from earthwave.state.history import History, default_path

UPGRADE = Path(__file__).parent / "fixtures" / "upgrade" / "v0.1.0"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (paths.CONFIG_DIR_ENV, paths.DATA_DIR_ENV):
        monkeypatch.delenv(name, raising=False)


def test_env_vars_relocate_config_history_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    assert default_config_path() == tmp_path / "cfg" / "config.toml"
    assert default_path() == tmp_path / "data" / "history.sqlite"
    assert default_log_path() == tmp_path / "data" / "logs" / "earthwave.log"


def test_blank_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, "  ")
    assert paths.config_dir() != Path()


def test_windows_defaults_follow_localappdata(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.config_dir() == tmp_path / "earthwave"
    assert paths.data_dir() == tmp_path / "earthwave"
    assert paths.log_dir() == tmp_path / "earthwave" / "Logs"


def test_first_run_in_an_isolated_dir_writes_the_template_there(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    load_config()
    assert (tmp_path / "cfg" / "config.toml").read_text(encoding="utf-8").lstrip().startswith("#")


def test_help_documents_the_env_vars(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert paths.CONFIG_DIR_ENV in out and paths.DATA_DIR_ENV in out and "--diagnose" in out


def test_diagnose_reports_paths_and_feeds_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if "jma.go.jp" in request.url.host else 200)

    text = report(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert "earthwave  " in text and "TERM=xterm-256color" in text
    assert "from EARTHWAVE_DATA_DIR" in text
    assert "hunter2" not in text and "SECRET_TOKEN" not in text
    assert text.count("\nfeed ") == len(FEEDS)
    assert "JMA" in text and "HTTP 503" in text and "HTTP 200" in text


def test_diagnose_reports_a_feed_failure_as_text(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    text = report(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert text.count("FAILED: ConnectError") == len(FEEDS)


def test_diagnose_offline_makes_no_requests(capsys):
    assert main(["--diagnose", "--offline"]) == 0
    assert "not checked (--offline)" in capsys.readouterr().out


def test_redact_masks_the_home_directory():
    assert redact(str(Path.home() / "x" / "config.toml")).startswith("~")


def test_the_spec_bundles_every_fixture_family_selftest_reads():
    spec = (ROOT / "earthwave.spec").read_text(encoding="utf-8")
    for family in ("usgs", "tsunamigov", "ndbc", "coops", "jma"):
        assert f"tests/fixtures/{family}" in spec, family


def test_selftest_finds_fixtures_under_the_unpack_dir_of_a_frozen_build(tmp_path, monkeypatch, capsys):
    shutil.copytree(ROOT / "tests" / "fixtures", tmp_path / "tests" / "fixtures")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    import importlib

    import earthwave.__main__ as cli

    try:
        importlib.reload(cli)
        assert tmp_path / "tests" / "fixtures" == cli.FIXTURES
        assert cli.selftest() == 0
    finally:
        monkeypatch.undo()
        importlib.reload(cli)
    out = capsys.readouterr().out
    assert "SKIPPED" not in out and "selftest: ok" in out


def test_selftest_skip_line_says_where_it_looked(tmp_path, capsys):
    assert selftest(tmp_path / "missing") == 0
    assert str(tmp_path / "missing") in capsys.readouterr().out


def test_state_written_by_0_1_0_survives_the_upgrade(tmp_path, monkeypatch):
    """The config and history.sqlite fixtures were written by the v0.1.0 code (tag v0.1.0)."""
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "data"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy(UPGRADE / "config.toml", default_config_path())
    shutil.copy(UPGRADE / "history.sqlite", default_path())

    config = load_config()
    assert config.view.min_magnitude == 4.0  # the user's edit, not the default

    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    history = History(default_path(), config.history.retain_days, ReplayClock(now, real=lambda: 0.0))
    history.open()
    quakes, unseen = history.load_since(datetime(2026, 11, 1, tzinfo=UTC))
    asyncio.run(history.close())
    assert {q.id for q in quakes} == {"us0001", "us0002"}
    assert unseen == set()  # acknowledgements survive too
    assert default_config_path().read_text(encoding="utf-8") == (UPGRADE / "config.toml").read_text(encoding="utf-8")


def test_diagnose_survives_an_empty_environment(monkeypatch):
    for name in ("TERM", "COLORTERM", "TERM_PROGRAM", "WT_SESSION", "NO_COLOR", "TMUX", "LANG"):
        monkeypatch.delenv(name, raising=False)
    assert "TERM=(unset)" in report(offline=True)


def test_redact_matches_case_insensitively_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: Path("C:/Users/Zed"))
    monkeypatch.setattr(os.path, "normcase", str.lower)  # what ntpath.normcase does to case
    assert redact(str(Path("C:/Users/Zed")).replace("Zed", "ZED") + "-x") == "~-x"


def test_redact_survives_a_missing_home(monkeypatch):
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    assert redact("/somewhere/config.toml") == "/somewhere/config.toml"


def test_the_terminal_line_is_redacted(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", str(Path.home() / "bin" / "term"))
    assert str(Path.home()) not in report(offline=True).split("terminal")[1].split("\n")[0]


def test_diagnose_reports_instead_of_crashing_on_a_bad_proxy(monkeypatch):
    def broken():
        raise ValueError("Unknown scheme for proxy URL")

    assert "not checked: ValueError" in report(client_factory=broken)
