"""Shared helpers for the UAT suite: screen text, sizes, and the app builders reused from the unit tests."""

import html
import re
from dataclasses import replace
from datetime import UTC, datetime

from test_observations import strip_app
from test_tsunami import KAM, NOW, TG, kam, kam_quake, read
from test_ui import WEEK_NOW, week_app

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import tsunamigov
from earthwave.state.store import Store

__all__ = [
    "KAM",
    "NOW",
    "SIZES",
    "TG",
    "TOO_SMALL_SIZES",
    "WEEK_NOW",
    "info_alert",
    "kam",
    "kam_alert_app",
    "kam_quake",
    "read",
    "screen_text",
    "strip_app",
    "week_app",
]

# Terminal sizes the suite drives: Windows Terminal default, the 100x30 minimum and the larger layouts.
SIZES = [(120, 30), (100, 30), (100, 50), (200, 50), (280, 70)]
TOO_SMALL_SIZES = [(80, 24), (99, 30), (100, 29)]

_CELL_W = 12.2  # Textual's SVG export: character cell width and line height
_CELL_H = 24.4
_TEXT = re.compile(r'<text class="([^"]*)" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>')


def screen_text(app: EarthwaveApp) -> str:
    """What is on screen right now, as plain text rows, read back from the SVG export (no private Textual API)."""
    cells: dict[int, dict[int, str]] = {}
    for cls, x, y, body in _TEXT.findall(app.export_screenshot()):
        if cls.endswith("-title"):
            continue
        row = round((float(y) - 20) / _CELL_H)
        col = round(float(x) / _CELL_W)
        for offset, ch in enumerate(html.unescape(body).replace("\xa0", " ")):
            cells.setdefault(row, {})[col + offset] = ch
    lines = []
    for row in range(max(cells, default=-1) + 1):
        line = cells.get(row, {})
        width = max(line, default=-1) + 1
        lines.append("".join(line.get(c, " ") for c in range(width)).rstrip())
    return "\n".join(lines)


def info_alert() -> TsunamiAlert:
    return tsunamigov.parse_cap(read(TG / "PAAQ_0.cap.xml"), read(TG / "PAAQ_0.txt"))


def kam_alert_app(*alert_indexes: int, quake: bool = True, **kwargs) -> EarthwaveApp:
    """The Kamchatka quake with the chosen replay-fixture products already in the store (0 watch, 1 warning, 2 cancel)."""
    clock = ReplayClock(datetime(2025, 7, 29, 23, 55, tzinfo=UTC), real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, view=replace(config.view, time_zone="UTC"))
    store = Store(config.alerts, config.home)
    if quake:
        store.merge(Batch("usgs", clock.now(), quakes=(kam_quake(),), backfill=True), clock)
    if alert_indexes:
        store.merge(Batch("tsunamigov", clock.now(), alerts=tuple(kam(i) for i in alert_indexes)), clock)
    return EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False, **kwargs)
