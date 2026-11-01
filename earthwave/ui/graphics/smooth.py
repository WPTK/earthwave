"""Antialiased pixel map: the scene is drawn at `factor` times the size and box-filtered down.

Each output pixel averages a factor x factor block of the supersampled raster, so coast edges, island outlines and
marker discs get intermediate colours instead of stair steps. The palette is built from what the image actually
contains: every distinct block of palette indices becomes one averaged colour (a handful for plain coast, a few dozen
with markers), capped at 255 registers; beyond that a block takes the nearest colour already registered.
"""

from __future__ import annotations

from dataclasses import replace

from earthwave.ui.graphics.raster import Scene, compose
from earthwave.ui.graphics.sixel import RGB

MAX_COLOURS = 255
FACTOR = 2


def supersample(scene: Scene, factor: int) -> Scene:
    """`scene` drawn on a `factor` times finer pixel grid (same view, same cells)."""
    shapes = tuple(
        replace(s, radius=s.radius * factor, ring_width=s.ring_width * factor, outline_width=s.outline_width * factor)
        for s in scene.shapes
    )
    cw, ch = scene.cell
    return replace(
        scene,
        width=scene.width * factor,
        height=scene.height * factor,
        cell=(cw * factor, ch * factor),
        shapes=shapes,
        brush=round(1.5 * factor),  # a coast line about 1.5 pixels wide once filtered down
    )


def _mix(colours: list[RGB]) -> RGB:
    n = len(colours)
    return (
        round(sum(c[0] for c in colours) / n),
        round(sum(c[1] for c in colours) / n),
        round(sum(c[2] for c in colours) / n),
    )


def render_smooth(scene: Scene, factor: int = FACTOR) -> tuple[bytes, list[RGB]]:
    """(palette indices, palette) for `scene` at its own pixel size, antialiased."""
    if factor <= 1:
        return compose(scene), list(scene.palette)
    big = compose(supersample(scene, factor))
    width, height = scene.width, scene.height
    big_w = width * factor
    base = scene.palette
    palette: list[RGB] = []
    by_colour: dict[RGB, int] = {}
    lut: dict[tuple[int, ...], int] = {}

    def register(colour: RGB) -> int:
        if colour in by_colour:
            return by_colour[colour]
        if len(palette) < MAX_COLOURS:
            by_colour[colour] = len(palette)
            palette.append(colour)
            return by_colour[colour]
        nearest = min(
            range(len(palette)), key=lambda i: sum((a - b) ** 2 for a, b in zip(palette[i], colour, strict=True))
        )
        by_colour[colour] = nearest
        return nearest

    def resolve(block: tuple[int, ...]) -> int:
        index = register(_mix([base[i] for i in block]))
        lut[block] = index
        return index

    for colour in base:  # the base colours first, so the sea is register 0 and the common blocks never meet the cap
        register(colour)
    out = bytearray()
    get = lut.get
    for y in range(height):
        planes = []
        for dy in range(factor):
            start = (y * factor + dy) * big_w
            row = big[start : start + big_w]
            planes.extend(row[dx::factor] for dx in range(factor))
        out += bytes([i if (i := get(block)) is not None else resolve(block) for block in zip(*planes, strict=True)])
    return bytes(out), palette
