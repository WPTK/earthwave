"""Terminal world map: cached coastline raster with magnitude markers, home, selection and attention blinking.

`MapModel` is pure (no Textual): it owns the zoom mode, pan offset and size, builds viewports, places markers with
the same projection the raster uses and composes frames. `MapPanel` is the widget around it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache, partial

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from earthwave.config import HomeConfig
from earthwave.models import Earthquake, TsunamiAlert
from earthwave.notify.inapp import TSUNAMI_PREFIX, AttentionTracker
from earthwave.sources import levels
from earthwave.sources.geo import haversine_km
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.theme import LEVEL_COLORS, mag_color
from earthwave.ui.widgets.braille import (
    WORLD,
    Viewport,
    coastline_canvas,
    project_cell,
    render_ascii,
    unproject,
    warm_coastline,
)

COAST_STYLE = Style(color="#5c7a99")
HOME_STYLE = Style(color="bright_white", bold=True)
REGION_LAT_SPAN = 40  # degrees of latitude shown around the selected quake
HOME_LAT_SPAN = 16  # degrees of latitude shown around home
WORLD_PAN_STEP = 30  # degrees of longitude per arrow press at world zoom
PAN_GRID = 5  # panned view centres snap to this many degrees so rasters are reused from the cache
TICK_SECONDS = 0.25  # blink refresh; finer than the 0.5 s phase flips of the attention styles, so both phases render

# Magnitude band -> marker. Bands: <4.5, 4.5-5.4, 5.5-6.4, 6.5+.
UNICODE_MARKERS = ("·", "•", "●", "◉")
ASCII_MARKERS = ("+", "o", "O", "@")
UNICODE_HOME, ASCII_HOME = "⌂", "H"


class ZoomMode(StrEnum):
    WORLD = "world"
    REGION = "region"
    HOME = "home"


def marker_band(mag: float) -> int:
    if mag < 4.5:
        return 0
    if mag < 5.5:
        return 1
    if mag < 6.5:
        return 2
    return 3


def zoom_view(center_lon: float, center_lat: float, lat_span: int, cols: int, rows: int) -> Viewport:
    """Viewport of `lat_span` degrees of latitude around a centre, with equal degrees per dot in both axes.

    Everything is rounded to whole degrees so panning and re-selecting reuse cached rasters. Longitude may run past
    180 (the projection and the raster wrap); latitude is shifted to stay within the poles.
    """
    lat_span = min(lat_span, 180)
    aspect = (cols * 2) / (rows * 4)
    lon_span = min(360, max(1, round(lat_span * aspect)))
    lat0 = min(max(round(center_lat) - lat_span // 2, -90), 90 - lat_span)
    lon0 = round(center_lon) - lon_span // 2
    lon0 = (lon0 + 180) % 360 - 180
    return Viewport(float(lon0), float(lat0), float(lon0 + lon_span), float(lat0 + lat_span))


def world_view(pan_lon: int = 0) -> Viewport:
    """Whole-world viewport, optionally scrolled sideways (the map wraps at the antimeridian)."""
    if not pan_lon % 360:
        return WORLD
    lon0 = pan_lon % 360 - 180  # the left edge is WORLD's -180 shifted east by pan_lon
    return Viewport(float(lon0), WORLD.lat0, float(lon0 + 360), WORLD.lat1)


@lru_cache(maxsize=32)
def base_lines(cols: int, rows: int, view: Viewport, ascii_mode: bool) -> tuple[str, ...]:
    """Coastline raster as text rows for one size and viewport. Cached: only markers change between ticks."""
    canvas = coastline_canvas(cols, rows, view)
    return tuple(render_ascii(canvas) if ascii_mode else canvas.render())


@dataclass(frozen=True, slots=True)
class TsunamiMark:
    """One tsunami alert in force, reduced to what the map draws."""

    alert_id: str
    level: str
    geometry: tuple | None  # ("circle", lat, lon, radius_km) | ("polygon", ((lon, lat), ...)) | None
    source: tuple[float, float] | None  # (lat, lon) of the source quake


def tsunami_marks(
    alerts: Sequence[TsunamiAlert], quake_for: Callable[[TsunamiAlert], Earthquake | None]
) -> list[TsunamiMark]:
    """Marks for alerts in force; source = linked quake, else the product origin, else a circle centre."""
    marks = []
    for alert in alerts:
        quake = quake_for(alert)
        if quake is not None:
            source: tuple[float, float] | None = (quake.lat, quake.lon)
        elif alert.origin is not None:
            source = (alert.origin[1], alert.origin[2])
        elif alert.geometry and alert.geometry[0] == "circle":
            source = (alert.geometry[1], alert.geometry[2])
        else:
            source = None
        marks.append(TsunamiMark(alert.id, alert.level, alert.geometry, source))
    return marks


def _inside_polygon(lon: float, lat: float, ring: Sequence[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _unwrap_ring(ring: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    offset = 0.0
    prev: float | None = None
    for lon, lat in ring:
        if prev is not None:
            if lon - prev > 180:
                offset -= 360.0
            elif prev - lon > 180:
                offset += 360.0
        prev = lon
        out.append((lon + offset, lat))
    return out


@lru_cache(maxsize=64)
def geometry_cells(geometry: tuple, view: Viewport, cols: int, rows: int, ascii_mode: bool) -> frozenset:
    """Coastline cells whose centre lies inside the alert geometry (a circle or a polygon), for one view."""
    blank = " " if ascii_mode else "\u2800"
    base = base_lines(cols, rows, view, ascii_mode)
    ring = _unwrap_ring(geometry[1]) if geometry[0] == "polygon" else []
    cells = set()
    for row, line in enumerate(base):
        for col, ch in enumerate(line):
            if ch == blank:
                continue
            lon, lat = unproject(col * 2 + 0.5, row * 4 + 1.5, view, cols * 2, rows * 4)
            if geometry[0] == "circle":
                hit = haversine_km(lat, lon, geometry[1], geometry[2]) <= geometry[3]
            else:
                hit = any(_inside_polygon(lon + shift, lat, ring) for shift in (-360.0, 0.0, 360.0))
            if hit:
                cells.add((col, row))
    return frozenset(cells)


def _level_style(mark: TsunamiMark, tracker: AttentionTracker | None) -> Style:
    """Level colour; while the alert has attention it blinks with the tracker's flash background."""
    style = Style.parse(LEVEL_COLORS.get(mark.level, "dodger_blue2"))
    key = TSUNAMI_PREFIX + mark.alert_id
    if tracker is not None and tracker.kind_of(key) is not None:
        flash = tracker.style_for(key)
        style = Style(color="white", bgcolor=flash.bg, bold=True) if flash is not None else style + Style(bold=True)
    return style


