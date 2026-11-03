"""Where earthwave keeps its files. One place, so --help, --diagnose and the code cannot disagree.

EARTHWAVE_CONFIG_DIR and EARTHWAVE_DATA_DIR relocate the config file and the history database (and, unless --log says
otherwise, the log file, in a ``logs`` folder under the data dir). Without them the platform user directories apply; on
Windows those come from %LOCALAPPDATA% when it is set (platformdirs asks the shell instead, so a test could not move
them), and from the shell's known folder otherwise.
"""

import os
import sys
from pathlib import Path

import platformdirs

APP = "earthwave"
CONFIG_DIR_ENV = "EARTHWAVE_CONFIG_DIR"
DATA_DIR_ENV = "EARTHWAVE_DATA_DIR"


def _override(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _windows_local() -> Path | None:
    base = os.environ.get("LOCALAPPDATA", "").strip() if sys.platform == "win32" else ""
    return Path(base) / APP if base else None


def config_dir() -> Path:
    return (
        _override(CONFIG_DIR_ENV)
        or _windows_local()
        or Path(platformdirs.user_config_dir(APP, appauthor=False, roaming=False))
    )


def data_dir() -> Path:
    return (
        _override(DATA_DIR_ENV)
        or _windows_local()
        or Path(platformdirs.user_data_dir(APP, appauthor=False, roaming=False))
    )


def log_dir() -> Path:
    if override := _override(DATA_DIR_ENV):
        return override / "logs"
    if local := _windows_local():
        return local / "Logs"
    return Path(platformdirs.user_log_dir(APP, appauthor=False))
