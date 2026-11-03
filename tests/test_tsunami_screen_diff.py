"""A poll updates the tsunami table in place: the cursor, the scroll position and the selected alert survive (#300)."""

from dataclasses import replace
from datetime import timedelta

from uat.harness import kam, kam_alert_app, screen_text

from earthwave.models import Batch
from earthwave.ui.screens.tsunami import TsunamiScreen, table_rows
from earthwave.ui.theme import LEVEL_COLORS


def _alert(n: int, level: str = "ADVISORY", minutes: int = 360):
    base = kam(1)
    return replace(
        base,
        id=f"diff-{n:02d}",
        dedupe_key=f"diff-{n:02d}",
        level=level,
        references=(),
        issued=base.issued + timedelta(minutes=n),
        expires=base.issued + timedelta(minutes=minutes),
        updated=base.issued + timedelta(minutes=n),
    )


async def test_a_poll_keeps_the_selected_alert_and_scroll_while_a_row_is_inserted_and_a_cell_changes():
    app = kam_alert_app()
    alerts = [_alert(n, minutes=10 if n == 5 else 360) for n in range(1, 13)]
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=tuple(alerts)), app.clock)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TsunamiScreen)
        table = screen.query_one("#alert_table")
        table.move_cursor(row=2, scroll=False)
        table.scroll_to(y=1, animate=False, immediate=True)
        await pilot.pause()
        assert table.max_scroll_y >= 1, "the table must scroll for this test to mean anything"
        before = (screen.selected().id, table.scroll_y)
        status_cell = table.get_cell(alerts[4].id, list(table.columns)[1])
        assert status_cell.plain == "active"

        # A newer Warning sorts above everything; alert 5 passes its expiry and turns overdue.
        warning = _alert(20, level="WARNING")
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(warning,)), app.clock)
        app.clock.advance_to(app.clock.now() + timedelta(minutes=20))
        app.refresh_tsunami()
        await pilot.pause()

        rows = table_rows(
            list(app.store.alerts()),
            {a.id: app.store.alert_status(a, app.clock) for a in app.store.alerts()},
        )
        assert [k.value for k in sorted(table.rows, key=table.get_row_index)] == [a.id for a in rows]
        assert rows[0].id == warning.id
        assert table.get_cell(alerts[4].id, list(table.columns)[1]).plain == "overdue"
        assert screen.selected().id == before[0]
        assert table.cursor_row == 3  # one row came in above the selection
        assert table.scroll_y == before[1]
        assert table.get_row(warning.id)[0].style == LEVEL_COLORS["WARNING"]


async def _open(app, pilot):
    await pilot.press("t")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, TsunamiScreen)
    return screen, screen.query_one("#alert_table")


async def test_a_status_that_gets_longer_is_shown_whole():
    app = kam_alert_app()
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(_alert(1, minutes=10),)), app.clock)
    async with app.run_test(size=(120, 30)) as pilot:
        await _open(app, pilot)
        assert "active" in screen_text(app) and "overdue" not in screen_text(app)
        app.clock.advance_to(app.clock.now() + timedelta(minutes=20))
        app.refresh_tsunami()
        await pilot.pause()
        assert "overdue" in screen_text(app).split("LEVEL", 1)[1]  # whole word, not clipped to "overdu"


async def test_a_level_arriving_later_and_closed_statuses_are_shown_whole():
    app = kam_alert_app(0)
    async with app.run_test(size=(120, 30)) as pilot:
        await _open(app, pilot)
        info = _alert(30, level="INFORMATION")
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(info,)), app.clock)
        app.refresh_tsunami()
        await pilot.pause()
        assert "INFORMATION" in screen_text(app)
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(kam(1),)), app.clock)  # supersedes the Watch
        app.refresh_tsunami()
        await pilot.pause()
        assert "superseded" in screen_text(app)
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(kam(2),)), app.clock)  # cancels the Warning
        app.refresh_tsunami()
        await pilot.pause()
        text = screen_text(app)
        assert "cancelled" in text and "CANCELLATION" in text


async def test_rows_that_go_away_and_a_table_that_empties_and_refills():
    app = kam_alert_app()
    alerts = [_alert(n) for n in range(1, 6)]
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=tuple(alerts)), app.clock)
    async with app.run_test(size=(120, 30)) as pilot:
        screen, table = await _open(app, pilot)
        shown = list(app.store.alerts())

        def poll(subset):
            app.store.alerts = lambda: iter(subset)
            app.refresh_tsunami()

        def order():
            return [k.value for k in sorted(table.rows, key=table.get_row_index)]

        poll(shown[:2])
        await pilot.pause()
        assert table.row_count == 2 and order() == [a.id for a in screen._rows]
        poll([])
        await pilot.pause()
        assert table.row_count == 0 and screen.selected() is None
        poll(shown)
        await pilot.pause()
        assert order() == [a.id for a in screen._rows] and len(order()) == 5
        assert screen.selected() is not None


def test_a_poll_before_the_screen_is_ready_does_nothing():
    TsunamiScreen().refresh_alerts()  # no app, nothing composed: returns before touching either


async def test_the_selected_row_stays_in_view_when_an_insert_above_pushes_it_down():
    app = kam_alert_app()
    alerts = [_alert(n) for n in range(1, 21)]
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=tuple(alerts)), app.clock)
    async with app.run_test(size=(120, 30)) as pilot:
        screen, table = await _open(app, pilot)
        last_visible = int(table.scroll_y) + table.scrollable_content_region.height - 2  # minus the header row
        table.move_cursor(row=last_visible)
        await pilot.pause()
        keep = screen.selected().id
        app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(_alert(40, level="WARNING"),)), app.clock)
        app.refresh_tsunami()
        await pilot.pause()
        assert screen.selected().id == keep
        top = int(table.scroll_y)
        assert top <= table.cursor_row < top + table.scrollable_content_region.height - 1
