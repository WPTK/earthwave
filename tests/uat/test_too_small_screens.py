"""UAT: every screen, not just the dashboard, says so when the terminal is below the minimum size."""

from uat.harness import kam_alert_app, screen_text, week_app

OVERLAY = "Terminal too small"


async def _check(app, pilot, key):
    await pilot.press(key)
    await pilot.pause()
    assert OVERLAY not in screen_text(app)
    screen = app.screen
    await pilot.resize_terminal(80, 24)
    await pilot.pause()
    assert OVERLAY in screen_text(app) and "80x24" in screen_text(app)
    assert app.screen is screen
    await pilot.resize_terminal(120, 40)
    await pilot.pause()
    assert OVERLAY not in screen_text(app)


async def test_tsunami_screen_shows_the_too_small_overlay():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(120, 40)) as pilot:
        await _check(app, pilot, "t")


async def test_observations_screen_shows_the_too_small_overlay():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _check(app, pilot, "o")


async def test_detail_screen_shows_the_too_small_overlay():
    app = week_app(offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _check(app, pilot, "enter")


async def test_tsunami_keys_that_act_on_the_hidden_screen_are_off():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert OVERLAY in screen_text(app)
        screen = app.screen
        before = screen.query_one("DataTable").cursor_row
        await pilot.press("j", "a")
        await pilot.pause()
        assert screen.query_one("DataTable").cursor_row == before


async def test_observations_key_does_nothing_behind_the_dashboard_overlay():
    app = week_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert app.screen is app.dashboard
