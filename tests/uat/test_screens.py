"""UAT: map zooms, quake detail, the filters modal and the help screen, driven with real keys at each size."""

import pytest
from test_ui import mock_transport, wait_for

from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HelpScreen
from earthwave.ui.widgets.quake_list import QuakeList
from uat.harness import kam_alert_app, screen_text, week_app


async def test_map_zoom_cycles_world_wide_region_close_world(size):
    if size[0] < 120:
        pytest.skip("the map column is hidden below 120 columns; covered by test_full_screen_map_below_120")
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        seen = []
        for _ in range(5):
            seen.append(next(t for t in ("world", "wide", "region", "close") if f"Map · {t}" in screen_text(app)))
            await pilot.press("z")
            await pilot.pause()
        assert seen == ["world", "wide", "region", "close", "world"]


async def test_region_zoom_follows_the_cursor(size):
    if size[0] < 120:
        pytest.skip("map hidden below 120 columns")
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("z", "z")
        await pilot.pause()
        first = screen_text(app)
        await pilot.press("j", "j", "j", "j", "j", "j", "j", "j", "j", "j")
        await pilot.pause()
        second = screen_text(app)
        assert "Map · region" in first and "Map · region" in second
        map_cols = size[0] - 62
        grid = lambda text: [line[-map_cols:] for line in text.splitlines()[2:20]]  # noqa: E731
        assert grid(first) != grid(second)  # the region recentres on the newly selected quake


@pytest.mark.parametrize("width,height", [(100, 30), (100, 50)])
async def test_full_screen_map_below_120(width, height):
    app = week_app()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        assert "Map ·" not in screen_text(app)
        await pilot.press("M")
        await pilot.pause()
        text = screen_text(app)
        assert "Map · world" in text and "AGE  MAG" not in text
        await pilot.press("M")
        await pilot.pause()
        assert "AGE  MAG" in screen_text(app) and "Map ·" not in screen_text(app)


async def test_detail_opens_loads_and_returns(size):
    app = week_app(transport=mock_transport([]))
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen) and len(app.screen_stack) == 3
        await wait_for(lambda: app.screen.detail is not None)
        await pilot.pause()
        text = screen_text(app)
        assert f"M{top.mag:.1f}" in text and "PAGER alert" in text and "ShakeMap MMI" in text
        assert "Loading" not in text
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_filters_modal_opens_validates_applies_and_cancels(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FiltersScreen)
        text = screen_text(app)
        assert "Filters" in text and "Minimum magnitude" in text and "Hours back" in text
        assert "Apply" in text and "Cancel" in text
        app.screen.query_one("#min_mag").value = "abc"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, FiltersScreen) and "Minimum magnitude must be a number" in screen_text(app)
        app.screen.query_one("#min_mag").value = "6.0"
        app.screen.query_one("#hours").value = "168"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is app.dashboard
        rows = app.dashboard.query_one(QuakeList).rows
        assert rows and all(q.mag >= 6.0 for q in rows)
        text = screen_text(app)
        assert len(rows) < 151 and app.view.hours == 168
        await pilot.press("f", "escape")
        await pilot.pause()
        assert app.screen is app.dashboard and app.view.min_magnitude == 6.0


async def test_help_screen_lists_keys_and_closes_with_both_keys(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        text = screen_text(app)
        for needle in ("Keys", "zoom map", "Colors", "Data", "USGS", "Natural Earth"):
            assert needle in text, needle
        assert "Esc or ? to close" in text
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen is app.dashboard
        await pilot.press("question_mark", "escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_tsunami_screen_fits_without_a_sideways_scroll():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        table = app.screen.query_one("#alert_table")
        assert table.max_scroll_x == 0  # STATUS, EXPIRES and the rest are all on screen
        assert "AREAS" not in screen_text(app)  # the areas are in the text below, not cut off in a cell


def _hue_name(red: int, green: int, blue: int) -> str:
    import colorsys

    hue = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)[0] * 360
    return "red" if hue < 20 or hue >= 320 else "orange" if hue < 42 else "yellow" if hue < 70 else "blue"


def _hue(app, style: str) -> str:
    """The plain colour word for a Rich style, read off the colour the app renders it in: Textual maps the sixteen
    ANSI colours through its own terminal theme (ANSI yellow is orange there), so Rich's default theme is not it."""
    from rich.style import Style

    return _hue_name(*Style.parse(style).color.get_truecolor(app.ansi_theme))


def test_help_names_the_colours_it_shows():
    from earthwave.ui.screens.help import HELP_TEXT
    from earthwave.ui.theme import LEVEL_COLORS, mag_color

    app = week_app()
    for level in ("WARNING", "ADVISORY", "WATCH"):  # what the level word really renders in, not a literal
        assert f"{level.title()} {_hue(app, LEVEL_COLORS[level])}" in HELP_TEXT, level
    assert len({_hue(app, LEVEL_COLORS[level]) for level in ("WARNING", "ADVISORY", "WATCH")}) == 3  # told apart
    for mag, word in [(6.0, "yellow"), (6.7, "orange"), (7.5, "red")]:  # the bands the help names by hue
        assert _hue(app, mag_color(mag)) == word, mag
    assert "yellow <6.5, orange <7, red 7+" in HELP_TEXT


async def test_each_tsunami_level_renders_a_different_colour_everywhere():
    """Read off the rendered screen and the widgets' computed styles: a Warning, an Advisory and a Watch call for
    different actions, so the panel word, banner, status bar and panel border differ between them."""
    from dataclasses import replace

    from earthwave.models import Batch
    from earthwave.state.events import TsunamiIssued
    from uat.harness import kam, rendered_fill

    shown: dict[str, dict[str, str]] = {}
    for level in ("WARNING", "ADVISORY", "WATCH"):
        app = kam_alert_app(quake=False)
        alert = replace(kam(1), level=level)
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(alert,)), app.clock)
        async with app.run_test(size=(120, 30)) as pilot:
            app.tracker.observe([TsunamiIssued(alert)])
            app.sync_tsunami()
            app.dashboard.refresh_view()
            await pilot.pause()
            banner, status, panel = (app.dashboard.query_one(i) for i in ("#banner", "#status", "#tsunami"))
            assert banner.display
            shown[level] = {
                "word": rendered_fill(app, level),
                "banner": banner.styles.background.hex,
                "status": status.styles.background.hex,
                "border": panel.styles.border_top[1].hex,
            }
    for part in ("word", "banner", "status", "border"):
        colours = {level: shown[level][part] for level in shown}
        assert len(set(colours.values())) == 3, (part, colours)
    from rich.color import Color

    hues = {level: _hue_name(*Color.parse(shown[level]["word"]).get_truecolor()) for level in shown}
    assert hues == {"WARNING": "red", "ADVISORY": "orange", "WATCH": "yellow"}
