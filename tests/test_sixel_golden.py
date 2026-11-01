"""SX: the sixel map compared with a golden image taken from a real render (the pilot app at a fixed cell size).

The golden is a PPM in tests/golden/sixel (marked `-text` so git never rewrites its bytes). Regenerate it on purpose with
EARTHWAVE_UPDATE_GOLDEN=1. The comparison tolerates scattered small differences and rejects a wrong pixel row; the
mutation test proves that: the same stream with one pixel row flipped must fail, the unchanged stream must pass.
"""

import os
from pathlib import Path

import pytest
from sixel_decode import assert_matches, compare, decode, mutate_pixel_row, read_ppm, write_ppm
from test_sixel import SIXEL
from test_ui import wait_for, week_app

from earthwave.ui.widgets.map_panel import MapPanel

GOLDEN = Path(__file__).parent / "golden" / "sixel" / "map_week_160x40.golden"


async def render_stream() -> str:
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._images)
        await pilot.pause()
        return next(iter(panel._images.values()))


async def test_map_matches_the_golden_render():
    rows, width, height = decode(await render_stream())
    if os.environ.get("EARTHWAVE_UPDATE_GOLDEN") == "1":
        write_ppm(GOLDEN, rows)
    want = read_ppm(GOLDEN)
    assert (len(want[0]), len(want)) == (width, height)
    diff = assert_matches(rows, want)
    assert diff.bad == 0  # the same code on the same data draws the same pixels


@pytest.mark.parametrize("y", [0, 7, 100, 151])
async def test_one_wrong_pixel_row_fails_the_golden_compare(y):
    stream = await render_stream()
    want = read_ppm(GOLDEN)
    _, width, height = decode(stream)
    y = min(y, height - 1)
    rows, _, _ = decode(mutate_pixel_row(stream, y))
    diff = compare(rows, want)
    assert diff.bad > 0 and diff.worst_row == y, diff
    with pytest.raises(AssertionError):
        assert_matches(rows, want)  # defaults: the bad pixels are over the global limit and one row is wrong
    with pytest.raises(AssertionError, match="pixel row"):
        assert_matches(rows, want, max_bad_fraction=0.05)  # global limit loosened: the per-row limit still catches it
    assert_matches(decode(stream)[0], want)  # the unchanged stream passes


async def test_tolerance_accepts_small_scattered_differences():
    want = read_ppm(GOLDEN)
    rows = [list(r) for r in want]
    for y in range(0, len(rows), 40):  # a few pixels off by a few levels, spread over the image
        for x in range(0, len(rows[0]), 97):
            r, g, b = rows[y][x]
            rows[y][x] = (min(255, r + 5), g, b)
    assert_matches(rows, want)
    rows[3] = [(255, 255, 255)] * len(rows[3])  # one whole row white
    with pytest.raises(AssertionError):
        assert_matches(rows, want)
