"""TOML configuration: defaults, validation, platformdirs paths and the commented first-run template."""

import dataclasses
import logging
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

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
    ("view", "time_zone"): ("local", "UTC"),
    ("alerts", "pager_min"): ("green", "yellow", "orange", "red"),
}
# (minimum, maximum); None means unbounded. USGS is never polled faster than 60 s, and no feed is polled so rarely
# that the status bar would call it fresh for hours.
RANGES: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ("poll", "usgs_seconds"): (60, 3600),
    ("poll", "tsunami_seconds"): (30, 600),
    ("poll", "dart_seconds"): (60, 3600),
    ("poll", "coops_seconds"): (60, 3600),
    ("poll", "alert_gauge_seconds"): (30, 600),
    ("view", "min_magnitude"): (-2, 10),
    ("view", "hours"): (1, 24 * 365),
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
    return Path(platformdirs.user_config_dir("earthwave", appauthor=False, roaming=False)) / "config.toml"


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
    choices = CHOICES.get((section, key))
    if choices and value not in choices:
        raise ConfigError(f"{where} must be one of {', '.join(choices)}, got {value!r}")
    return value


def parse_config(data: dict[str, Any]) -> Config:
    """Build a Config from parsed TOML; unknown keys and bad values raise ConfigError (a typo must not pass quietly)."""
    sections: dict[str, Any] = {}
    for fld in dataclasses.fields(Config):
        cls = type(getattr(Config(), fld.name))
        raw = data.get(fld.name, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"[{fld.name}] must be a table")
        defaults = cls()
        known = {f.name for f in dataclasses.fields(cls)}
        values = {}
        for key, value in raw.items():
            if key not in known:
                raise ConfigError(f"unknown key [{fld.name}] {key}")
            values[key] = _coerce(fld.name, key, value, getattr(defaults, key))
        sections[fld.name] = cls(**values)
    for key in data:
        if key not in sections:
            raise ConfigError(f"unknown section [{key}]")
    return Config(**sections)


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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return parse_config(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def parse_home(text: str) -> tuple[float, float]:
    try:
        lat_s, lon_s = text.split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError as exc:
        raise ConfigError(f"--home expects LAT,LON, got {text!r}") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ConfigError(f"--home out of range: {text!r}")
    return lat, lon


def with_overrides(
    config: Config,
    min_magnitude: float | None = None,
    hours: float | None = None,
    home: tuple[float, float] | None = None,
    glyphs: str | None = None,
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
        home_cfg = dataclasses.replace(home_cfg, name=home_cfg.name or "home", lat=home[0], lon=home[1])
    return dataclasses.replace(config, view=view, home=home_cfg)
