"""TOML configuration: defaults, validation, platformdirs paths and the commented first-run template."""

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from earthwave.paths import config_dir

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PollConfig:
    usgs_seconds: int = 60
    tsunami_seconds: int = 60
    dart_seconds: int = 900
    coops_seconds: int = 300
    alert_gauge_seconds: int = 60


@dataclass(frozen=True, slots=True)
class ViewConfig:
    min_magnitude: float = 2.5
    hours: float = 24
    glyphs: str = "auto"
    map: bool = True
    map_renderer: str = "auto"
    time_zone: str = "local"


@dataclass(frozen=True, slots=True)
class HomeConfig:
    name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    radius_km: float = 500

    @property
    def enabled(self) -> bool:
        """A name plus real coordinates; lat and lon both 0 is the unset default, so it counts as no home."""
        return bool(self.name) and (self.lat, self.lon) != (0.0, 0.0)


@dataclass(frozen=True, slots=True)
class AlertsConfig:
    major_magnitude: float = 6.0
    near_home_magnitude: float = 3.0
    pager_min: str = "yellow"
    tsunami: bool = True
    gauge_anomaly_dart_m: float = 0.05
    gauge_anomaly_gauge_m: float = 0.3


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    desktop: bool = True
    bell: bool = True
    contact: str = ""


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    usgs: bool = True
    tsunamigov: bool = True
    nws: bool = True
    jma: bool = True


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    retain_days: int = 90


@dataclass(frozen=True, slots=True)
class Config:
    poll: PollConfig = field(default_factory=PollConfig)
    view: ViewConfig = field(default_factory=ViewConfig)
    home: HomeConfig = field(default_factory=HomeConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)


CHOICES: dict[tuple[str, str], tuple[str, ...]] = {
    ("view", "glyphs"): ("auto", "unicode", "ascii"),
    ("view", "map_renderer"): ("auto", "braille", "sixel"),
    ("view", "time_zone"): ("local", "UTC"),
    ("alerts", "pager_min"): ("green", "yellow", "orange", "red"),
}
MAX_VIEW_HOURS = 24 * 35  # the store keeps 35 days of quakes, so no view reaches further back

# (minimum, maximum); None means unbounded. USGS is never polled faster than 60 s, and no feed is polled so rarely
# that the status bar would call it fresh for hours.
RANGES: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("poll", "usgs_seconds"): (60, 3600),
    ("poll", "tsunami_seconds"): (30, 600),
    ("poll", "dart_seconds"): (60, 3600),
    ("poll", "coops_seconds"): (60, 3600),
    ("poll", "alert_gauge_seconds"): (30, 600),
    ("view", "min_magnitude"): (-2, 10),
    ("view", "hours"): (1, MAX_VIEW_HOURS),
    ("home", "lat"): (-90, 90),
    ("home", "lon"): (-180, 180),
    ("home", "radius_km"): (1, 20_000),
    ("alerts", "major_magnitude"): (0, 10),
    ("alerts", "near_home_magnitude"): (-2, 10),
    ("alerts", "gauge_anomaly_dart_m"): (0.001, 10),
    ("alerts", "gauge_anomaly_gauge_m"): (0.001, 10),
    ("history", "retain_days"): (1, 3650),
}

TEMPLATE = """\
# earthwave configuration. Edit and restart; the app only reads this file.

[poll]
usgs_seconds = 60            # USGS feeds refresh once a minute; values below 60 are rejected
tsunami_seconds = 60
dart_seconds = 900
coops_seconds = 300
alert_gauge_seconds = 60

[view]
min_magnitude = 2.5          # hide quakes below this magnitude
hours = 24                   # show quakes from the last N hours
glyphs = "auto"              # auto | unicode | ascii
map = true
map_renderer = "auto"       # auto | braille | sixel; auto uses sixel pixels only where the terminal reports them
time_zone = "local"          # local | UTC

[home]
name = ""                    # empty = no home; the distance column is hidden
lat = 0.0
lon = 0.0
radius_km = 500

[alerts]
major_magnitude = 6.0
near_home_magnitude = 3.0
pager_min = "yellow"         # green | yellow | orange | red
tsunami = true
gauge_anomaly_dart_m = 0.05
gauge_anomaly_gauge_m = 0.3

[notify]
desktop = true
bell = true
contact = ""                 # appended to the User-Agent sent to NOAA/NWS

[sources]
usgs = true
tsunamigov = true
nws = true
jma = true

[history]
retain_days = 90
"""


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    where = f"[{section}] {key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false, got {value!r}")
        return value
    if isinstance(default, int | float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} must be a number, got {value!r}")
        if not math.isfinite(value):
            raise ConfigError(f"{where} must be a finite number, got {value!r}")
        low, high = RANGES.get((section, key), (None, None))
        if (low is not None and value < low) or (high is not None and value > high):
            raise ConfigError(f"{where} must be between {low} and {high}, got {value!r}")
        return int(value) if isinstance(default, int) and float(value).is_integer() else value
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be text, got {value!r}")
    if not value.isprintable():
        raise ConfigError(f"{where} must not contain control characters, got {value!r}")
    if (section, key) == ("notify", "contact") and not value.isascii():
        raise ConfigError(f"{where} must be plain ASCII (it goes into an HTTP header), got {value!r}")
    choices = CHOICES.get((section, key))
    if choices and value not in choices:
        raise ConfigError(f"{where} must be one of {', '.join(choices)}, got {value!r}")
    return value


