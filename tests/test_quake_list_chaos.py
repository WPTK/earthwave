"""Quake list rows stay exactly as wide as the list for wide, control and extreme values (#292)."""

from dataclasses import replace

import pytest
from conftest import row_strip, week_app

from earthwave.ui.widgets.quake_list import QuakeList


@pytest.mark.parametrize(
    "place",
    [
        pytest.param("東京湾 近海 の 地震 が 発生 しました 東京湾 近海 の 地震 が 発生 しました", id="cjk-long"),
        pytest.param("🌋 Mount Etna 🌋 Sicily, Italy and a lot more place text to overflow the column xx", id="emoji"),
        pytest.param("x\ty", id="tab"),
        pytest.param("a\nb", id="newline"),
    ],
)
async def test_row_width_is_cells_not_characters(place):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        strip = row_strip(app, replace(base, place=place, pager_alert="red", tsunami_flag=True))
        assert "\n" not in strip.text and "\t" not in strip.text, repr(strip.text)
        assert strip.cell_length == width, (strip.cell_length, width)
        assert strip.text.rstrip().endswith("TR"), strip.text


async def test_extreme_magnitude_keeps_columns_aligned():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        for mag in (-10.1, 10.0, 100.0):
            assert row_strip(app, replace(base, mag=mag)).cell_length == width, mag
