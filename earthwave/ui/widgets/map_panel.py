"""Terminal world map: cached coastline raster with magnitude markers, home, selection and attention blinking.

`MapModel` is pure (no Textual): it owns the zoom mode, pan offset and size, builds viewports, places markers with
the same projection the raster uses and composes frames. `MapPanel` is the widget around it.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
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

from earthwave.config import AlertsConfig, HomeConfig
from earthwave.models import Earthquake, TsunamiAlert
from earthwave.notify.inapp import TSUNAMI_PREFIX, AttentionTracker
from earthwave.sources import levels
from earthwave.sources.geo import haversine_km
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.graphics.probe import FALLBACK_CELL, GraphicsChoice
from earthwave.ui.graphics.raster import Palette, Scene, Shape
from earthwave.ui.graphics.sixel import RGB, encode
from earthwave.ui.graphics.smooth import render_smooth
from earthwave.ui.theme import LEVEL_COLORS, mag_color
from earthwave.ui.widgets.braille import (
    WORLD,
    Viewport,
    coastline_canvas,
    land_cells,
    project_cell,
    render_ascii,
    unproject,
    warm_coastline,
)

log = logging.getLogger(__name__)

# Plain fallback for a bare `row_segments` call; the panel colours coast and land from the theme
# (`MapPanel.DEFAULT_CSS`).
COAST_STYLE = Style()
LAND_MIN_DOTS = 3  # a cell is land when at least this many of its 8 dots are inside a land polygon
HOME_STYLE = Style(color="bright_white", bold=True)
WORLD_TOP = 84.0  # north edge of a world view too short to show all 180 degrees of latitude (Greenland ends at 83.6)
MAJOR_MAG = AlertsConfig().major_magnitude  # with nothing selected, panned to or home, zoom goes to the latest of these
WORLD_PAN_STEP = 30  # degrees of longitude per arrow press at world zoom
PAN_GRID = 5  # panned view centres snap to this many degrees so rasters are reused from the cache
IMAGE_CACHE = 4  # encoded sixel images kept (a few hundred KB each): the views the user is flipping between
WRITE_CHUNK = 8192  # Textual writes Windows output in chunks this size, and so does the image
TICK_SECONDS = 0.25  # blink refresh; finer than the 0.5 s phase flips of the attention styles, so both phases render

# Magnitude band -> marker. Bands: <4.5, 4.5-5.4, 5.5-6.4, 6.5+.
UNICODE_MARKERS = ("·", "•", "●", "◉")
ASCII_MARKERS = ("+", "o", "O", "@")
UNICODE_HOME, ASCII_HOME = "⌂", "H"


class ZoomMode(StrEnum):
    WORLD = "world"
    WIDE = "wide"
    REGION = "region"
    CLOSE = "close"


ZOOM_ORDER = tuple(ZoomMode)
ZOOM_SPAN = {ZoomMode.WIDE: 90, ZoomMode.REGION: 40, ZoomMode.CLOSE: 16}  # degrees of latitude shown


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


def world_view(pan_lon: int = 0, cols: int = 0, rows: int = 0) -> Viewport:
    """All 360 degrees of longitude (scrolled sideways by `pan_lon`; the map wraps at the antimeridian).

    With a size the view has equal degrees per dot in both axes: a wide panel shows more than the 180 degrees of
    latitude (blank bands above and below), a short one the latitudes from `WORLD_TOP` down. Without one, the
    plain whole-world window.
    """
    lon0 = float(pan_lon % 360 - 180)  # the left edge is WORLD's -180 shifted east by pan_lon
    if cols < 1 or rows < 1:
        return Viewport(lon0, WORLD.lat0, lon0 + 360, WORLD.lat1)
    span = 360 / (cols * 2 - 1) * (rows * 4 - 1)
    if span >= 180:
        return Viewport(lon0, -span / 2, lon0 + 360, span / 2)
    lat0 = max(WORLD_TOP - span, -90.0)
    return Viewport(lon0, lat0, lon0 + 360, lat0 + span)


@lru_cache(maxsize=32)
def base_lines(cols: int, rows: int, view: Viewport, ascii_mode: bool) -> tuple[str, ...]:
    """Coastline raster as text rows for one size and viewport. Cached: only markers change between ticks."""
    canvas = coastline_canvas(cols, rows, view)
    return tuple(render_ascii(canvas) if ascii_mode else canvas.render())


@lru_cache(maxsize=32)
def land_rows(cols: int, rows: int, view: Viewport) -> tuple[bytes, ...]:
    """Per cell row, one byte per column: 1 where the cell is land. Cached like `base_lines`."""
    counts = land_cells(cols, rows, view)
    return tuple(bytes(c >= LAND_MIN_DOTS for c in counts[r * cols : (r + 1) * cols]) for r in range(rows))


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
        base_lines(cols, rows, world_view(0, cols, rows), ascii_mode)


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
    land: tuple[bytes, ...] = ()


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


def row_segments(
    base: str,
    markers: Sequence[Marker],
    tint: dict[int, Style] | None = None,
    land: bytes = b"",
    coast: Style = COAST_STYLE,
    land_bg: Style | None = None,
) -> list[Segment]:
    """Split one raster row into styled runs: markers by column (any order), `tint` maps columns to a coast colour.

    `land` flags the land cells of the row; they get the `land_bg` background under whatever is drawn there, except
    a style that already sets a background or is reversed (the selected marker, a flashing alert), which keep theirs.
    """
    tint = tint or {}
    by_col = {m.col: m for m in markers}
    segments: list[Segment] = []
    run = ""
    run_style = coast
    for col, ch in enumerate(base):
        if col in by_col:
            glyph, style = by_col[col].glyph, by_col[col].style
        else:
            glyph, style = ch, tint.get(col, coast)
        if land_bg is not None and col < len(land) and land[col] and style.bgcolor is None and not style.reverse:
            style = style + land_bg
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
        self.focus: tuple[float, float] | None = None  # zoom centre when nothing is selected
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
        """World -> wide -> region -> close -> world. Each step recentres on the selected quake; with none, on the
        point the map is panned to, else home, else the latest major quake (no quake at all: it stays on the world)."""
        nxt = ZOOM_ORDER[(ZOOM_ORDER.index(self.mode) + 1) % len(ZOOM_ORDER)]
        if nxt is ZoomMode.WORLD:
            self.focus = None
        elif self.selected is None:
            self.focus = self._pan_point() or self.focus or self._default_focus()
        self.mode = nxt
        self.pan_lon = self.pan_lat = 0
        return self.mode

    def _pan_point(self) -> tuple[float, float] | None:
        """Where the user has panned to (the world view's centre line or the zoomed view's centre), None if unpanned."""
        if not (self.pan_lon or self.pan_lat):
            return None
        if self.mode is ZoomMode.WORLD:
            return float(self.pan_lon), 0.0
        view = self.view()
        return _wrap180((view.lon0 + view.lon1) / 2), (view.lat0 + view.lat1) / 2

    def _default_focus(self) -> tuple[float, float] | None:
        if self.home is not None:
            return self.home.lon, self.home.lat
        latest = max((q for q in self.quakes if q.mag >= MAJOR_MAG), key=lambda q: q.time, default=None)
        latest = latest or max(self.quakes, key=lambda q: q.time, default=None)
        return (latest.lon, latest.lat) if latest is not None else None

    def _centre(self) -> tuple[float, float] | None:
        """Centre (lon, lat) of a zoomed view before panning: the selected quake, else the remembered focus."""
        if self.selected is not None:
            return self.selected.lon, self.selected.lat
        return self.focus

    def _anchor(self) -> tuple[float, float, int] | None:
        """Centre (lon, lat) and latitude span of the zoomed view, or None for the world view."""
        centre = self._centre()
        if self.mode is ZoomMode.WORLD or centre is None:
            return None
        return centre[0], centre[1], ZOOM_SPAN[self.mode]

    @property
    def shown(self) -> str:
        """The zoom actually drawn, with its span: a zoom with nothing to centre on falls back to the world."""
        span = ZOOM_SPAN.get(self.mode)
        if span is None or self._anchor() is None:
            return ZoomMode.WORLD.value
        return f"{self.mode.value} {span}{'°' if not self.ascii_mode else ' deg'}"

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
            return world_view(self.pan_lon, cols, rows)
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
        land = land_rows(self.cols, self.rows, view)
        selected_id = self.selected.id if self.selected else None
        markers = place_markers(
            self.quakes, selected_id, self.home, view, self.cols, self.rows, self.ascii_mode, self.tracker
        )
        tint, rings = place_tsunami(self.tsunami, view, self.cols, self.rows, self.ascii_mode, self.tracker)
        for key, ring in rings.items():
            if key not in markers:  # rings never cover a quake or the home marker
                markers[key] = ring
        return Frame(self.cols, self.rows, view, base, markers, tint, land)

    def plain_rows(self) -> list[str]:
        """Text-only frame (raster plus marker glyphs), used by tests and goldens."""
        frame = self.frame()
        rows = [list(line) for line in frame.base]
        for marker in frame.markers.values():
            rows[marker.row][marker.col] = marker.glyph
        return ["".join(r) for r in rows]


@dataclass(frozen=True, slots=True)
class PixelColors:
    """Colours of the pixel map that do not come from a marker's own style (the panel fills them from the theme)."""

    sea: RGB
    land: RGB
    coast: RGB
    outline: RGB = (6, 10, 18)  # one pixel edge round every marker, so it reads on sea, land and coast alike
    home: RGB = (255, 255, 255)
    attention: RGB = (255, 255, 255)