def parse_config(data: dict[str, Any]) -> Config:
    """Build a Config from parsed TOML; unknown keys and bad values raise ConfigError (a typo must not pass quietly).
    Every problem is collected, so one run shows them all, each naming its [section] key."""
    sections: dict[str, Any] = {}
    problems: list[str] = []
    for fld in dataclasses.fields(Config):
        cls = type(getattr(Config(), fld.name))
        raw = data.get(fld.name, {})
        if not isinstance(raw, dict):
            problems.append(f"[{fld.name}] must be a table")
            continue
        defaults = cls()
        known = {f.name for f in dataclasses.fields(cls)}
        values = {}
        for key, value in raw.items():
            if key not in known:
                problems.append(f"unknown key [{fld.name}] {key}")
                continue
            try:
                values[key] = _coerce(fld.name, key, value, getattr(defaults, key))
            except ConfigError as exc:
                problems.append(str(exc))
        sections[fld.name] = cls(**values)
    problems += [f"unknown section [{key}]" for key in data if key not in _SECTION_NAMES]
    if problems:
        raise ConfigError(_join(problems))
    return Config(**sections)


_SECTION_NAMES = {f.name for f in dataclasses.fields(Config)}
_SYNTAX_AT = re.compile(r"\(at line (\d+), column \d+\)")
_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$")
_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_MULTILINE_OPENERS = ("[", "{", '"""', "'''")


def _join(problems: list[str]) -> str:
    return problems[0] if len(problems) == 1 else f"{len(problems)} problems:\n  " + "\n  ".join(problems)


def _syntax_problems(text: str, exc: tomllib.TOMLDecodeError) -> list[str]:
    """TOML parsing stops at the first error. Each `key = value` line is therefore tried on its own, which finds the
    other bad values and names their keys; failing that, the parser's own position and the line it points at."""
    lines = text.splitlines()
    section, found = "", []
    for number, line in enumerate(lines, start=1):
        if header := _HEADER.match(line):
            section = header.group(1)
            continue
        match = _KEY_LINE.match(line)
        if not match or match.group(2).startswith(_MULTILINE_OPENERS):
            continue  # multi-line values cannot be judged one line at a time
        try:
            tomllib.loads(line)
        except tomllib.TOMLDecodeError as inner:
            where = f"[{section}] {match.group(1)}" if section else match.group(1)
            found.append(f"line {number}: {where}: {str(inner).split(' (at')[0].lower()}: {match.group(2)!r}")
    if found:
        return found
    at = _SYNTAX_AT.search(str(exc))
    if not at:
        return [str(exc)]
    number = int(at.group(1))
    shown = f": {lines[number - 1].strip()!r}" if 0 < number <= len(lines) else ""
    return [f"line {number}: {str(exc).split(' (at')[0].lower()}{shown}"]


