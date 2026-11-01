import itertools
import os
import time
from pathlib import Path

import pytest

from earthwave.ui.widgets.braille import (
    _BITS,
    WORLD,
    BrailleCanvas,
    Viewport,
    _land_cached,
    coastline_canvas,
    dot_raster,
    draw_polyline,
    level_for,
    load_land,
    load_levels,
    project,
    project_cell,
    render_ascii,
    unproject,
    wrap_lon,
)

GOLDEN = Path(__file__).parent / "golden" / "braille"


def test_project_corners_and_center():
    assert project(-180, 90, WORLD, 160, 96) == (0, 0)
    assert project(180, -90, WORLD, 160, 96) == (159, 95)
    x, y = project(0, 0, WORLD, 161, 97)
    assert (x, y) == (80, 48)


def test_project_roundtrip():
    view = Viewport(120, -10, 160, 30)
    x, y = project(143.5, 12.25, view, 200, 100)
    lon, lat = unproject(x, y, view, 200, 100)
    assert lon == pytest.approx(143.5)
    assert lat == pytest.approx(12.25)


def test_wrap_lon_and_project_cell():
    view = Viewport(150, -60, 210, 0)  # spans the antimeridian
    assert wrap_lon(-170, view) == pytest.approx(190)
    assert project_cell(-170, -30, view, 60, 15) is not None
    assert project_cell(180, -30, view, 60, 15) is not None
    assert project_cell(100, -30, view, 60, 15) is None  # west of lon0
    assert project_cell(-149, -30, view, 60, 15) is None  # just east of lon1 (211)
    assert project_cell(180, 10, view, 60, 15) is None  # north of lat1
    assert project_cell(-180, 90, WORLD, 80, 24) == (0, 0)
    assert project_cell(179.9, -90, WORLD, 80, 24) == (79, 23)


def test_set_and_render_dot_bits():
    c = BrailleCanvas(2, 1)
    c.set(0, 0)
    c.set(1, 3)
    c.set(2, 0)
    assert c.render() == ["\u2881\u2801"]
    c.set(-1, 0)
    c.set(99, 99)  # ignored
    assert c.get(0, 0) and not c.get(0, 1)


def test_full_cell_is_all_dots():
    c = BrailleCanvas(1, 1)
    for x in range(2):
        for y in range(4):
            c.set(x, y)
    assert c.render() == ["\u28ff"]


def test_bresenham_horizontal_vertical_diagonal():
    c = BrailleCanvas(4, 2)
    c.line(0, 0, 7, 0)
    assert all(c.get(x, 0) for x in range(8))
    c.clear()
    c.line(3, 0, 3, 7)
    assert all(c.get(3, y) for y in range(8))
    c.clear()
    c.line(0, 0, 7, 7)
    assert all(c.get(i, i) for i in range(8))
    assert sum(c.get(x, y) for x in range(8) for y in range(8)) == 8


def test_line_is_symmetric_and_clipped():
    a, b = BrailleCanvas(5, 3), BrailleCanvas(5, 3)
    a.line(0, 0, 9, 11)
    b.line(9, 11, 0, 0)
    assert a.render() == b.render()
    c = BrailleCanvas(2, 1)
    c.line(-50, -50, -10, -10)  # wholly off canvas
    c.line(-5, 1, 50, 1)  # crosses it
    assert c.get(0, 1) and c.get(3, 1)


def test_dateline_polyline_draws_across_the_edge():
    # 170E -> 170W is a 20 degree hop east across the antimeridian, not a 340 degree sweep west.
    c = BrailleCanvas(40, 10)
    draw_polyline(c, [(170, 0), (-170, 0)], WORLD)
    xs = [x for x in range(c.width) for y in range(c.height) if c.get(x, y)]
    assert min(xs) == 0 and max(xs) == c.width - 1
    assert len(xs) < 20  # two short stubs, not a 340-degree line
    # In a viewport that spans the antimeridian the same line is continuous.
    d = BrailleCanvas(40, 10)
    draw_polyline(d, [(170, 0), (-170, 0)], Viewport(160, -10, 200, 10))
    row = [x for x in range(d.width) if any(d.get(x, y) for y in range(d.height))]
    assert row == list(range(min(row), max(row) + 1))
    assert max(row) - min(row) > 30


