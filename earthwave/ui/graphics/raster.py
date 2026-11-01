"""Pixel raster of the map: land fill and coastline from the tiered data, tint cells and marker shapes on top.

Pure Python, no Textual. A raster is a `bytes` of palette indices, row-major. `render_base` draws sea, land and coast
for one (view, size) and is cached; `compose` copies it and adds what changes more often (tsunami tint, markers).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from earthwave.ui.graphics.sixel import RGB
from earthwave.ui.widgets.braille import (
    BrailleCanvas,
    Viewport,
    _draw_unwrapped,
    land_spans,
    level_for_dot,
    prepared_coastline,
    project,
    wrap_lon,
)

SEA, LAND, COAST, OUTLINE = 0, 1, 2, 3  # the fixed palette slots; marker colours are added after them
BRUSH = 2  # coast line width in pixels


class Palette:
    """Palette under construction: the four fixed colours first, then one slot per distinct colour added."""

    def __init__(self, sea: RGB, land: RGB, coast: RGB, outline: RGB) -> None:
        self.colours: list[RGB] = [sea, land, coast, outline]

    def index(self, colour: RGB) -> int:
        if colour not in self.colours:
            if len(self.colours) >= 255:
                return COAST
            self.colours.append(colour)
        return self.colours.index(colour)


class PixelCanvas(BrailleCanvas):
    """A `BrailleCanvas` look-alike whose dots are single pixels, so the coastline code draws into it unchanged."""

    def __init__(self, width: int, height: int, pixels: bytearray, brush: int = BRUSH) -> None:
        self.cols = self.rows = 0
        self.width, self.height = width, height
        self.pixels = pixels
        self.brush = brush

    def set(self, x: int, y: int) -> None:
        w, h, pixels = self.width, self.height, self.pixels
        for dy in range(self.brush):
            for dx in range(self.brush - dy):  # a small corner brush, so lines stay thin and unbroken
                if 0 <= x + dx < w and 0 <= y + dy < h:
                    pixels[(y + dy) * w + x + dx] = COAST


@lru_cache(maxsize=8)
def render_base(view: Viewport, width: int, height: int, brush: int = BRUSH) -> bytes:
    """Sea, land and coast as palette indices for a `width` x `height` pixel view of `view`."""
    pixels = bytearray(width * height)  # SEA is 0
    dot = min((view.lon1 - view.lon0) / max(width - 1, 1), (view.lat1 - view.lat0) / max(height - 1, 1))
    level = level_for_dot(dot)
    land = bytes([LAND])
    for y, x0, x1 in land_spans(width, height, view, level):
        start = y * width
        pixels[start + x0 : start + x1 + 1] = land * (x1 - x0 + 1)
    canvas = PixelCanvas(width, height, pixels, brush)
    for pts, bounds in prepared_coastline(level):
        _draw_unwrapped(canvas, pts, bounds, view)
    return bytes(pixels)


@dataclass(frozen=True, slots=True)
class Shape:
    """A marker: a filled disc (with a one pixel outline), or a ring when `fill` is None; positioned by lon/lat."""

    lon: float
    lat: float
    radius: float
    fill: int | None
    ring: int | None = None  # extra ring drawn around the disc
    ring_width: int = 2
    outline: bool = True
    outline_width: float = 1.0


@dataclass(frozen=True, slots=True)
class Scene:
    """Everything that decides the picture; hashable, so it is the cache key for the encoded image."""

    view: Viewport
    width: int
    height: int
    palette: tuple[RGB, ...]
    cell: tuple[int, int]  # pixel size of one terminal cell, used to place `tints`
    tints: tuple[tuple[int, int, int], ...] = ()  # (col, row, palette index): coast pixels of that cell take the colour
    shapes: tuple[Shape, ...] = ()  # drawn in order, so the last one is on top
    brush: int = BRUSH


def _span(pixels: bytearray, width: int, height: int, y: int, x0: int, x1: int, value: int) -> None:
    if 0 <= y < height:
        x0, x1 = max(x0, 0), min(x1, width - 1)
        if x0 <= x1:
            start = y * width
            pixels[start + x0 : start + x1 + 1] = bytes([value]) * (x1 - x0 + 1)


def _half_width(radius: float, dy: int) -> int | None:
    squared = radius * radius - dy * dy
    return None if squared < 0 else int(math.sqrt(squared) + 0.5)


def draw_disc(pixels: bytearray, width: int, height: int, cx: int, cy: int, radius: float, value: int) -> None:
    for dy in range(-math.ceil(radius), math.ceil(radius) + 1):
        if (half := _half_width(radius, dy)) is not None:
            _span(pixels, width, height, cy + dy, cx - half, cx + half, value)


def draw_ring(
    pixels: bytearray, width: int, height: int, cx: int, cy: int, radius: float, thickness: int, value: int
) -> None:
    inner = radius - thickness
    for dy in range(-math.ceil(radius), math.ceil(radius) + 1):
        outer_half = _half_width(radius, dy)
        if outer_half is None:
            continue
        inner_half = _half_width(inner, dy) if inner > 0 else None
        if inner_half is None:
            _span(pixels, width, height, cy + dy, cx - outer_half, cx + outer_half, value)
        else:
            _span(pixels, width, height, cy + dy, cx - outer_half, cx - inner_half, value)
            _span(pixels, width, height, cy + dy, cx + inner_half, cx + outer_half, value)


def _draw_shape(pixels: bytearray, width: int, height: int, view: Viewport, shape: Shape) -> None:
    lon = wrap_lon(shape.lon, view)
    if lon > view.lon1 or not view.lat0 <= shape.lat <= view.lat1:
        return
    fx, fy = project(lon, shape.lat, view, width, height)
    cx, cy = round(fx), round(fy)
    if shape.ring is not None:
        draw_ring(pixels, width, height, cx, cy, shape.radius + shape.ring_width + shape.outline_width, shape.ring_width, shape.ring)
    if shape.fill is not None:
        if shape.outline:
            draw_disc(pixels, width, height, cx, cy, shape.radius + shape.outline_width, OUTLINE)
        draw_disc(pixels, width, height, cx, cy, shape.radius, shape.fill)


def compose(scene: Scene) -> bytes:
    """The base raster with the scene's tint and shapes drawn over it."""
    width, height = scene.width, scene.height
    pixels = bytearray(render_base(scene.view, width, height, scene.brush))
    cw, ch = scene.cell
    for col, row, index in scene.tints:
        table = bytes(index if i == COAST else i for i in range(256))
        for y in range(row * ch, min((row + 1) * ch, height)):
            start = y * width + col * cw
            end = min(start + cw, (y + 1) * width)
            if start < end:
                pixels[start:end] = pixels[start:end].translate(table)
    for shape in scene.shapes:
        _draw_shape(pixels, width, height, scene.view, shape)
    return bytes(pixels)