def place_tsunami(
    marks: Sequence[TsunamiMark],
    view: Viewport,
    cols: int,
    rows: int,
    ascii_mode: bool,
    tracker: AttentionTracker | None = None,
) -> tuple[dict[tuple[int, int], Style], dict[tuple[int, int], Marker]]:
    """(coast tint per cell, source-ring markers). Higher alert levels overwrite lower ones where they overlap."""
    tint: dict[tuple[int, int], Style] = {}
    rings: dict[tuple[int, int], Marker] = {}
    for mark in sorted(marks, key=lambda m: levels.RANK.get(m.level, 0)):
        style = _level_style(mark, tracker)
        if mark.geometry:
            for cell in geometry_cells(mark.geometry, view, cols, rows, ascii_mode):
                tint[cell] = style
        if mark.source is not None and (cell := project_cell(mark.source[1], mark.source[0], view, cols, rows)):
            for dc, glyph in ((-1, "("), (1, ")")):
                col = cell[0] + dc
                if 0 <= col < cols:
                    rings[(col, cell[1])] = Marker(col, cell[1], glyph, style, (False, False, 0, -1.0))
    return tint, rings


def warm_world(cols: int, rows: int, ascii_mode: bool) -> None:
    """Load the coastline and rasterise the world view for this size, so the first draw is a cache hit."""
    warm_coastline()
    if cols > 0 and rows > 0:
        base_lines(cols, rows, WORLD, ascii_mode)


