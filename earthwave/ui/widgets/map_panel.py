"""Terminal world map: cached coastline raster with magnitude markers, home, selection and attention blinking.

`MapModel` is pure (no Textual): it owns the zoom mode, pan offset and size, builds viewports, places markers with
the same projection the raster uses and composes frames. `MapPanel` is the widget around it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from earthwave.config import HomeConfig
from earthwave.models import Earthquake
from earthwave.notify.inapp import AttentionTracker
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.theme import mag_color
from earthwave.ui.widgets.braille import WORLD, Viewport, coastline_canvas, project_cell, render_ascii

COAST_STYLE = Style(color="#5c7a99")
HOME_STYLE = Style(color="bright_white", bold=True)
REGION_LAT_SPAN = 40  # degrees of latitude shown around the selected quake
HOME_LAT_SPAN = 16  # degrees of latitude shown around home
WORLD_PAN_STEP = 30  # degrees of longitude per arrow press at world zoom

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
    lon0 = (pan_lon + 180) % 360 - 180
    return Viewport(float(lon0), WORLD.lat0, float(lon0 + 360), WORLD.lat1)


@lru_cache(maxsize=32)
def base_lines(cols: int, rows: int, view: Viewport, ascii_mode: bool) -> tuple[str, ...]:
    """Coastline raster as text rows for one size and viewport. Cached: only markers change between ticks."""
    canvas = coastline_canvas(cols, rows, view)
    return tuple(render_ascii(canvas) if ascii_mode else canvas.render())


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


def _marker_style(quake: Earthquake, selected: bool, tracker: AttentionTracker | None) -> tuple[Style, bool]:
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


def row_segments(base: str, markers: Sequence[Marker]) -> list[Segment]:
    """Split one raster row around its markers (given in any order)."""
    segments: list[Segment] = []
    pos = 0
    for marker in sorted(markers, key=lambda m: m.col):
        if marker.col > pos:
            segments.append(Segment(base[pos : marker.col], COAST_STYLE))
        segments.append(Segment(marker.glyph, marker.style))
        pos = marker.col + 1
    if pos < len(base):
        segments.append(Segment(base[pos:], COAST_STYLE))
    return segments


class MapModel:
    """Zoom, pan and frame composition without any widget machinery."""

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

    # --- state --------------------------------------------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def set_quakes(self, quakes: Sequence[Earthquake], selected: Earthquake | None) -> None:
        self.quakes = quakes
        self.select(selected)

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

    def pan(self, dx: int, dy: int) -> None:
        """Move the view by `dx`/`dy` quarter-steps (east and north positive)."""
        view = self.view()
        if self.mode is ZoomMode.WORLD:
            self.pan_lon += dx * WORLD_PAN_STEP
            return
        self.pan_lon += dx * max(1, round((view.lon1 - view.lon0) / 4))
        self.pan_lat += dy * max(1, round((view.lat1 - view.lat0) / 4))

    # --- geometry -----------------------------------------------------------------------------------------------

    def view(self) -> Viewport:
        cols, rows = max(self.cols, 1), max(self.rows, 1)
        if self.mode is ZoomMode.REGION and self.selected is not None:
            return zoom_view(
                self.selected.lon + self.pan_lon, self.selected.lat + self.pan_lat, REGION_LAT_SPAN, cols, rows
            )
        if self.mode is ZoomMode.HOME and self.home is not None:
            return zoom_view(self.home.lon + self.pan_lon, self.home.lat + self.pan_lat, HOME_LAT_SPAN, cols, rows)
        return world_view(self.pan_lon)

    def frame(self) -> Frame:
        view = self.view()
        if self.cols < 1 or self.rows < 1:
            return Frame(self.cols, self.rows, view, (), {})
        base = base_lines(self.cols, self.rows, view, self.ascii_mode)
        selected_id = self.selected.id if self.selected else None
        markers = place_markers(
            self.quakes, selected_id, self.home, view, self.cols, self.rows, self.ascii_mode, self.tracker
        )
        return Frame(self.cols, self.rows, view, base, markers)

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
        # Hook for M2: tsunami alert geometry (NWS polygon, CAP circle) and the source-quake ring. Stored only; the
        # panel draws nothing from it yet.
        self.tsunami_overlay: object | None = None

    def on_mount(self) -> None:
        self.model.resize(self.size.width, self.size.height)
        self._update_title()
        if self._ticks:
            self.set_interval(0.5, self.refresh)

    def on_resize(self, event: Resize) -> None:
        self.model.resize(event.size.width, event.size.height)
        self.refresh()

    # --- data and keys --------------------------------------------------------------------------------------------

    def set_quakes(self, quakes: Sequence[Earthquake], selected: Earthquake | None) -> None:
        self.model.set_quakes(quakes, selected)
        self.refresh()

    def select(self, quake: Earthquake | None) -> None:
        self.model.select(quake)
        self.refresh()

    def set_tsunami_overlay(self, overlay: object | None) -> None:
        """M2 hook: hand over alert geometry and source quakes to draw. Not rendered in M3b."""
        self.tsunami_overlay = overlay

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
            self.model.resize(self.size.width, self.size.height)
            self._frame = self.model.frame()
            self._by_row = {}
            for marker in self._frame.markers.values():
                self._by_row.setdefault(marker.row, []).append(marker)
        frame = self._frame
        if y >= frame.rows or y >= len(frame.base):
            return Strip.blank(self.size.width)
        return Strip(row_segments(frame.base[y], self._by_row.get(y, ())))
