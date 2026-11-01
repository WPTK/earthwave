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
    "rendered_fill",
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


def rendered_fill(app: EarthwaveApp, word: str) -> str:
    """The fill colour (#rrggbb) the exported screen paints the first run of text that starts with `word`."""
    svg = app.export_screenshot()
    run = re.search(rf'<text class="([^"]*)"[^>]*>{re.escape(word)}', svg)
    assert run, f"{word!r} is not on screen"
    for cls in run.group(1).split():
        found = re.search(rf"\.{re.escape(cls)} \{{[^}}]*fill: (#[0-9a-fA-F]{{6}})", svg)
        if found:
            return found.group(1).lower()
    raise AssertionError(f"no fill for {word!r}")


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
