import os
import time
from pathlib import Path

import pytest

from earthwave.ui.widgets.braille import (
    WORLD,
    BrailleCanvas,
    Viewport,
    coastline_canvas,
    draw_polyline,
    load_coastline,
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
    lines = load_coastline()
    assert len(lines) > 1000
    assert sum(len(line) for line in lines) > 100_000  # linecast's 1:10m land layer has about 138k points
    for line in lines:
        assert len(line) >= 2
        for lon, lat in line:
            assert -180 <= lon <= 180 and -90 <= lat <= 90


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