def test_coastline_data_shape():
    levels = load_levels()
    assert [t for t, _ in levels] == [0.01, 0.1, 0.4]  # finest first
    fine = levels[0][1]
    assert len(fine) > 1000
    assert sum(len(line) for line in fine) > 100_000  # linecast's 1:10m land layer has about 138k points
    for _, lines in levels:
        for line in lines:
            assert len(line) >= 2
            for lon, lat in line:
                assert -180 <= lon <= 180 and -90 <= lat <= 90
    sizes = [sum(len(line) for line in lines) for _, lines in levels]
    assert sizes[0] > 5 * sizes[1] > 5 * sizes[2]  # each level is far lighter than the one before


def _extent(line) -> float:
    lons, lats = [p[0] for p in _unwrapped(line)], [p[1] for p in line]
    return max(max(lons) - min(lons), max(lats) - min(lats))


def _unwrapped(line):
    out, offset, prev = [], 0.0, None
    for lon, lat in line:
        if prev is not None and abs(lon - prev) > 180:
            offset += 360.0 if prev > lon else -360.0
        prev = lon
        out.append((lon + offset, lat))
    return out


def test_level_for_picks_the_coarsest_level_under_half_a_dot():
    def dot(deg):  # a view whose dots are `deg` across on both axes
        return Viewport(0, 0, deg * 399, deg * 199), 200, 50

    assert [level_for(*dot(d)) for d in (0.01, 0.05, 0.19, 0.2, 0.7, 0.8, 2.0)] == [0, 0, 0, 1, 1, 2, 2]
    assert level_for(WORLD, 198, 45) == 2  # 0.9 degrees a dot at full-screen width
    assert level_for(Viewport(0, 0, 180, 90), 198, 45) == 1  # a 90 degree view
    assert level_for(Viewport(0, 0, 32, 16), 198, 45) == 0  # a 16 degree view keeps the fine line
    assert level_for(Viewport(0, 0, 10, 10), 1, 1) in (0, 1, 2)  # tiny canvases do not divide by zero


def test_no_island_a_cell_wide_is_dropped_at_any_level():
    levels = load_levels()
    fine = levels[0][1]
    for (tolerance, lines), cell_deg in ((levels[1], 2 * 0.2), (levels[2], 2 * 0.8)):  # a cell is 2 dots wide
        wanted = sum(1 for line in fine if _extent(line) >= cell_deg * 1.1)
        kept = sum(1 for line in lines if _extent(line) >= cell_deg)
        assert kept >= wanted, tolerance
    assert len(levels[2][1]) < len(fine) // 5  # and the specks below a dot are gone from the world level


def test_islands_still_light_dots_at_the_world_view():
    c = coastline_canvas(160, 40)
    for name, lon, lat in (
        ("Sri Lanka", 80.7, 7.8),
        ("Tasmania", 146.6, -42.0),
        ("Iceland", -19.0, 65.0),
        ("Cuba", -79.5, 21.8),
    ):
        x, y = project(lon, lat, WORLD, c.width, c.height)
        assert any(c.get(round(x) + dx, round(y) + dy) for dx in range(-2, 3) for dy in range(-2, 3)), name


def test_no_level_has_a_dateline_streak():
    for tolerance, lines in load_levels():
        for line in lines:
            pts = _unwrapped(line)
            for a, b in itertools.pairwise(pts):
                assert abs(a[0] - b[0]) < 60, (tolerance, a, b)  # a streak across the map would be ~360 degrees


def test_pacific_view_across_the_dateline_has_no_horizontal_streak():
    view = Viewport(120.0, -50.0, 240.0, 50.0)  # Japan to Hawaii, straddling 180
    c = coastline_canvas(120, 50, view)
    for y in range(c.height):
        run = best = 0
        for x in range(c.width):
            run = run + 1 if c.get(x, y) else 0
            best = max(best, run)
        assert best < c.width // 3, y


def test_coastline_cache_returns_private_copies():
    before = coastline_canvas(40, 12).render()
    a = coastline_canvas(40, 12)
    a.set(0, 0)
    a.set(1, 1)
    assert a.render() != before
    assert coastline_canvas(40, 12).render() == before


def test_huge_offcanvas_segment_is_clipped_fast():
    view = Viewport(10, 10, 11, 11)
    c = BrailleCanvas(20, 5)
    # ~36 million dots long at this scale; unclipped stepping would take millions of iterations.
    draw_polyline(c, [(-170, 10.5), (0, 10.5), (170, 10.5)], view)
    y = round(project(0, 10.5, view, c.width, c.height)[1])
    assert all(c.get(x, y) for x in range(c.width))
    c.clear()
    c.line(-10_000_000, 3, 10_000_000, 3)
    assert all(c.get(x, 3) for x in range(c.width))
    c.clear()
    c.line(-10_000_000, -5, 10_000_000, -9)  # misses entirely
    assert not any(c.cell_bits(i, j) for i in range(c.cols) for j in range(c.rows))


