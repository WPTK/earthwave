"""Tsunami screen: alert table, the selected alert's text, and the gauge strip."""

from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from earthwave.models import TsunamiAlert
from earthwave.sources import levels
from earthwave.state.store import IN_FORCE
from earthwave.ui.labels import fmt_time
from earthwave.ui.theme import GREEN, LEVEL_COLORS, RED, YELLOW
from earthwave.ui.widgets.gauge_strip import GaugeStrip
from earthwave.ui.widgets.status_bar import down_feeds
from earthwave.ui.widgets.too_small import TooSmall, refresh_too_small, sync_too_small
from earthwave.ui.widgets.tsunami_panel import OFFLINE_LINE, feed_line, fmt_until

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

SOURCE_NAMES = {"tsunamigov": "tsunami.gov", "nws": "NWS", "jma": "JMA"}
STATUS_STYLE = {
    "active": GREEN,
    "overdue": f"bold {RED}",
    "expired": "grey62",
    "cancelled": "grey62",
    "superseded": "grey62",
    "withdrawn": "grey62",
}


def _cell_key(cell: object) -> object:
    """What a cell shows: Rich's Text equality ignores its base style, which is where the colour is."""
    return (cell.plain, str(cell.style)) if isinstance(cell, Text) else cell


def table_rows(alerts: list[TsunamiAlert], statuses: dict[str, str]) -> list[TsunamiAlert]:
    """Alerts in force first (highest level, newest), then the rest newest first."""

    def key(alert: TsunamiAlert) -> tuple:
        live = statuses[alert.id] in IN_FORCE
        issued = alert.issued.timestamp() if alert.issued else 0.0
        return (not live, -levels.RANK[alert.level] if live else 0, -issued)

    return sorted(alerts, key=key)


def render_alert(
    alert: TsunamiAlert, status: str, quake_line: str, time_zone: str, expires: datetime | None = None
) -> Text:
    """`expires` is the store's believed expiry for a product that states one (the feed's own can be impossible)."""
    expires = expires or alert.expires
    text = Text()
    if alert.level == levels.CANCELLATION:  # the headline is the cancelled alert's: the level and status say no more
        text.append("Cancelled: ", LEVEL_COLORS[alert.level])
        text.append(f"{alert.headline}\n\n", "bold")
    else:
        text.append(f"{alert.level} ", LEVEL_COLORS[alert.level])
        text.append(f"({status})\n", STATUS_STYLE.get(status, "grey62"))
        text.append(f"{alert.headline}\n\n", "bold")
    if alert.issued:
        text.append(f"Issued   {fmt_time(alert.issued, time_zone)}\n", "grey70")
    if expires:
        text.append(f"Expires  {fmt_time(expires, time_zone)}\n", "grey70")
    text.append(f"Source   {SOURCE_NAMES.get(alert.source, alert.source)}\n", "grey70")
    if alert.areas:
        text.append(f"Areas    {'; '.join(alert.areas)}\n", "grey70")
    if quake_line:
        text.append(f"Quake    {quake_line}\n", "grey70")
    text.append("\n")
    kept = [line for line in alert.text.splitlines() if line.strip() != "ZCZC"]  # the start-of-message marker
    text.append("\n".join(kept).lstrip("\n") or "(no bulletin text)")
    return text