@dataclass(frozen=True, slots=True)
class Marker:
    col: int
    row: int
    glyph: str
    style: Style
    rank: tuple[int, int, int, float]  # selected, attention, home, magnitude: higher wins a shared cell


@dataclass(frozen=True, slots=True)
class Frame:
    cols: int
    rows: int
    view: Viewport
    base: tuple[str, ...]
    markers: dict[tuple[int, int], Marker]
    tint: dict[tuple[int, int], Style]


def _marker_style(quake: Earthquake, selected: bool, tracker: AttentionTracker | None) -> tuple[Style, bool]:
    """Marker style. A selected quake that is also flashing shows the selected style (reverse video, no flash)."""
    style = Style.parse(mag_color(quake.mag))
    attended = False
    if tracker is not None and tracker.kind_of(quake.id) is not None:
        attended = True
        flash = tracker.style_for(quake.id)
        style = Style(color="white", bgcolor=flash.bg, bold=True) if flash is not None else style + Style(bold=True)
    if selected:
        style = Style(color=style.color, bold=True, reverse=True)
    return style, attended


def place_markers(
    quakes: Sequence[Earthquake],
    selected_id: str | None,
    home: HomeConfig | None,
    view: Viewport,
    cols: int,
    rows: int,
    ascii_mode: bool,
    tracker: AttentionTracker | None = None,
) -> dict[tuple[int, int], Marker]:
    """One marker per cell. A shared cell goes to the selected quake, then attention, then home, then magnitude."""
    glyphs = ASCII_MARKERS if ascii_mode else UNICODE_MARKERS
    cells: dict[tuple[int, int], Marker] = {}

    def put(marker: Marker) -> None:
        key = (marker.col, marker.row)
        if key not in cells or marker.rank > cells[key].rank:
            cells[key] = marker

    for quake in quakes:
        cell = project_cell(quake.lon, quake.lat, view, cols, rows)
        if cell is None:
            continue
        selected = quake.id == selected_id
        style, attended = _marker_style(quake, selected, tracker)
        put(Marker(*cell, glyphs[marker_band(quake.mag)], style, (selected, attended, 0, quake.mag)))
    if home is not None and home.enabled and (cell := project_cell(home.lon, home.lat, view, cols, rows)):
        put(Marker(*cell, ASCII_HOME if ascii_mode else UNICODE_HOME, HOME_STYLE, (0, 0, 1, 0.0)))
    return cells


def row_segments(base: str, markers: Sequence[Marker], tint: dict[int, Style] | None = None) -> list[Segment]:
    """Split one raster row into styled runs: markers by column (any order), `tint` maps columns to a coast colour."""
    tint = tint or {}
    by_col = {m.col: m for m in markers}
    segments: list[Segment] = []
    run = ""
    run_style = COAST_STYLE
    for col, ch in enumerate(base):
        if col in by_col:
            glyph, style = by_col[col].glyph, by_col[col].style
        else:
            glyph, style = ch, tint.get(col, COAST_STYLE)
        if style == run_style:
            run += glyph
            continue
        if run:
            segments.append(Segment(run, run_style))
        run, run_style = glyph, style
    if run:
        segments.append(Segment(run, run_style))
    return segments


def _wrap180(lon: float) -> float:
    return (lon + 180) % 360 - 180


