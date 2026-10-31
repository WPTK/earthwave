"""Braille dot canvas, equirectangular projection and the world coastline raster.

Pure Python with no Textual dependency. A `BrailleCanvas(cols, rows)` is a grid of 2x4 dots per terminal
cell. `coastline_canvas` rasterises the bundled Natural Earth 1:10m coastline for a viewport and caches
the result; `render_ascii` is the 1x1 cell fallback for terminals without braille glyphs.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable, Sequence
from functools import cache, lru_cache
from pathlib import Path
from typing import NamedTuple

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "coastline_10m.json"

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


@cache
def load_coastline() -> tuple[tuple[tuple[float, float], ...], ...]:
    """Bundled coastline polylines as ((lon, lat), ...) tuples."""
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return tuple(tuple((float(x), float(y)) for x, y in line) for line in raw)


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


def draw_polyline(canvas: BrailleCanvas, points: Sequence[tuple[float, float]], view: Viewport) -> None:
    """Draw a lon/lat polyline, handling the antimeridian by drawing the unwrapped line at +-360 shifts."""
    pts = _unwrap(points)
    if len(pts) < 2:
        return
    lo = min(p[0] for p in pts)
    hi = max(p[0] for p in pts)
    if max(p[1] for p in pts) < view.lat0 or min(p[1] for p in pts) > view.lat1:
        return
    k_min = int((view.lon0 - hi) // 360) - 1
    k_max = int((view.lon1 - lo) // 360) + 1
    for k in range(k_min, k_max + 1):
        shift = 360.0 * k
        if hi + shift < view.lon0 or lo + shift > view.lon1:
            continue
        xy = [project(lon + shift, lat, view, canvas.width, canvas.height) for lon, lat in pts]
        for (xa, ya), (xb, yb) in itertools.pairwise(xy):
            canvas.line(round(xa), round(ya), round(xb), round(yb))


@lru_cache(maxsize=16)
def _coastline_cached(cols: int, rows: int, view: Viewport) -> BrailleCanvas:
    canvas = BrailleCanvas(cols, rows)
    for line in load_coastline():
        draw_polyline(canvas, line, view)
    return canvas


def coastline_canvas(cols: int, rows: int, view: Viewport = WORLD) -> BrailleCanvas:
    """Coastline raster for the viewport and size, cached per (cols, rows, viewport). Returns a private copy."""
    return _coastline_cached(cols, rows, Viewport(*view)).copy()