class TsunamiScreen(Screen):
    DEFAULT_CSS = """
    TsunamiScreen #gauges { border-top: solid $border; }  /* a rule sets the strip off from the bulletin */
    """

    BINDINGS = [
        Binding("escape,t,q", "back", "Back"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("a", "ack", "Ack tsunami"),
    ]

    def __init__(self):
        super().__init__()
        self._rows: list[TsunamiAlert] = []
        self._ready = False  # the table and its columns exist only once the screen has composed and mounted
        self._shown_id: str | None = None  # the alert whose text is in the bulletin pane

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="tsunami_body"):
            yield DataTable(id="alert_table", cursor_type="row", zebra_stripes=False)
            with VerticalScroll(id="alert_text_scroll"):
                yield Static("", id="alert_text")
            with VerticalScroll(id="gauges"):
                yield GaugeStrip(id="gauge_strip")
        yield Footer(compact=True, show_command_palette=False)
        yield TooSmall(id="too_small")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Under the too-small overlay the keys that act on the hidden screen are off."""
        del parameters
        return not (self.has_class("too-small") and action in ("cursor_down", "cursor_up", "ack"))

    def on_resize(self, event: events.Resize) -> None:
        if not sync_too_small(self, event) and self._ready:  # hiding the table dropped its focus
            self.call_after_refresh(self.query_one(DataTable).focus)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("LEVEL", "STATUS", "SOURCE", "ISSUED", "EXPIRES")  # the areas are in the text below
        table.focus()
        self._ready = True
        self.refresh_alerts()
        self.refresh_gauges()

    def selected(self) -> TsunamiAlert | None:
        table = self.query_one(DataTable)
        return self._rows[table.cursor_row] if self._rows and table.cursor_row < len(self._rows) else None

    def refresh_alerts(self) -> None:
        if not self._ready:  # a poll can land right after the screen is pushed, before it has composed
            return
        app = self.earthwave
        refresh_too_small(self, self.size.width, self.size.height)  # an alert can arrive while the window stays small
        table = self.query_one(DataTable)
        keep = self.selected()
        alerts = list(app.store.alerts())
        statuses = {a.id: app.store.alert_status(a, app.clock) for a in alerts}
        self._rows = table_rows(alerts, statuses)
        now = app.clock.now()
        tz = app.config.view.time_zone
        scroll_y = table.scroll_y
        wanted = {}
        for alert in self._rows:
            status = statuses[alert.id]
            wanted[alert.id] = (
                Text(alert.level, LEVEL_COLORS[alert.level]),
                Text("" if alert.level == levels.CANCELLATION else status, STATUS_STYLE.get(status, "grey62")),
                SOURCE_NAMES.get(alert.source, alert.source),
                fmt_time(alert.issued, tz) if alert.issued else "",
                "overdue"
                if status == "overdue"
                else fmt_until(app.store.expiry(alert), now)
                if status == "active"
                else fmt_time(
                    app.store.expiry(alert), tz
                )  # what the detail below shows, so a closed row keeps its date
                if alert.expires and alert.level != levels.CANCELLATION
                else "",
            )
        # Diff by alert id so a poll keeps the cursor, the scroll position and the reader's place (clear() lost them).
        gone = [k.value for k in table.rows if k.value not in wanted]
        for key in gone:
            table.remove_row(key)
        columns = list(table.columns)
        for alert_id, cells in wanted.items():
            if alert_id not in table.rows:
                table.add_row(*cells, key=alert_id)
                continue
            for column, new in zip(columns, cells, strict=True):  # ColumnKey objects
                if _cell_key(table.get_cell(alert_id, column)) != _cell_key(new):
                    table.update_cell(alert_id, column, new, update_width=True)
        if gone and table.row_count:  # a removal never narrows a column: re-measure each one through a surviving cell
            first = next(iter(table.rows))
            for column, cell in zip(columns, table.get_row(first), strict=True):
                table.update_cell(first, column, cell, update_width=True)
        order = [k.value for k in sorted(table.rows, key=lambda k: table.get_row_index(k))]
        if order != [a.id for a in self._rows]:
            # sort() hands its key only the cell values; the level cell object is unique to its row.
            place = {id(table.get_row(a.id)[0]): index for index, a in enumerate(self._rows)}
            table.sort(key=lambda cells: place[id(cells[0])])
        table.scroll_to(y=scroll_y, animate=False, immediate=True)
        if keep is not None and keep.id in wanted:  # scrolls only if the row shifted out of view
            table.move_cursor(row=table.get_row_index(keep.id), animate=False)
        self._show_selected()

    def refresh_gauges(self) -> None:
        app = self.earthwave
        self.query_one(GaugeStrip).show(app.gauge_rows(), app.clock.now())

    def _feed_note(self) -> Text | None:
        """The state of the feeds, when it qualifies an empty or short list: offline, or tsunami feeds failing."""
        app = self.earthwave
        if app.offline:  # nothing was fetched, so an empty table says nothing
            return Text(OFFLINE_LINE, style=f"bold {YELLOW}")
        feeds, none_fresh = app.tsunami_feeds_down()
        if not feeds:
            return None
        down = down_feeds(feeds)
        return feed_line(down, none_fresh)

    def _show_selected(self) -> None:
        app = self.earthwave
        alert = self.selected()
        body = self.query_one("#alert_text", Static)
        note = self._feed_note()
        head = Text()
        if note is not None:
            head.append_text(note)
            head.append("\n\n")
        if alert is None:
            self._shown_id = None
            if app.offline:
                empty = "Nothing was requested this run, so no tsunami products are known."
            elif note is not None:
                empty = "No tsunami products received."
            else:
                empty = "No tsunami products received yet."
            head.append(empty, style="grey62")
            body.update(head)
            return
        if alert.id != self._shown_id:  # another alert starts at its level and status; a poll keeps the reader's place
            self._shown_id = alert.id
            self.query_one("#alert_text_scroll").scroll_home(animate=False)
        quake = app.store.quake_for(alert)
        quake_line = f"M{quake.mag:.1f} {quake.place} ({quake.id})" if quake else ""
        head.append_text(
            render_alert(
                alert,
                app.store.alert_status(alert, app.clock),
                quake_line,
                app.config.view.time_zone,
                app.store.expiry(alert) if alert.expires else None,
            )
        )
        body.update(head)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_selected()

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_ack(self) -> None:
        tracker = self.earthwave.tracker
        count = tracker.tsunami_unacked()
        tracker.ack_tsunami()
        self.earthwave.refresh_tsunami()
        self.notify(
            f"Acknowledged {count} tsunami alert{'s' if count != 1 else ''}"
            if count
            else "No unacknowledged tsunami alerts",
            timeout=2,
        )

    def action_back(self) -> None:
        self.app.pop_screen()