def load_config(path: Path | None = None) -> Config:
    """Read the config file. The default path gets the commented template written first if it does not exist;
    an explicit path that does not exist is an error, so a mistyped --config never runs on defaults."""
    if path is not None and not path.exists():
        raise ConfigError(f"{path}: config file not found")
    path = path or default_config_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEMPLATE, encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"{path}: cannot write the first-run template: {exc}") from exc
        log.info("config: wrote first-run template to %s", path)
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {_join(_syntax_problems(text, exc))}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return parse_config(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def parse_home(text: str, field: str = "--home") -> tuple[float, float]:
    """LAT,LON from `text`; `field` names what the person typed it into (a flag, or a dialog's field) in the errors."""
    try:
        lat_s, lon_s = text.split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError as exc:
        raise ConfigError(f"{field} expects LAT,LON (the config's [home] lat and lon), got {text!r}") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ConfigError(f"{field} out of range (the config's [home] lat is -90 to 90, lon -180 to 180): {text!r}")
    return lat, lon


def with_overrides(
    config: Config,
    min_magnitude: float | None = None,
    hours: float | None = None,
    home: tuple[float, float] | None = None,
    glyphs: str | None = None,
    home_name: str | None = None,
) -> Config:
    """Apply command-line overrides; --home sets a coordinate pair and names it "home" if the config has no name."""
    view = config.view
    for flag, key, value in (("--min-mag", "min_magnitude", min_magnitude), ("--hours", "hours", hours)):
        low, high = RANGES[("view", key)]
        if value is not None and not (low <= value <= high):
            raise ConfigError(f"{flag} must be between {low} and {high}, got {value!r}")
    if min_magnitude is not None:
        view = dataclasses.replace(view, min_magnitude=min_magnitude)
    if hours is not None:
        view = dataclasses.replace(view, hours=hours)
    if glyphs is not None:
        view = dataclasses.replace(view, glyphs=glyphs)
    home_cfg = config.home
    if home is not None:
        home_cfg = dataclasses.replace(home_cfg, name=home_name or home_cfg.name or "home", lat=home[0], lon=home[1])
    return dataclasses.replace(config, view=view, home=home_cfg)


_HOME_HEADER = re.compile(r"^\s*\[\s*home\s*\]\s*(#.*)?$")
_TABLE_START = re.compile(r"^\s*\[")
_HOME_KEY = re.compile(r"^(\s*)(name|lat|lon)(\s*=\s*)(.*)$")


def _value_end(rest: str) -> int:
    """Where the value at the start of `rest` ends: after its closing quote for a string (a `#` inside is text), else
    before the whitespace that leads a comment, else at the end."""
    if rest[:1] in ('"', "'"):
        quote, i = rest[0], 1
        while i < len(rest):
            if quote == '"' and rest[i] == "\\":
                i += 2
            elif rest[i] == quote:
                return i + 1
            else:
                i += 1
        return len(rest)
    comment = re.search(r"\s*#", rest)
    return comment.start() if comment else len(rest.rstrip())


def save_home(path: Path, name: str, lat: float, lon: float) -> None:
    """Write name, lat and lon into the [home] table of the config file, changing nothing else (comments, other keys,
    other sections, the newline style and a BOM stay as they are). A missing table or key is added. The new text is
    built and parsed in memory first and then swapped in with a temp file and os.replace, so on any failure the
    original is untouched. A [home] written as an inline table, dotted keys or a quoted header is refused."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    bom = text.startswith("﻿")
    text = text.removeprefix("﻿")
    newline = "\r\n" if "\r\n" in text else "\n"
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    lines = re.split(r"\r?\n", text)
    start = next((i for i, line in enumerate(lines) if _HOME_HEADER.match(line)), None)
    if start is None and "home" in before:
        raise ConfigError(
            f"{path}: [home] is written as an inline table, dotted keys or a quoted header, which --save-home does "
            "not edit; change name, lat and lon there by hand"
        )
    values = {"name": json.dumps(name, ensure_ascii=False), "lat": repr(float(lat)), "lon": repr(float(lon))}
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += (["", "[home]"] if lines else ["[home]"]) + [f"{key} = {value}" for key, value in values.items()]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _TABLE_START.match(lines[i])), len(lines))
        pending = dict(values)
        for i in range(start + 1, end):
            match = _HOME_KEY.match(lines[i])
            if not match or match.group(2) not in pending:
                continue
            rest = match.group(4)
            key = match.group(2)
            if rest.startswith(('"""', "'''")):
                raise ConfigError(f"{path}: [home] {key} is a multi-line string; change it by hand")
            lines[i] = f"{match.group(1)}{key}{match.group(3)}{pending.pop(key)}{rest[_value_end(rest) :]}"
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1  # before the blank lines that separate the next table
        lines[insert:insert] = [f"{key} = {value}" for key, value in pending.items()]
    if lines[-1].strip():
        lines.append("")
    new_text = newline.join(lines)
    try:
        after = tomllib.loads(new_text).get("home", {})
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{path}: saving the home would leave an invalid file ({exc}); change [home] by hand"
        ) from exc
    if (after.get("name"), after.get("lat"), after.get("lon")) != (name, float(lat), float(lon)):
        raise ConfigError(f"{path}: could not set [home] cleanly; change it by hand")
    payload = (("﻿" if bom else "") + new_text).encode("utf-8")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with contextlib.suppress(OSError):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise ConfigError(f"{path}: cannot save the home: {exc}") from exc