def _truecolor(color, theme) -> RGB:
    """RGB of a Rich colour (None reads as black); ANSI names resolve through the app's ANSI theme."""
    if color is None:
        return (0, 0, 0)
    t = color.get_truecolor(theme)
    return (t.red, t.green, t.blue)


MARKER_RADIUS = (0.3, 0.42, 0.6, 0.85)  # marker disc radius per magnitude band, in cell widths
PIXEL_BAND = 6  # a sixel band; the image height is a whole number of them


def pixel_size(cols: int, rows: int, cell: tuple[int, int]) -> tuple[int, int]:
    """Image size in pixels for a panel of `cols` x `rows` cells: full width, height cut to whole sixel bands."""
    return cols * cell[0], rows * cell[1] // PIXEL_BAND * PIXEL_BAND


def pixel_scene(
    model: MapModel, cell: tuple[int, int], colors: PixelColors, rgb_of: Callable[[str], RGB]
) -> Scene | None:
    """The model's frame as a pixel `Scene`: the same view, markers and tsunami tint, drawn at pixel resolution.

    `rgb_of` turns a style colour name (`mag_color`, `LEVEL_COLORS`) into RGB. Attention is a steady white ring
    rather than a blink: the image is only re-sent when the picture changes.
    """
    width, height = pixel_size(model.cols, model.rows, cell)
    if width < 1 or height < PIXEL_BAND:
        return None
    cw = cell[0]
    view = model.view()
    palette = Palette(colors.sea, colors.land, colors.coast, colors.outline)
    tints: dict[tuple[int, int], int] = {}
    shapes: list[Shape] = []
    for mark in sorted(model.tsunami, key=lambda m: levels.RANK.get(m.level, 0)):
        index = palette.index(rgb_of(LEVEL_COLORS.get(mark.level, "dodger_blue2")))
        if mark.geometry:
            for place in geometry_cells(mark.geometry, view, model.cols, model.rows, False):
                tints[place] = index
        if mark.source is not None:
            shapes.append(Shape(mark.source[1], mark.source[0], cw * 1.3, None, ring=index, ring_width=2))
    selected_id = model.selected.id if model.selected else None
    tracker = model.tracker

    def rank(quake: Earthquake) -> tuple[bool, bool, float]:
        return quake.id == selected_id, tracker is not None and tracker.kind_of(quake.id) is not None, quake.mag

    for quake in sorted(model.quakes, key=rank):
        selected, attended, _ = rank(quake)
        radius = max(2.0, cw * MARKER_RADIUS[marker_band(quake.mag)])
        ring = palette.index(colors.attention) if selected or attended else None
        shapes.append(Shape(quake.lon, quake.lat, radius, palette.index(rgb_of(mag_color(quake.mag))), ring=ring))
    if model.home is not None:
        shapes.append(Shape(model.home.lon, model.home.lat, max(2.0, cw * 0.45), palette.index(colors.home)))
    return Scene(
        view,
        width,
        height,
        tuple(palette.colours),
        cell,
        tuple((c, r, i) for (c, r), i in sorted(tints.items())),
        tuple(shapes),
    )