class MapModel:
    """Zoom, pan and frame composition without any widget machinery.

    `set_quakes` follows the data (the list cursor may land on another quake after a refresh) and keeps the pan;
    `select` is the user moving the cursor and recentres.
    """

    def __init__(
        self, home: HomeConfig | None = None, ascii_mode: bool = False, tracker: AttentionTracker | None = None
    ):
        self.home = home if home and home.enabled else None
        self.ascii_mode = ascii_mode
        self.tracker = tracker
        self.cols = 0
        self.rows = 0
        self.mode = ZoomMode.WORLD
        self.pan_lon = 0
        self.pan_lat = 0
        self.quakes: Sequence[Earthquake] = ()
        self.selected: Earthquake | None = None
        self.tsunami: Sequence[TsunamiMark] = ()

    # --- state --------------------------------------------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def set_quakes(self, quakes: Sequence[Earthquake], selected: Earthquake | None) -> None:
        self.quakes = quakes
        self.selected = selected

    def set_tsunami(self, marks: Sequence[TsunamiMark]) -> None:
        self.tsunami = marks

    def select(self, quake: Earthquake | None) -> None:
        if (quake.id if quake else None) != (self.selected.id if self.selected else None):
            self.pan_lon = self.pan_lat = 0
        self.selected = quake

    def cycle_zoom(self) -> ZoomMode:
        """World -> region around the selected quake -> home -> world, skipping modes that have no anchor."""
        order = [ZoomMode.WORLD, ZoomMode.REGION, ZoomMode.HOME]
        for step in range(1, 4):
            mode = order[(order.index(self.mode) + step) % 3]
            if mode is ZoomMode.REGION and self.selected is None:
                continue
            if mode is ZoomMode.HOME and self.home is None:
                continue
            self.mode = mode
            break
        self.pan_lon = self.pan_lat = 0
        return self.mode

    def _anchor(self) -> tuple[float, float, int] | None:
        """Centre (lon, lat) and latitude span of the zoomed view, or None for the world view."""
        if self.mode is ZoomMode.REGION and self.selected is not None:
            return self.selected.lon, self.selected.lat, REGION_LAT_SPAN
        if self.mode is ZoomMode.HOME and self.home is not None:
            return self.home.lon, self.home.lat, HOME_LAT_SPAN
        return None

    def pan(self, dx: int, dy: int) -> None:
        """Move the view by `dx`/`dy` quarter-steps (east and north positive).

        Longitude wraps modulo 360 and latitude stops at the poles, so reversing direction always moves at once.
        """
        anchor = self._anchor()
        if anchor is None:
            self.pan_lon = _wrap180(self.pan_lon + dx * WORLD_PAN_STEP)
            return
        _, lat, span = anchor
        view = self.view()
        step_lon = max(PAN_GRID, round((view.lon1 - view.lon0) / 4 / PAN_GRID) * PAN_GRID)
        step_lat = max(PAN_GRID, round(span / 4 / PAN_GRID) * PAN_GRID)
        self.pan_lon = _wrap180(self.pan_lon + dx * step_lon)
        lo, hi = -90 + span // 2, 90 - span + span // 2
        self.pan_lat = min(max(lat + self.pan_lat + dy * step_lat, lo), hi) - lat

    # --- geometry -----------------------------------------------------------------------------------------------

    def has_attention(self) -> bool:
        if self.tracker is None:
            return False
        return any(self.tracker.kind_of(q.id) is not None for q in self.quakes) or any(
            self.tracker.kind_of(TSUNAMI_PREFIX + m.alert_id) is not None for m in self.tsunami
        )

    def view(self) -> Viewport:
        cols, rows = max(self.cols, 1), max(self.rows, 1)
        anchor = self._anchor()
        if anchor is None:
            return world_view(self.pan_lon)
        lon, lat, span = anchor
        if self.pan_lon or self.pan_lat:
            lon = round((lon + self.pan_lon) / PAN_GRID) * PAN_GRID
            lat = round((lat + self.pan_lat) / PAN_GRID) * PAN_GRID
        return zoom_view(lon, lat, span, cols, rows)

    def frame(self) -> Frame:
        view = self.view()
        if self.cols < 1 or self.rows < 1:
            return Frame(self.cols, self.rows, view, (), {}, {})
        base = base_lines(self.cols, self.rows, view, self.ascii_mode)
        selected_id = self.selected.id if self.selected else None
        markers = place_markers(
            self.quakes, selected_id, self.home, view, self.cols, self.rows, self.ascii_mode, self.tracker
        )
        tint, rings = place_tsunami(self.tsunami, view, self.cols, self.rows, self.ascii_mode, self.tracker)
        for key, ring in rings.items():
            if key not in markers:  # rings never cover a quake or the home marker
                markers[key] = ring
        return Frame(self.cols, self.rows, view, base, markers, tint)

    def plain_rows(self) -> list[str]:
        """Text-only frame (raster plus marker glyphs), used by tests and goldens."""
        frame = self.frame()
        rows = [list(line) for line in frame.base]
        for marker in frame.markers.values():
            rows[marker.row][marker.col] = marker.glyph
        return ["".join(r) for r in rows]


