"""Render the map as PNGs: the sixel picture (encoded, decoded again, so it shows what a terminal would) above an
illustration of the braille map with land fill, for the same scene. No terminal needed.

    .venv\\Scripts\\python scripts\\render_map_png.py OUTDIR [--cell 10x20] [--sizes 200x50,280x70]

Each size is a terminal size; the map panel inside it is measured from the real dashboard layout. The braille half
is drawn from the same cell data the terminal gets (dots as small squares, markers as discs), not from a font.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from rich.style import Style  # noqa: E402
from sixel_decode import decode  # noqa: E402

from earthwave.app import EarthwaveApp  # noqa: E402
from earthwave.clock import ReplayClock  # noqa: E402
from earthwave.config import Config, with_overrides  # noqa: E402
from earthwave.models import Earthquake  # noqa: E402
from earthwave.state.store import Store  # noqa: E402
from earthwave.ui.graphics.probe import GraphicsChoice  # noqa: E402
from earthwave.ui.graphics.smooth import render_smooth  # noqa: E402
from earthwave.ui.graphics.sixel import encode  # noqa: E402
from earthwave.ui.widgets.braille import coastline_canvas  # noqa: E402
from earthwave.ui.widgets.map_panel import MapModel, pixel_scene  # noqa: E402

PLACES = {
    "japan": (35.7, 139.7),
    "cuba": (21.5, -79.5),
    "philippines": (12.9, 121.8),
    "indonesia": (-2.5, 118.0),
}
ZOOMS = ("world", "wide", "region", "close")
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def png(path: Path, rows: list[list[tuple[int, int, int]]]) -> None:
    height, width = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    )


def quakes_around(lat: float, lon: float) -> list[Earthquake]:
    def quake(name: str, dlat: float, dlon: float, mag: float, minutes: int) -> Earthquake:
        return Earthquake(name, NOW - timedelta(minutes=minutes), lat + dlat, lon + dlon, mag, 10.0, place=name)

    return [
        quake("main", 0, 0, 6.8, 5),
        quake("a", 1.5, -2.0, 4.7, 60),
        quake("b", -2.0, 3.0, 5.9, 120),
        quake("c", 3.0, 2.5, 3.4, 200),
        quake("d", -0.8, -1.2, 5.1, 300),
    ]


def braille_rows(model: MapModel, colors, cell: tuple[int, int], rgb_of) -> list[list[tuple[int, int, int]]]:
    """The braille+fill map as pixels: land cells shaded, 2x4 dots per cell as squares, markers as discs."""
    cw, ch = cell
    frame = model.frame()
    canvas = coastline_canvas(model.cols, model.rows, frame.view)
    img = [[colors.sea] * (model.cols * cw) for _ in range(model.rows * ch)]

    def put(x: int, y: int, colour) -> None:
        if 0 <= y < len(img) and 0 <= x < len(img[0]):
            img[y][x] = colour

    for row in range(model.rows):
        for col in range(model.cols):
            if frame.land[row][col]:
                for y in range(row * ch, (row + 1) * ch):
                    img[y][col * cw : (col + 1) * cw] = [colors.land] * cw
            bits = canvas.cell_bits(col, row)
            if not bits:
                continue
            for dx in range(2):
                for dy in range(4):
                    if canvas.get(col * 2 + dx, row * 4 + dy):
                        cx = col * cw + round(cw * (0.25 + 0.5 * dx))
                        cy = row * ch + round(ch * (0.125 + 0.25 * dy))
                        for yy in range(cy - 1, cy + 2):
                            for xx in range(cx - 1, cx + 2):
                                put(xx, yy, colors.coast)
    radius = {"·": 2, "•": 3, "●": 4, "◉": 6, "⌂": 5}
    for marker in frame.markers.values():
        r = radius.get(marker.glyph, 3)
        colour = (
            colors.home
            if marker.glyph == "⌂"
            else rgb_of(str(marker.style.color.name if marker.style.color else "white"))
        )
        cx, cy = marker.col * cw + cw // 2, marker.row * ch + ch // 2
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    put(xx, yy, colour)
    return img


async def panel_of(size: tuple[int, int], cell: tuple[int, int]):
    clock = ReplayClock(NOW, real=lambda: 0.0)
    config = with_overrides(Config(), min_magnitude=-2.0, hours=24, glyphs="unicode")
    app = EarthwaveApp(
        config,
        store=Store(),
        clock=clock,
        run_scheduler=False,
        ticks=False,
        graphics=GraphicsChoice("sixel", cell, "preview"),
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        panel = app.dashboard.query_one("#map")
        theme = app.ansi_theme

        def rgb_of(name: str) -> tuple[int, int, int]:
            t = Style.parse(name).color.get_truecolor(theme)
            return t.red, t.green, t.blue

        return panel.size.width, panel.size.height, panel.pixel_colors(), rgb_of


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--cell", default="10x20")
    parser.add_argument("--sizes", default="200x50,280x70")
    parser.add_argument("--places", default=",".join(PLACES))
    args = parser.parse_args()
    cell = tuple(int(v) for v in args.cell.split("x"))
    args.outdir.mkdir(parents=True, exist_ok=True)
    for size_text in args.sizes.split(","):
        size = tuple(int(v) for v in size_text.split("x"))
        cols, rows, colors, rgb_of = asyncio.run(panel_of(size, cell))
        print(f"{size_text}: map panel {cols}x{rows} cells = {cols * cell[0]}x{rows * cell[1]} px")
        for place in args.places.split(","):
            lat, lon = PLACES[place]
            model = MapModel(None, False, None)
            model.resize(cols, rows)
            quakes = quakes_around(lat, lon)
            model.set_quakes(quakes, quakes[0])
            for zoom in ZOOMS:
                if zoom != "world":
                    model.cycle_zoom()
                scene = pixel_scene(model, cell, colors, rgb_of)
                started = time.perf_counter()
                pixels, palette = render_smooth(scene)
                raster_ms = (time.perf_counter() - started) * 1000
                data = encode(pixels, scene.width, scene.height, palette)
                total_ms = (time.perf_counter() - started) * 1000
                pixels, width, height = decode(data)
                sixel = [[px or (255, 0, 255) for px in row] for row in pixels]
                top = braille_rows(model, colors, cell, rgb_of)
                gap = [[(255, 255, 255)] * len(top[0]) for _ in range(6)]
                sixel += [[colors.sea] * width for _ in range(len(top) - height)]
                name = f"{place}_{zoom}_{size_text}"
                png(args.outdir / f"{name}_braille_over_sixel.png", top + gap + sixel)
                print(f"  {name}: sixel {len(data) // 1024} KiB, {len(palette)} colours, "
                      f"raster {raster_ms:.0f} ms, +encode {total_ms - raster_ms:.0f} ms")


if __name__ == "__main__":
    main()