def test_full_world_render_performance():
    start = time.perf_counter()
    c = coastline_canvas(240, 60, Viewport(-179.0, -89.0, 179.0, 89.0))  # uncached viewport
    assert time.perf_counter() - start < 5.0
    assert sum(bin(c.cell_bits(i, j)).count("1") for i in range(c.cols) for j in range(c.rows)) > 2000
    start = time.perf_counter()
    coastline_canvas(240, 60, Viewport(-179.0, -89.0, 179.0, 89.0))  # cache hit
    assert time.perf_counter() - start < 0.1


def test_polyline_outside_viewport_draws_nothing():
    c = BrailleCanvas(20, 5)
    draw_polyline(c, [(100, 50), (110, 60)], Viewport(-10, -10, 10, 10))
    assert not any(c.cell_bits(i, j) for i in range(c.cols) for j in range(c.rows))


def test_known_landmarks_have_dots():
    c = coastline_canvas(160, 40)

    # Florida and Tokyo bay have coastline dots nearby; the mid Atlantic does not.
    def has_dots(lon, lat, r=1):
        x, y = project(lon, lat, WORLD, c.width, c.height)
        return any(c.get(round(x) + dx, round(y) + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1))

    assert has_dots(-80, 26)  # Florida
    assert has_dots(139.7, 35.7)  # Tokyo bay
    assert not has_dots(-40, 20, 2)  # mid Atlantic


def _check_golden(name: str, lines: list[str]) -> None:
    path = GOLDEN / name
    text = "\n".join(lines) + "\n"
    if os.environ.get("EARTHWAVE_UPDATE_GOLDEN"):
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize(("cols", "rows"), [(80, 24), (160, 40)])
def test_world_unicode_render(cols, rows):
    _check_golden(f"world_{cols}x{rows}_unicode.txt", coastline_canvas(cols, rows).render())


@pytest.mark.parametrize(("cols", "rows"), [(80, 24), (160, 40)])
def test_world_ascii_render(cols, rows):
    _check_golden(f"world_{cols}x{rows}_ascii.txt", render_ascii(coastline_canvas(cols, rows)))


def test_render_ascii_halves():
    c = BrailleCanvas(3, 1)
    c.set(0, 0)  # upper half only
    c.set(2, 3)  # lower half only
    c.set(4, 0)
    c.set(4, 3)  # both halves
    assert render_ascii(c) == ["..:"]
    assert render_ascii(BrailleCanvas(2, 1)) == ["  "]


