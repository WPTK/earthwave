"""UAT: the dashboard, the too-small message and live resizing, driven through Pilot at real terminal sizes."""

from conftest import wait_until

from earthwave.ui.widgets.map_panel import MapPanel
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
        assert "too small" not in text


async def test_status_bar_shows_counts_and_filter(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await wait_until(pilot, lambda: "Quit" in screen_text(app).splitlines()[-1], what="the footer row")
        rows = [line for line in screen_text(app).splitlines() if line.strip()]
        last = rows[-2]  # the status bar sits just above the footer
        assert "Quit" in rows[-1]
        assert "151 shown of 151" in last and "M4.5+ 7d" in last and "sort:time" in last


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
        assert "Map ·" not in text and "AGE  MAG" in text
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
        assert "Terminal too small" not in text and "Map · world" in text
        assert not app.dashboard.has_class("too-small")

        await pilot.press("z")  # screen-level keys still work after the round trip
        assert "Map · region" in screen_text(app)


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


async def test_closing_search_in_the_full_screen_map_keeps_focus_on_the_map():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press("M", "slash")
        assert app.focused is app.dashboard.query_one("#search")
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.dashboard.query_one(MapPanel)


async def test_focus_survives_the_map_hiding_on_resize():
    app = week_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        assert app.focused is app.dashboard.query_one(MapPanel)
        await pilot.resize_terminal(110, 30)
        await pilot.pause()
        await pilot.pause()
        assert app.focused is app.dashboard.query_one(QuakeList)


async def test_an_open_search_box_keeps_focus_through_a_too_small_round_trip():
    app = week_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        await pilot.pause()
        assert app.focused is app.dashboard.query_one("#search")
        await pilot.press("a")  # typed into the box, not run as the ack-all binding
        assert app.dashboard.query_one("#search").value.endswith("a")


async def test_quakes_under_a_hidden_list_are_not_marked_seen():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        assert app.dashboard.list_shown and quake_list.visible_ids()
        await pilot.press("M")
        await pilot.pause()
        assert not app.dashboard.list_shown
        app._previous_visible = set(quake_list.visible_ids())
        app._tick()
        assert app._previous_visible == set()  # the full-screen map hides the list: nothing on it can be seen
        await pilot.press("M")
        await pilot.pause()
        assert app.dashboard.list_shown
