"""UAT: the dashboard, the too-small message and live resizing, driven through Pilot at real terminal sizes."""

import pytest

from earthwave.ui.widgets.quake_list import QuakeList
from uat.harness import screen_text, week_app


async def test_dashboard_shows_list_status_and_ticker(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        text = screen_text(app)
        top = app.dashboard.query_one(QuakeList).rows[0]
        assert app.screen is app.dashboard and len(app.screen_stack) == 2
        assert "Latest major: M6.2" in text
        assert "AGE  MAG DEPTH" in text and "PLACE" in text
        assert top.place in text  # the newest quake, whole place string, is on screen
        assert "151 shown of 151" in text and "M4.5+ 7d" in text
        assert "too small" not in text


async def test_map_and_tsunami_panel_only_where_the_width_allows(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        text = screen_text(app)
        if size[0] >= 120:
            assert "Map · world" in text and "No tsunami alerts in force" in text
            assert not app.dashboard.has_class("no-map")
        else:
            assert "Map ·" not in text and "No tsunami alerts in force" not in text
            assert app.dashboard.has_class("no-map")


async def test_list_navigation_moves_the_cursor_and_fills_the_height(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        first = quake_list.selected
        await pilot.press("j", "j")
        assert quake_list.selected is quake_list.rows[2] and quake_list.selected is not first
        await pilot.press("k")
        assert quake_list.selected is quake_list.rows[1]
        lines = [ln for ln in screen_text(app).splitlines() if "km" in ln]
        assert len(lines) >= size[1] - 8  # the list uses the rows the terminal gives it


async def test_too_small_terminal_shows_a_clear_message_and_hides_the_dashboard(small):
    app = week_app()
    async with app.run_test(size=small) as pilot:
        await pilot.pause()
        text = screen_text(app)
        assert f"Terminal too small: {small[0]}x{small[1]}" in text
        assert "earthwave needs at least 100x30." in text and "Resize the window to continue." in text
        assert "AGE  MAG" not in text and "shown of" not in text
        assert app.dashboard.has_class("too-small")


async def test_too_small_terminal_still_quits_on_q():
    app = week_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.return_code == 0


async def test_live_resize_relays_out_and_keeps_working():
    app = week_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert "Map · world" in screen_text(app)

        await pilot.resize_terminal(100, 50)
        await pilot.pause()
        text = screen_text(app)
        assert "Map ·" not in text and "151 shown of 151" in text and "AGE  MAG" in text
        assert len(text.splitlines()) > 40  # the list grew into the extra rows

        await pilot.resize_terminal(200, 50)
        await pilot.pause()
        text = screen_text(app)
        assert "Map · world" in text and "No tsunami alerts in force" in text

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        text = screen_text(app)
        assert "Terminal too small: 80x24" in text and "AGE  MAG" not in text

        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        text = screen_text(app)
        assert "Terminal too small" not in text and "Map · world" in text and "151 shown of 151" in text
        assert not app.dashboard.has_class("too-small")

        await pilot.press("z")  # screen-level keys still work after the round trip
        assert "Map · region" in screen_text(app)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: after the too-small overlay has been shown and the terminal grows back, no widget has focus "
    "(app.focused is None), so j/k and Enter on the quake list do nothing until Tab is pressed",
)
async def test_list_keeps_focus_after_a_too_small_round_trip():
    app = week_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        before = quake_list.selected
        await pilot.press("j")
        assert quake_list.selected is not before
