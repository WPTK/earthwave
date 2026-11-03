"""PAGER and tsunami badges: a letter plus a colour in the list rows and the detail header (#324)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_quake_list_chaos import row_strip
from test_ui import WEEK_NOW, week_app

from earthwave.models import Earthquake
from earthwave.ui.labels import pager_badge, tsunami_badge
from earthwave.ui.screens.detail import render_detail
from earthwave.ui.theme import PAGER_COLORS, TSUNAMI_BADGE_COLOR
from earthwave.ui.widgets.quake_list import QuakeList

QUAKE = Earthquake("badge", WEEK_NOW - timedelta(hours=1), 38.0, 142.0, 7.1, place="Off Honshu, Japan")


def test_every_pager_level_has_its_own_letter_and_colour():
    badges = {level: pager_badge(level) for level in PAGER_COLORS}
    assert [b[0] for b in badges.values()] == ["G", "Y", "O", "R"]
    assert len({b[1] for b in badges.values()}) == 4
    assert pager_badge(None) == (" ", "") and pager_badge("purple") == (" ", "")


def test_the_tsunami_badge_is_a_letter_in_a_colour_no_pager_level_uses():
    letter, color = tsunami_badge(True)
    assert letter == "T" and color == TSUNAMI_BADGE_COLOR
    assert color.split()[-1] not in PAGER_COLORS.values()
    assert tsunami_badge(False) == (" ", "")


@pytest.mark.parametrize(
    "columns", [80, 120]
)  # the list is narrower than the 100-column floor only in this direct call
@pytest.mark.parametrize("home", [True, False])
async def test_badges_end_the_row_at_the_list_width(columns, home):
    app = week_app(home=home)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        for flagged in (False, True):
            for alert in (None, "green", "red"):
                quake = replace(quakes.rows[0], tsunami_flag=flagged, pager_alert=alert)
                for selected in (False, True):
                    strip = quakes._row_strip(quake, selected, columns)
                    assert strip.cell_length == columns
                    assert strip.text[-2:] == (("T" if flagged else " ") + (alert[0].upper() if alert else " "))


@pytest.mark.parametrize("columns", [100, 120])
async def test_rendered_rows_fill_the_list_and_end_in_their_badges(columns):
    app = week_app()
    async with app.run_test(size=(columns, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        width = quakes.scrollable_content_region.width
        for index, quake in enumerate(quakes.rows[:10]):
            line = quakes.render_line(index)
            assert line.cell_length == width
            assert line.text[-2:] == (("T" if quake.tsunami_flag else " ") + (quake.pager_alert or " ")[0].upper())


async def test_badge_styles_follow_the_theme_constants():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quake = replace(app.dashboard.query_one(QuakeList).rows[0], tsunami_flag=True, pager_alert="orange")
        segments = list(row_strip(app, quake))
        flag, pager = segments[-2], segments[-1]
        assert (flag.text, pager.text) == ("T", "O")
        assert flag.style.bold and flag.style.color != pager.style.color


@pytest.mark.parametrize(
    ("flagged", "alert", "expected"),
    [
        (False, None, "M7.1 Off Honshu"),
        (True, None, "M7.1 [T] Off"),
        (False, "red", "M7.1 [R] Off"),
        (True, "red", "M7.1 [T] [R] Off"),
    ],
)
def test_the_detail_header_carries_the_badges(flagged, alert, expected):
    quake = replace(QUAKE, tsunami_flag=flagged, pager_alert=alert)
    text = render_detail(quake, None, "", None, "utc", WEEK_NOW)
    assert text.plain.splitlines()[0].startswith(expected)


def test_the_detail_header_strips_control_characters_from_the_place():
    text = render_detail(replace(QUAKE, place="a\tb\nc"), None, "", None, "utc", WEEK_NOW)
    assert text.plain.splitlines()[0] == "M7.1 a b c"


def test_dashboard_snapshot_100x30(snap_compare):
    assert snap_compare(week_app(), terminal_size=(100, 30))
