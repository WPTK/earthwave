"""`earthwave --diagnose`: one block to paste into a bug report. Nothing secret goes in it.

Redaction: file paths show the home directory as ``~``, the user name is masked wherever it appears, the config file's
contents are never read, and of the environment only the presence and value of a few terminal variables are shown.
"""

import getpass
import os
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from earthwave import __version__
from earthwave.config import default_config_path
from earthwave.logging_setup import default_log_path
from earthwave.paths import CONFIG_DIR_ENV, DATA_DIR_ENV
from earthwave.state.history import default_path as default_history_path

TERMINAL_VARS = ("TERM", "COLORTERM", "TERM_PROGRAM", "WT_SESSION", "NO_COLOR", "TMUX", "LANG")
# One cheap request per feed host: the same hosts the app polls.
FEEDS = (
    ("USGS", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"),
    ("tsunami.gov", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"),
    ("NWS", "https://api.weather.gov/alerts/active/count"),
    ("NDBC", "https://www.ndbc.noaa.gov/data/realtime2/46402.dart"),
    ("CO-OPS", "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=waterlevels&limit=1"),
    ("JMA", "https://www.jma.go.jp/bosai/tsunami/data/list.json"),
)
HEADERS = {"User-Agent": f"earthwave/{__version__} (--diagnose)"}


def _fold(text: str) -> str:
    """Case-fold for matching where the file system does (Windows paths differ only in case)."""
    return os.path.normcase(text) if sys.platform == "win32" else text


def redact(text: str) -> str:
    """Home directory to ``~`` and the account name masked."""
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):  # no home directory can be determined
        home = ""
    if home:
        at = _fold(text).find(_fold(home))
        while at != -1:
            text = text[:at] + "~" + text[at + len(home) :]
            at = _fold(text).find(_fold(home))
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    if len(user) >= 3:
        text = text.replace(user, "<user>")
    return text


def check_feed(client: httpx.Client, url: str) -> str:
    start = time.monotonic()
    try:
        with client.stream("GET", url) as response:
            ms = round((time.monotonic() - start) * 1000)
            return f"HTTP {response.status_code} in {ms} ms" if response.is_success else f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return f"FAILED: {type(exc).__name__}"


def _path_line(label: str, path: Path, env: str | None = None) -> str:
    note = f" (from {env})" if env and os.environ.get(env, "").strip() else ""
    state = "exists" if path.exists() else "not created yet"
    return f"{label:<10}{redact(str(path))}  [{state}]{note}"


def report(offline: bool = False, client_factory: Callable[[], httpx.Client] | None = None) -> str:
    lines = [
        f"earthwave  {__version__}",
        f"python    {platform.python_version()} ({'frozen binary' if getattr(sys, 'frozen', False) else 'pip/source'})",
        f"os        {platform.system()} {platform.release()} {platform.machine()}",
        "terminal  " + redact(", ".join(f"{name}={os.environ.get(name, '(unset)')}" for name in TERMINAL_VARS)),
        _path_line("config", default_config_path(), CONFIG_DIR_ENV),
        _path_line("history", default_history_path(), DATA_DIR_ENV),
        _path_line("log", default_log_path(), DATA_DIR_ENV),
    ]
    if offline:
        lines.append("feeds     not checked (--offline)")
    else:
        make = client_factory or (lambda: httpx.Client(timeout=5.0, headers=HEADERS, follow_redirects=True))
        try:
            with make() as client:
                lines.extend(f"feed      {name:<12}{check_feed(client, url)}" for name, url in FEEDS)
        except (httpx.InvalidURL, httpx.HTTPError, ValueError, OSError) as exc:  # e.g. a malformed proxy variable
            lines.append(f"feeds     not checked: {type(exc).__name__}")
    return "\n".join(lines)