def _connected(canvas: BrailleCanvas, a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when lit dots form an 8-connected path from dot a to dot b (searched inside their bounding box + 1)."""
    x0, x1 = min(a[0], b[0]) - 1, max(a[0], b[0]) + 1
    y0, y1 = min(a[1], b[1]) - 1, max(a[1], b[1]) + 1
    seen, todo = {a}, [a]
    while todo:
        x, y = todo.pop()
        if (x, y) == b:
            return True
        for dx, dy in itertools.product((-1, 0, 1), repeat=2):
            n = (x + dx, y + dy)
            if n not in seen and x0 <= n[0] <= x1 and y0 <= n[1] <= y1 and canvas.get(*n):
                seen.add(n)
                todo.append(n)
    return False


@pytest.mark.parametrize("size", [(198, 45), (138, 40)], ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("span", [16, 40, 90])
@pytest.mark.parametrize("place", [(138.0, 37.0), (127.5, 36.5)], ids=["japan", "korea"])
def test_coast_polylines_are_connected_dot_paths(place, span, size):
    """Every pair of consecutive vertices on screen is joined by lit, 8-connected dots at every zoom tier.
    The coast is a continuous line; it only looks dotted because braille dots sit apart in a font."""
    from earthwave.ui.widgets import braille
    from earthwave.ui.widgets.map_panel import zoom_view

    cols, rows = size
    view = zoom_view(place[0], place[1], span, cols, rows)
    canvas = coastline_canvas(cols, rows, view)
    w, h = canvas.width, canvas.height
    checked = 0
    for pts, (lo, hi, lat_lo, lat_hi) in braille._prepared_coastline(level_for(view, cols, rows)):
        if lat_hi < view.lat0 or lat_lo > view.lat1:
            continue
        for shift in (-360.0, 0.0, 360.0):
            if hi + shift < view.lon0 or lo + shift > view.lon1:
                continue
            dots = [
                (
                    round((lon + shift - view.lon0) / (view.lon1 - view.lon0) * (w - 1)),
                    round((view.lat1 - lat) / (view.lat1 - view.lat0) * (h - 1)),
                )
                for lon, lat in pts
            ]
            for a, b in itertools.pairwise(dots):
                if a != b and all(0 <= x < w and 0 <= y < h for x, y in (a, b)):
                    checked += 1
                    assert _connected(canvas, a, b), (a, b)
    assert checked > 300


def test_land_levels_match_coastline_levels():
    assert len(load_land()) == len(load_levels())
    assert all(ring[0] == ring[-1] for level in load_land() for ring in level)


def _land_dot(land: bytes, cols: int, x: int, y: int) -> bool:
    return bool(land[(y >> 2) * cols + (x >> 1)] & _BITS[x & 1][y & 3])


def test_land_dots_fill_land_and_leave_sea_and_holes():
    from earthwave.ui.widgets.map_panel import zoom_view

    cols, rows = 198, 45
    view = zoom_view(138, 37, 40, cols, rows)
    land = _land_cached(cols, rows, view)

    def at(lon, lat):
        x, y = project(lon, lat, view, cols * 2, rows * 4)
        return _land_dot(land, cols, round(x), round(y))

    assert at(138.0, 36.5)  # central Honshu
    assert not at(143.0, 33.0)  # Pacific
    assert not at(131.0, 40.0)  # Sea of Japan
    wide = zoom_view(60, 40, 90, cols, rows)
    x, y = project(51.0, 41.5, wide, cols * 2, rows * 4)  # Caspian Sea: a hole in the Eurasia ring
    assert not _land_dot(_land_cached(cols, rows, wide), cols, round(x), round(y))


@pytest.mark.parametrize(("lon", "lat", "span"), [(138, 37, 40), (-79, 21, 20), (-79, 21, 40)])  # Japan, Cuba
def test_fill_edge_and_coast_share_one_dot_grid(lon, lat, span):
    """Every land dot on the edge of the fill has a coast dot within one dot: no cell-sized gap between the two."""
    from earthwave.ui.widgets.map_panel import zoom_view

    cols, rows = 198, 45
    view = zoom_view(lon, lat, span, cols, rows)
    land, coast = _land_cached(cols, rows, view), coastline_canvas(cols, rows, view)
    width, height = cols * 2, rows * 4

    def is_land(x, y):
        return 0 <= x < width and 0 <= y < height and _land_dot(land, cols, x, y)

    edge = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if is_land(x, y)
        and any(
            0 <= x + dx < width and 0 <= y + dy < height and not is_land(x + dx, y + dy)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
    ]
    assert len(edge) > 500
    assert all(any(coast.get(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)) for x, y in edge)


def test_dot_raster_shows_coast_dots_only_in_coast_cells_and_land_dots_elsewhere():
    from earthwave.ui.widgets.map_panel import zoom_view

    cols, rows = 198, 45
    view = zoom_view(138, 37, 40, cols, rows)
    glyphs, flags = dot_raster(cols, rows, view)
    coast, land = coastline_canvas(cols, rows, view), _land_cached(cols, rows, view)
    assert dot_raster(cols, rows, view) is dot_raster(cols, rows, view)  # cached
    interior = coast_cells = 0
    for r in range(rows):
        for c in range(cols):
            bits = ord(glyphs[r][c]) - 0x2800
            if coast.cell_bits(c, r):
                coast_cells += 1
                assert bits == coast.cell_bits(c, r) and not flags[r][c]
            elif land[r * cols + c]:
                interior += 1
                assert bits == land[r * cols + c] and flags[r][c]
            else:
                assert bits == 0 and not flags[r][c]
    assert interior > 100 and coast_cells > 100


def test_land_fill_spans_the_antimeridian():
    from earthwave.ui.widgets.map_panel import zoom_view

    cols, rows = 120, 36
    view = zoom_view(179, -17, 40, cols, rows)  # Fiji straddles 180
    land = _land_cached(cols, rows, view)
    assert sum(1 for c in land if c) > 20
    width, height = cols * 2, rows * 4
    for y in range(height):  # no streak: no dot row is lit across (nearly) the whole width
        assert sum(_land_dot(land, cols, x, y) for x in range(width)) < width // 2