class MapPanel(Widget, can_focus=True):
    """The map. With `graphics` set to a sixel choice it draws a pixel image instead of braille cells.

    The image is not part of Textual's cell grid: it is written to the terminal after the cells, inside the same
    synchronized update (`EarthwaveApp._end_update` calls `emit_image`). The cells under it are blank, so whatever
    Textual repaints there (a modal, a toast, a resize) erases the image and `emit_image` puts it back; when nothing
    repaints the map, nothing is written.
    """

    COMPONENT_CLASSES = {"map--coast", "map--land"}
    DEFAULT_CSS = """
    MapPanel > .map--coast { color: $primary-lighten-1; }
    MapPanel > .map--land { background: $surface-lighten-2; }
    """
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
        graphics: GraphicsChoice | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._ticks = ticks
        self._glyphs = glyphs
        self.model = MapModel(home, ascii_mode=not glyphs.unicode, tracker=tracker)
        self._frame: Frame | None = None
        self._by_row: dict[int, list[Marker]] = {}
        self._tint_by_row: dict[int, dict[int, Style]] = {}
        self._sixel = graphics is not None and graphics.renderer == "sixel"
        self._cell = (graphics.cell if graphics is not None else None) or FALLBACK_CELL
        self._scene: Scene | None = None  # the picture wanted now
        self._images: OrderedDict[Scene, str] = OrderedDict()  # encoded pictures, most recent last
        self._building: Scene | None = None
        self._on_terminal: Scene | None = None  # the picture last written, None once something may have erased it
        self._stale = True  # Textual has repainted the cells since the image was written
        self._attention_seen = False
        self._rgb_cache: dict[tuple[str, int], RGB] = {}

    @property
    def renderer(self) -> str:
        return "sixel" if self._sixel else "braille"

    def on_mount(self) -> None:
        self.model.resize(self.size.width, self.size.height)
        self._update_title()
        if self._sixel:
            register = getattr(self.app, "add_overlay", None)
            if register is not None:
                register(self.emit_image)
            self.app.theme_changed_signal.subscribe(self, lambda _theme: self.refresh())
        if self._ticks:
            self.set_interval(TICK_SECONDS, self._tick)

    def on_unmount(self) -> None:
        if self._sixel and (unregister := getattr(self.app, "remove_overlay", None)) is not None:
            unregister(self.emit_image)

    def _tick(self) -> None:
        if self._sixel:  # the picture is re-sent only when it differs, so a tick that changes nothing is free
            if self.model.has_attention() or self._attention_seen:
                self.refresh()
        elif self.model.has_attention():
            self.refresh()

    def on_resize(self, event: Resize) -> None:
        cols, rows = self.size.width, self.size.height  # content size: event.size still includes the border
        self.model.resize(cols, rows)
        if not self._sixel:
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
        self._update_title()
        self.refresh()

    def select(self, quake: Earthquake | None) -> None:
        self.model.select(quake)
        self._update_title()
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
        self.border_title = f"Map {sep} {self.model.shown}"

    # --- pixel image ----------------------------------------------------------------------------------------------

    def pixel_colors(self) -> PixelColors:
        """Sea, land and coast as the braille map shows them: from the panel's background and component styles."""
        sea = self.background_colors[1]
        theme = self.app.ansi_theme
        return PixelColors(
            (sea.r, sea.g, sea.b),
            _truecolor(self.get_component_rich_style("map--land").bgcolor, theme),
            _truecolor(self.get_component_rich_style("map--coast").color, theme),
        )

    def rgb_of(self, name: str) -> RGB:
        theme = self.app.ansi_theme
        key = (name, id(theme))
        if key not in self._rgb_cache:
            self._rgb_cache[key] = _truecolor(Style.parse(name).color, theme)
        return self._rgb_cache[key]

    def _sync_image(self) -> bool:
        """Work out the picture the model calls for; True when the cells must be repainted for a new, ready image."""
        scene = pixel_scene(self.model, self._cell, self.pixel_colors(), self.rgb_of)
        self._attention_seen = self.model.has_attention()
        if scene == self._scene:
            return False
        self._scene = scene
        if scene is None or scene in self._images:
            return True
        if scene != self._building:
            self._building = scene
            self.run_worker(
                partial(self._build, scene), thread=True, group="sixel", exclusive=True, exit_on_error=False
            )
        return False

    def _build(self, scene: Scene) -> None:
        """Worker thread: rasterise and encode `scene`, then hand it back to the event loop."""
        pixels, palette = render_smooth(scene)
        data = encode(pixels, scene.width, scene.height, palette)
        self.app.call_from_thread(self._built, scene, data)

    def _built(self, scene: Scene, data: str) -> None:
        self._images[scene] = data
        while len(self._images) > IMAGE_CACHE:
            self._images.popitem(last=False)
        if self._building == scene:
            self._building = None
        if scene == self._scene:  # still what is wanted: repaint the cells, the image follows in the same update
            super().refresh()

    def invalidate_image(self) -> None:
        """The cells under the image were repainted by something this panel did not see (another screen came and
        went): repaint them so the image follows."""
        if self._sixel:
            self._on_terminal = None
            self._stale = True
            super().refresh()

    def emit_image(self, write: Callable[[str], None]) -> None:
        """Write the picture to the terminal if the cells under it were repainted since it was last written.

        Called after every screen update, inside the synchronized update. Writes nothing while another screen is on
        top (the modal's repaint has erased it and it must not draw over the dialog), nothing while the map is hidden,
        and nothing when the picture on the terminal is the wanted one and untouched.
        """
        if not self._sixel:
            return
        try:
            on_top = self.app.screen is self.screen
        except Exception:  # not mounted, or the screen stack is empty: shutting down
            return
        region = self.content_region
        if not on_top or not self.display or region.width < 1 or region.height < 1:
            self._on_terminal = None
            return
        scene = self._scene
        data = self._images.get(scene) if scene is not None else None
        if data is None or (not self._stale and self._on_terminal == scene):
            return
        if (region.width, region.height) != (self.model.cols, self.model.rows):
            return  # the model has not seen the new size yet; the resize that follows repaints
        try:
            self._images.move_to_end(scene)
            write(f"\x1b7\x1b[{region.y + 1};{region.x + 1}H")
            for start in range(0, len(data), WRITE_CHUNK):
                write(data[start : start + WRITE_CHUNK])
            write("\x1b8")
        except Exception as exc:  # a terminal that takes the image badly gets the braille map instead
            log.warning("sixel output failed (%r); falling back to braille", exc)
            self._sixel = False
            self._on_terminal = None
            self.refresh()
            return
        self._on_terminal = scene
        self._stale = False

    # --- rendering ------------------------------------------------------------------------------------------------

    def refresh(self, *regions, **kwargs):
        self._frame = None
        if self._sixel and not regions and not kwargs and not self._sync_image():
            return self  # the picture is unchanged (or still being drawn): leave the one on the terminal alone
        return super().refresh(*regions, **kwargs)

    def render_lines(self, crop):
        if self._sixel:
            self._stale = True  # the compositor is about to write these cells (even from its cache), erasing the image
        return super().render_lines(crop)

    def render_line(self, y: int) -> Strip:
        if self._sixel:
            self._stale = True  # these cells are about to be written, which erases the image under them
            return Strip.blank(self.size.width, self.rich_style)
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
        coast = Style(color=self.get_component_rich_style("map--coast").color)  # the panel supplies the sea
        land_bg = Style(bgcolor=self.get_component_rich_style("map--land").bgcolor)
        land = frame.land[y] if y < len(frame.land) else b""
        markers = self._by_row.get(y, ())
        return Strip(row_segments(frame.base[y], markers, self._tint_by_row.get(y), land, coast, land_bg))
