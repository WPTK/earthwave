"""Braille dot canvas, equirectangular projection and the world coastline raster.

Pure Python with no Textual dependency. A `BrailleCanvas(cols, rows)` is a grid of 2x4 dots per terminal
cell. `coastline_canvas` rasterises the bundled Natural Earth 1:10m coastline for a viewport and caches
the result; `render_ascii` is the 1x1 cell fallback for terminals without braille glyphs.
The data file holds three levels of detail and `level_for` picks one from the dot size of the view. `land_cells`
fills the matching land polygons (same three levels) and reports how many dots of each cell are land.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from functools import cache, lru_cache
from pathlib import Path
from typing import NamedTuple

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "coastline_10m.json"
LAND_FILE = DATA_FILE.with_name("land_10m.json")

# Braille dot bit for (dx, dy) within a cell: dots 1-3 and 7 on the left, 4-6 and 8 on the right.
_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_UPPER = 0x01 | 0x02 | 0x08 | 0x10  # dot rows 0-1
_LOWER = 0x04 | 0x40 | 0x20 | 0x80  # dot rows 2-3
_BLANK = 0x2800


class Viewport(NamedTuple):
    """Lon/lat window; `lat0` is the south edge. `lon1` may exceed 180 to span the antimeridian."""

    lon0: float
    lat0: float
    lon1: float
    lat1: float


WORLD = Viewport(-180.0, -90.0, 180.0, 90.0)


def project(lon: float, lat: float, view: Viewport, width: int, height: int) -> tuple[float, float]:
    """Equirectangular projection to fractional dot coordinates (0,0 top-left, width-1,height-1 bottom-right)."""
    x = (lon - view.lon0) / (view.lon1 - view.lon0) * (width - 1)
    y = (view.lat1 - lat) / (view.lat1 - view.lat0) * (height - 1)
    return x, y


def unproject(x: float, y: float, view: Viewport, width: int, height: int) -> tuple[float, float]:
    """Inverse of `project`; returns (lon, lat)."""
    lon = view.lon0 + x / (width - 1) * (view.lon1 - view.lon0)
    lat = view.lat1 - y / (height - 1) * (view.lat1 - view.lat0)
    return lon, lat


def wrap_lon(lon: float, view: Viewport) -> float:
    """Shift `lon` by multiples of 360 into [view.lon0, view.lon0 + 360)."""
    return view.lon0 + (lon - view.lon0) % 360.0


def project_cell(lon: float, lat: float, view: Viewport, cols: int, rows: int) -> tuple[int, int] | None:
    """Terminal cell (col, row) containing the point, or None when it lies outside the viewport."""
    lon = wrap_lon(lon, view)
    if lon > view.lon1 or not view.lat0 <= lat <= view.lat1:
        return None
    x, y = project(lon, lat, view, cols * 2, rows * 4)
    return min(cols - 1, round(x) // 2), min(rows - 1, round(y) // 4)


class BrailleCanvas:
    """Dot grid of `cols` x `rows` cells, each 2 dots wide and 4 dots tall."""

    def __init__(self, cols: int, rows: int) -> None:
        if cols < 1 or rows < 1:
            raise ValueError("canvas needs at least one cell")
        self.cols = cols
        self.rows = rows
        self.width = cols * 2
        self.height = rows * 4
        self._cells = [0] * (cols * rows)

    def clear(self) -> None:
        self._cells = [0] * (self.cols * self.rows)

    def copy(self) -> BrailleCanvas:
        other = BrailleCanvas(self.cols, self.rows)
        other._cells = list(self._cells)
        return other

    def set(self, x: int, y: int) -> None:
        """Light one dot; dots outside the canvas are ignored."""
        if 0 <= x < self.width and 0 <= y < self.height:
            self._cells[(y >> 2) * self.cols + (x >> 1)] |= _BITS[x & 1][y & 3]

    def get(self, x: int, y: int) -> bool:
        if 0 <= x < self.width and 0 <= y < self.height:
            return bool(self._cells[(y >> 2) * self.cols + (x >> 1)] & _BITS[x & 1][y & 3])
        return False

    def line(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Bresenham line between two dot coordinates; either end may be off canvas (clipped first)."""
        clipped = self._clip(x0, y0, x1, y1)
        if clipped is None:
            return
        x0, y0, x1, y1 = clipped
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def _clip(self, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int] | None:
        """Liang-Barsky clip to the canvas; segments already inside are returned untouched."""
        w, h = self.width - 1, self.height - 1
        if 0 <= x0 <= w and 0 <= x1 <= w and 0 <= y0 <= h and 0 <= y1 <= h:
            return x0, y0, x1, y1
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
            if p == 0:
                if q < 0:
                    return None
                continue
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return None
        return (
            round(x0 + t0 * dx),
            round(y0 + t0 * dy),
            round(x0 + t1 * dx),
            round(y0 + t1 * dy),
        )

    def cell_bits(self, col: int, row: int) -> int:
        return self._cells[row * self.cols + col]

    def render(self) -> list[str]:
        """One string of braille glyphs per cell row (empty cells are U+2800)."""
        c = self.cols
        return ["".join(chr(_BLANK + b) for b in self._cells[r * c : (r + 1) * c]) for r in range(self.rows)]