class MapPanel(Widget, can_focus=True):
    BINDINGS = [
        Binding("left", "pan(-1, 0)", "West", show=False),
        Binding("right", "pan(1, 0)", "East", show=False),
        Binding("up", "pan(0, 1)", "North", show=False),
        Binding("down", "pan(0, -1)", "South", show=False),
    ]

    def __init__(
        self,
        tracker: AttentionTracker,
        glyphs: Glyphs,
        home: HomeConfig | None = None,
        ticks: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._ticks = ticks
        self._glyphs = glyphs
        self.model = MapModel(home, ascii_mode=not glyphs.unicode, tracker=tracker)
        self._frame: Frame | None = None
        self._by_row: dict[int, list[Marker]] = {}
        self._tint_by_row: dict[int, dict[int, Style]] = {}

    def on_mount(self) -> None:
        self.model.resize(self.size.width, self.size.height)
        self._update_title()
        if self._ticks:
            self.set_interval(TICK_SECONDS, self._tick)

    def _tick(self) -> None:
        if self.model.has_attention():
            self.refresh()

    def on_resize(self, event: Resize) -> None:
        cols, rows = self.size.width, self.size.height  # content size: event.size still includes the border
        self.model.resize(cols, rows)
        self.run_worker(
            partial(warm_world, cols, rows, self.model.ascii_mode),
            thread=True,
            group="warm",
            exclusive=True,
            exit_on_error=False,
        )
        self.refresh()

    # --- data and keys --------------------------------------------------------------------------------------------

    def set_quakes(self, quakes: Sequence[Earthquake], selected: Earthquake | None) -> None:
        self.model.set_quakes(quakes, selected)
        self.refresh()

    def select(self, quake: Earthquake | None) -> None:
        self.model.select(quake)
        self.refresh()

    def set_tsunami(self, marks: Sequence[TsunamiMark]) -> None:
        """Alerts in force: their geometry tints the coast and a ring flanks the source quake."""
        if list(marks) != list(self.model.tsunami):
            self.model.set_tsunami(marks)
            self.refresh()

    def action_cycle_zoom(self) -> None:
        self.model.cycle_zoom()
        self._update_title()
        self.refresh()

    def action_pan(self, dx: int, dy: int) -> None:
        self.model.pan(dx, dy)
        self.refresh()

    def _update_title(self) -> None:
        sep = "·" if self._glyphs.unicode else "-"
        self.border_title = f"Map {sep} {self.model.mode.value}"

    # --- rendering ------------------------------------------------------------------------------------------------

    def refresh(self, *regions, **kwargs):
        self._frame = None
        return super().refresh(*regions, **kwargs)

    def render_line(self, y: int) -> Strip:
        if self._frame is None:
            self._frame = self.model.frame()
            self._by_row = {}
            self._tint_by_row = {}
            for (col, row), style in self._frame.tint.items():
                self._tint_by_row.setdefault(row, {})[col] = style
            for marker in self._frame.markers.values():
                self._by_row.setdefault(marker.row, []).append(marker)
        frame = self._frame
        if y >= frame.rows or y >= len(frame.base):
            return Strip.blank(self.size.width, Style())
        return Strip(row_segments(frame.base[y], self._by_row.get(y, ()), self._tint_by_row.get(y)))
