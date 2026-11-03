"""A poll updates the tsunami table in place: the cursor, the scroll position and the selected alert survive (#300)."""

from dataclasses import replace
from datetime import timedelta

from uat.harness import kam, kam_alert_app

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