def render_ascii(canvas: BrailleCanvas) -> list[str]:
    """1x1 cell fallback: ' ' empty, '.' when only one half of the cell has dots, ':' when both halves do."""
    lines = []
    for r in range(canvas.rows):
        row = []
        for c in range(canvas.cols):
            bits = canvas.cell_bits(c, r)
            if not bits:
                row.append(" ")
            elif bits & _UPPER and bits & _LOWER:
                row.append(":")
            else:
                row.append(".")
        lines.append("".join(row))
    return lines


Polylines = tuple[tuple[tuple[float, float], ...], ...]


@cache
def load_levels() -> tuple[tuple[float, Polylines], ...]:
    """Bundled coastline as (Douglas-Peucker tolerance in degrees, polylines of (lon, lat)), finest level first."""
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return tuple(
        (
            float(level["tolerance"]),
            tuple(tuple((float(x), float(y)) for x, y in line) for line in level["lines"]),
        )
        for level in raw
    )


def level_for(view: Viewport, cols: int, rows: int) -> int:
    """Index of the coarsest level whose tolerance is under half a dot at this view; 0 (finest) when none is."""
    dot = min(
        (view.lon1 - view.lon0) / max(cols * 2 - 1, 1),
        (view.lat1 - view.lat0) / max(rows * 4 - 1, 1),
    )
    best = 0
    for index, (tolerance, _) in enumerate(load_levels()):
        if tolerance <= dot / 2:
            best = index
    return best


