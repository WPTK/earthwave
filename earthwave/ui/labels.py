"""Text formatting helpers for the quake list, status bar and detail screen."""

from datetime import datetime

from earthwave.models import Earthquake
from earthwave.sources.geo import bearing_deg, compass, haversine_km
from earthwave.state.store import ViewFilter
from earthwave.ui.theme import PAGER_COLORS, TSUNAMI_BADGE_COLOR


def fmt_age(seconds: float) -> str:
    """Compact age: 45s, 12m, 5h, 3d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def zone_label(shown: datetime) -> str:
    """EDT, UTC; Windows spells zones out ('Central Europe Standard Time'), which shows as the offset, UTC+01:00."""
    zone = shown.tzname() or ""
    offset = shown.utcoffset()
    if " " in zone and offset is not None:
        minutes = int(offset.total_seconds() // 60)
        sign = "-" if minutes < 0 else "+"
        zone = f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    return zone


def fmt_time(dt: datetime, time_zone: str = "local") -> str:
    """The one timestamp format, 2026-10-31 08:00:00 EDT."""
    shown = dt.astimezone() if time_zone == "local" else dt
    return f"{shown:%Y-%m-%d %H:%M:%S} {zone_label(shown)}".strip()


def fmt_distance(quake: Earthquake, lat: float, lon: float) -> str:
    """Distance and compass direction of the quake as seen from (lat, lon), e.g. '1234 km NNE'."""
    km = haversine_km(lat, lon, quake.lat, quake.lon)
    return f"{km:.0f} km {compass(bearing_deg(lat, lon, quake.lat, quake.lon))}"


def fmt_depth(depth_km: float) -> str:
    """Whole kilometres; a depth that rounds to zero is "0", not "-0" (USGS reports -0.2 km for a quake at the surface).
    A real negative depth, above the datum, keeps its sign."""
    text = f"{depth_km:.0f}"
    return "0" if text == "-0" else text


def fmt_hours(hours: float) -> str:
    if hours % 24 == 0 and hours >= 168:  # whole weeks read as days; 48 h and 72 h stay in hours
        return f"{int(hours // 24)}d"
    return f"{hours:g}h"


def filter_summary(view: ViewFilter) -> str:
    text = f"M{view.min_magnitude:g}+ {fmt_hours(view.hours)}"
    return f'{text} "{view.query}"' if view.query else text


def pager_badge(alert: str | None) -> tuple[str, str]:
    """The PAGER level as one letter (G/Y/O/R, never colour alone) and its colour; a blank cell if none."""
    if alert in PAGER_COLORS:
        return alert[0].upper(), PAGER_COLORS[alert]
    return " ", ""


def tsunami_badge(flagged: bool, letter: str = "T") -> tuple[str, str]:
    """The tsunami flag as a letter and its colour; a blank cell when the quake has none."""
    return (letter, TSUNAMI_BADGE_COLOR) if flagged else (" ", "")
