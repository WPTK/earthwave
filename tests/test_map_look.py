"""The look of the pixel map, asserted rather than eyeballed (#226): land, sea, coast and markers sit on one grid, a
marker is drawn at the pixel its lon/lat projects to, and the tsunami ring is wide enough to read on land (#222)."""

from conftest import make_quake

from earthwave.ui.graphics.raster import COAST, LAND, SEA
from earthwave.ui.graphics.smooth import render_smooth
from earthwave.ui.widgets.braille import project
from earthwave.ui.widgets.map_panel import (
    TSUNAMI_RING_WIDTH,
    MapModel,
    PixelColors,
    TsunamiMark,
    pixel_scene,
)

CELL = (10, 20)
SEA_RGB, LAND_RGB, COAST_RGB = (10, 20, 60), (60, 60, 70), (90, 150, 220)


def rgb_of(name: str) -> tuple[int, int, int]:
    return {"red": (255, 0, 0), "orange1": (255, 135, 0)}.get(name, (200, 200, 0))


def scene_of(quakes=(), tsunami=()):
    m = MapModel()
    m.resize(160, 40)
    m.set_quakes(list(quakes), None)
    m.set_tsunami(list(tsunami))
    scene = pixel_scene(m, CELL, PixelColors(SEA_RGB, LAND_RGB, COAST_RGB), rgb_of)
    pixels, palette = render_smooth(scene, factor=1)
    return scene, pixels, palette


def at(scene, pixels, lon, lat) -> int:
    x, y = project(lon, lat, scene.view, scene.width, scene.height)
    return pixels[round(y) * scene.width + round(x)]


def test_land_sea_and_coast_share_one_grid():
    scene, pixels, _ = scene_of()
    assert at(scene, pixels, -98, 39) == LAND  # Kansas
    assert at(scene, pixels, 25, 18) == LAND  # the Sahara
    assert at(scene, pixels, -150, 0) == SEA  # the mid Pacific
    assert at(scene, pixels, -30, -30) == SEA  # the South Atlantic
    assert set(pixels) >= {SEA, LAND, COAST}  # and the coast is drawn over both


def test_coast_pixels_lie_between_land_and_sea():
    scene, pixels, _ = scene_of()
    w, h = scene.width, scene.height
    seen = 0
    for y in range(1, h - 1, 3):
        for x in range(1, w - 1, 3):
            if pixels[y * w + x] == COAST:
                around = {pixels[(y + dy) * w + x + dx] for dy in (-3, 0, 3) for dx in (-3, 0, 3)} - {COAST}
                seen += 1
                assert around <= {SEA, LAND}  # nothing else shares the coast's neighbourhood
    assert seen > 100


def test_marker_is_drawn_where_its_lon_lat_projects():
    quake = make_quake("m", 6.0, lat=-20.0, lon=-150.0)  # open ocean
    scene, pixels, _ = scene_of([quake])
    assert at(scene, pixels, -150, -20) not in (SEA, LAND, COAST)
    assert at(scene, pixels, -140, -20) == SEA  # and nothing beside it


def test_tsunami_source_ring_is_wide_enough_to_read_on_land():
    mark = TsunamiMark("a", "warning", None, (39.0, -98.0))  # Kansas
    scene, pixels, _ = scene_of(tsunami=[mark])
    ring = scene.shapes[0]
    assert ring.ring_width == TSUNAMI_RING_WIDTH >= 4
    x, y = project(-98, 39, scene.view, scene.width, scene.height)
    start = round(y) * scene.width + round(x) + int(ring.radius)
    ring_pixels = [px for px in pixels[start : start + 12] if px not in (SEA, LAND, COAST)]
    assert len(ring_pixels) >= TSUNAMI_RING_WIDTH