def _unwrap(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Make longitudes continuous (no jump over 180 degrees), so a dateline crossing becomes one long segment."""
    out: list[tuple[float, float]] = []
    offset = 0.0
    prev: float | None = None
    for lon, lat in points:
        if prev is not None:
            if lon - prev > 180:
                offset -= 360.0
            elif prev - lon > 180:
                offset += 360.0
        prev = lon
        out.append((lon + offset, lat))
    return out


def _draw_unwrapped(
    canvas: BrailleCanvas, pts: Sequence[tuple[float, float]], bounds: tuple[float, float, float, float], view: Viewport
) -> None:
    """Draw continuous-longitude points (bounds: lon lo, lon hi, lat lo, lat hi) at each +-360 shift in view."""
    lo, hi, lat_lo, lat_hi = bounds
    if lat_hi < view.lat0 or lat_lo > view.lat1:
        return
    k_min = int((view.lon0 - hi) // 360) - 1
    k_max = int((view.lon1 - lo) // 360) + 1
    width, height = canvas.width, canvas.height
    lon_span = view.lon1 - view.lon0
    lat_span = view.lat1 - view.lat0
    wm, hm = width - 1, height - 1
    for k in range(k_min, k_max + 1):
        shift = 360.0 * k
        if hi + shift < view.lon0 or lo + shift > view.lon1:
            continue
        prev: tuple[int, int] | None = None
        for lon, lat in pts:
            cur = (round((lon + shift - view.lon0) / lon_span * wm), round((view.lat1 - lat) / lat_span * hm))
            if cur != prev:
                if prev is None:
                    canvas.set(cur[0], cur[1])  # a polyline collapsing to one dot still leaves that dot
                elif not (  # both ends past the same edge: nothing of the segment is on the canvas
                    (prev[0] < 0 and cur[0] < 0)
                    or (prev[0] > wm and cur[0] > wm)
                    or (prev[1] < 0 and cur[1] < 0)
                    or (prev[1] > hm and cur[1] > hm)
                ):
                    canvas.line(prev[0], prev[1], cur[0], cur[1])
                prev = cur


def _bounds(pts: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), max(lons), min(lats), max(lats)


def draw_polyline(canvas: BrailleCanvas, points: Sequence[tuple[float, float]], view: Viewport) -> None:
    """Draw a lon/lat polyline, handling the antimeridian by drawing the unwrapped line at +-360 shifts."""
    pts = _unwrap(points)
    if len(pts) >= 2:
        _draw_unwrapped(canvas, pts, _bounds(pts), view)


@cache
def _prepared_coastline(level: int) -> tuple[tuple[list[tuple[float, float]], tuple[float, float, float, float]], ...]:
    """One level's polylines unwrapped once, with bounding boxes, so a view can reject most of them cheaply."""
    out = []
    for line in load_levels()[level][1]:
        pts = _unwrap(line)
        if len(pts) >= 2:
            out.append((pts, _bounds(pts)))
    return tuple(out)


def warm_coastline(level: int | None = None) -> None:
    """Load and prepare the coastline data (call off the UI thread to avoid a first-draw stall)."""
    for index in range(len(load_levels())) if level is None else (level,):
        _prepared_coastline(index)


@lru_cache(maxsize=16)
def _coastline_cached(cols: int, rows: int, view: Viewport) -> BrailleCanvas:
    canvas = BrailleCanvas(cols, rows)
    for pts, bounds in _prepared_coastline(level_for(view, cols, rows)):
        _draw_unwrapped(canvas, pts, bounds, view)
    return canvas


def coastline_canvas(cols: int, rows: int, view: Viewport = WORLD) -> BrailleCanvas:
    """Coastline raster for the viewport and size, cached per (cols, rows, viewport). Returns a private copy."""
    return _coastline_cached(cols, rows, Viewport(*view)).copy()


@cache
def load_land() -> tuple[Polylines, ...]:
    """Bundled land polygon rings (lon, lat), one entry per level in the same order as `load_levels`."""
    raw = json.loads(LAND_FILE.read_text(encoding="utf-8"))
    return tuple(tuple(tuple((float(x), float(y)) for x, y in ring) for ring in level["lines"]) for level in raw)


@cache
def _prepared_land(level: int) -> tuple[tuple[tuple[tuple[float, float], ...], tuple[float, float, float, float]], ...]:
    return tuple((ring, _bounds(ring)) for ring in load_land()[level])


@lru_cache(maxsize=16)
def _land_cached(cols: int, rows: int, view: Viewport) -> bytes:
    """Land dots per cell (0-8), row by row, by even-odd scanline fill of the land rings at each dot row."""
    width, height = cols * 2, rows * 4
    lon_scale = (width - 1) / (view.lon1 - view.lon0)
    lat_scale = (height - 1) / (view.lat1 - view.lat0)
    crossings: list[list[float]] = [[] for _ in range(height)]
    for ring, (lo, hi, lat_lo, lat_hi) in _prepared_land(level_for(view, cols, rows)):
        if lat_hi < view.lat0 or lat_lo > view.lat1:
            continue
        for k in range(int((view.lon0 - hi) // 360) - 1, int((view.lon1 - lo) // 360) + 2):
            shift = 360.0 * k
            if hi + shift < view.lon0 or lo + shift > view.lon1:
                continue
            xs = [(lon + shift - view.lon0) * lon_scale for lon, _ in ring]
            ys = [(view.lat1 - lat) * lat_scale for _, lat in ring]
            for i in range(len(ring) - 1):
                y0, y1 = ys[i], ys[i + 1]
                if y0 == y1:
                    continue
                first = max(math.ceil(min(y0, y1)), 0)
                last = min(math.ceil(max(y0, y1)) - 1, height - 1)
                if first > last:
                    continue
                x0, slope = xs[i], (xs[i + 1] - xs[i]) / (y1 - y0)
                for y in range(first, last + 1):
                    crossings[y].append(x0 + (y - y0) * slope)
    counts = [0] * (cols * rows)
    for y, row in enumerate(crossings):
        if len(row) < 2:
            continue
        row.sort()
        base = (y >> 2) * cols
        for a, b in zip(row[::2], row[1::2], strict=False):
            i0, i1 = max(math.ceil(a), 0), min(math.floor(b), width - 1)
            if i0 > i1:
                continue
            for col in range(i0 >> 1, (i1 >> 1) + 1):
                counts[base + col] += min(i1, col * 2 + 1) - max(i0, col * 2) + 1
    return bytes(min(c, 8) for c in counts)


def land_cells(cols: int, rows: int, view: Viewport = WORLD) -> bytes:
    """Land dots (0-8) per terminal cell, row-major, cached per (cols, rows, viewport)."""
    return _land_cached(cols, rows, Viewport(*view))
