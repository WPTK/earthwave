"""The alert states a first-time user checks on: too-small window, cancellation, acknowledgement, overdue, colour, layout."""

from datetime import UTC, datetime, timedelta

from uat.harness import kam, kam_alert_app, screen_text
from uat.test_tsunami_states import observe

from earthwave.models import Batch
from earthwave.ui.screens.tsunami import render_alert
from earthwave.ui.widgets.banner import build_banner
from earthwave.ui.widgets.gauge_strip import GaugeRowData
from earthwave.ui.widgets.tsunami_panel import build_panel, recent_cancellation

AFTER_CANCEL = datetime(2025, 7, 30, 1, 45, tzinfo=UTC)  # five minutes after the 01:40 cancellation


# --- a window too small for the dashboard still says an alert is in force ---------------------------------------------


async def test_too_small_screen_names_the_alert_in_force_and_is_not_alarm_coloured():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        text = screen_text(app)
        assert "TSUNAMI WARNING in force - enlarge the window" in text and "Terminal too small: 80x24" in text
        bg = app.dashboard.query_one("#too_small").styles.background
        assert not (bg.r > bg.g + 40 and bg.r > bg.b + 40)  # no maroon: the alert line is the one thing to read


async def test_too_small_screen_without_an_alert_says_only_that_it_is_too_small():
    app = kam_alert_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "in force" not in screen_text(app)


async def test_too_small_line_appears_when_an_alert_arrives_while_the_window_stays_small():
    app = kam_alert_app(0)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert "WATCH in force" in screen_text(app)
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(kam(1),)), app.clock)
        app.refresh_tsunami()
        await pilot.pause()
        assert "WARNING in force" in screen_text(app)


# --- a cancellation is visible on the dashboard and reads as one on the tsunami screen --------------------------------


async def test_dashboard_panel_says_the_warning_was_cancelled():
    app = kam_alert_app(0, 1, 2)
    app.clock.advance_to(AFTER_CANCEL)
    async with app.run_test(size=(120, 30)) as pilot:
        app.dashboard.refresh_view()
        await pilot.pause()
        text = screen_text(app)
        assert "Warning cancelled 2025-07-30 01:40:12 UTC" in text
        assert "No tsunami alerts in force" not in text


def test_the_cancelled_line_lapses_and_gives_way_to_an_alert_in_force():
    app = kam_alert_app(0, 1, 2)
    alerts = app.store.alerts()
    now = AFTER_CANCEL
    line = recent_cancellation(alerts, app.store.get_alert, now, "UTC")
    assert line.startswith("Warning cancelled ")
    assert recent_cancellation(alerts, app.store.get_alert, now + timedelta(hours=7), "UTC") == ""
    assert build_panel([], now, cancelled=line).plain == line
    assert build_panel([], now).plain == "No tsunami alerts in force"
    assert build_panel([kam(1)], now, cancelled=line).plain.startswith("WARNING")  # an alert in force comes first


def test_the_cancellation_product_is_titled_cancelled_with_no_duplicated_word():
    plain = render_alert(kam(2), "cancellation", "", "UTC").plain
    assert plain.startswith("Cancelled: Tsunami Warning: Off East Coast Of Kamchatka")
    assert "CANCELLATION" not in plain and "(cancellation)" not in plain
    assert render_alert(kam(1), "active", "", "UTC").plain.startswith("WARNING (active)")  # other products unchanged


async def test_tsunami_screen_table_leaves_the_status_blank_for_a_cancellation():
    app = kam_alert_app(0, 1, 2)
    app.clock.advance_to(AFTER_CANCEL)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        row = next(line for line in screen_text(app).splitlines() if "CANCELLATION" in line and "UTC" in line)
        assert "cancellation" not in row  # the level column already says it


# --- acknowledging keeps the alert in force on screen -------------------------------------------------------------------


async def test_acknowledging_keeps_a_one_line_banner_reading_acknowledged():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(120, 30)) as pilot:
        observe(app, 1)
        await pilot.pause()
        assert "a acknowledge" in screen_text(app)
        await pilot.press("a")
        await pilot.pause()
        banner = app.dashboard.query_one("#banner")
        assert banner.display and banner.size.height == 1
        assert "TSUNAMI WARNING" in screen_text(app) and "acknowledged" in screen_text(app)
        assert "a acknowledge" not in screen_text(app)
        assert "TSUNAMI" in screen_text(app).splitlines()[-2]  # the status bar still says it


# --- a Warning is the loudest thing on screen ---------------------------------------------------------------------------


async def test_warning_banner_and_status_bar_are_solid_red_and_louder_than_watch():
    async def colours(*indexes):
        app = kam_alert_app(*indexes)
        async with app.run_test(size=(120, 30)) as pilot:
            observe(app, indexes[-1])
            await pilot.pause()
            return app.dashboard.query_one("#banner").styles.background, app.dashboard.query_one(
                "#status"
            ).styles.background

    banner, status = await colours(0, 1)
    for red in (banner, status):
        assert red.r >= 200 and red.g <= 40 and red.b <= 40  # a solid red, not the theme's muted pink
    watch, _ = await colours(0)
    assert watch.r >= 200 and watch.g >= 180 and watch.b <= 40  # Watch stays yellow


# --- overdue is on the banner -------------------------------------------------------------------------------------------


def test_banner_text_says_overdue():
    assert "OVERDUE" in build_banner([kam(1)], 120, overdue=True).plain
    assert "OVERDUE" not in build_banner([kam(1)], 120).plain
    assert "acknowledged" in build_banner([kam(1)], 120, acked=True).plain


async def test_dashboard_banner_says_overdue_when_the_warning_is_past_its_expiry():
    app = kam_alert_app(0, 1)
    app.clock.advance_to(kam(1).expires + timedelta(minutes=10))
    async with app.run_test(size=(120, 30)) as pilot:
        observe(app, 1)
        await pilot.pause()
        banner_row = screen_text(app).splitlines()[1]
        assert "TSUNAMI WARNING" in banner_row and "OVERDUE" in banner_row


# --- the tsunami screen sizes its table to its rows and keeps the gauge strip clear of the footer ----------------------


async def test_tsunami_screen_gives_the_room_to_the_bulletin_and_the_whole_gauge_strip():
    for size in ((120, 30), (100, 30)):
        app = kam_alert_app(0, 1)
        now = app.clock.now()
        rows = [
            GaugeRowData(f"s{i}", f"Station {i}", 100.0 * i, "15m", now, (0.0,) * 50, 0.05, "dart") for i in range(4)
        ]
        app.gauge_rows = lambda rows=rows: rows
        async with app.run_test(size=size) as pilot:
            await pilot.press("t")
            await pilot.pause()
            screen = app.screen
            table, text, gauges = (screen.query_one(i) for i in ("#alert_table", "#alert_text_scroll", "#gauges"))
            assert table.size.height <= 4  # header and two alerts, not 40% of the screen
            assert text.size.height >= 12
            assert gauges.virtual_size.height <= gauges.size.height  # the whole strip, nothing scrolled away
            footer = screen.query_one("Footer")
            assert (
                screen.query_one("#gauge_strip").region.bottom < footer.region.y
            )  # a blank line between the last gauge row and the footer


async def test_open_search_box_shows_enter_and_escape_hints_whatever_is_typed():
    from uat.harness import week_app

    app = week_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("slash", "a", "l")
        await pilot.pause()
        assert "Enter apply, Esc cancel" in screen_text(app)
