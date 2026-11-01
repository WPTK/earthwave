"""Tsunami screen: alert table, the selected alert's text, and the gauge strip."""

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
from earthwave.ui.theme import LEVEL_COLORS
from earthwave.ui.widgets.gauge_strip import GaugeStrip
from earthwave.ui.widgets.too_small import TooSmall, refresh_too_small, sync_too_small
from earthwave.ui.widgets.tsunami_panel import fmt_until

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

SOURCE_NAMES = {"tsunamigov": "tsunami.gov", "nws": "NWS", "jma": "JMA"}
STATUS_STYLE = {
    "active": "green",
    "overdue": "bold red",
    "expired": "grey50",
    "cancelled": "grey50",
    "superseded": "grey50",
    "withdrawn": "grey50",
}


def table_rows(alerts: list[TsunamiAlert], statuses: dict[str, str]) -> list[TsunamiAlert]:
    """Alerts in force first (highest level, newest), then the rest newest first."""

    def key(alert: TsunamiAlert) -> tuple:
        live = statuses[alert.id] in IN_FORCE
        issued = alert.issued.timestamp() if alert.issued else 0.0
        return (not live, -levels.RANK[alert.level] if live else 0, -issued)

    return sorted(alerts, key=key)


def render_alert(alert: TsunamiAlert, status: str, quake_line: str, time_zone: str) -> Text:
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
    if alert.expires:
        text.append(f"Expires  {fmt_time(alert.expires, time_zone)}\n", "grey70")
    text.append(f"Source   {SOURCE_NAMES.get(alert.source, alert.source)}\n", "grey70")
    if alert.areas:
        text.append(f"Areas    {'; '.join(alert.areas)}\n", "grey70")
    if quake_line:
        text.append(f"Quake    {quake_line}\n", "grey70")
    text.append("\n")
    text.append(alert.text or "(no bulletin text)")
    return text


class TsunamiScreen(Screen):
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
        table.clear()
        for alert in self._rows:
            status = statuses[alert.id]
            table.add_row(
                Text(alert.level, LEVEL_COLORS[alert.level]),
                Text("" if alert.level == levels.CANCELLATION else status, STATUS_STYLE.get(status, "grey62")),
                SOURCE_NAMES.get(alert.source, alert.source),
                fmt_time(alert.issued, tz) if alert.issued else "",
                "overdue"
                if status == "overdue"
                else fmt_until(app.store.expiry(alert), now)
                if status == "active"
                else "",
                key=alert.id,
            )
        if keep is not None:
            for index, alert in enumerate(self._rows):
                if alert.id == keep.id:
                    table.move_cursor(row=index)
        self._show_selected()

    def refresh_gauges(self) -> None:
        app = self.earthwave
        self.query_one(GaugeStrip).show(app.gauge_rows(), app.clock.now())

    def _show_selected(self) -> None:
        app = self.earthwave
        alert = self.selected()
        body = self.query_one("#alert_text", Static)
        if alert is None:
            self._shown_id = None
            body.update(Text("No tsunami products received yet.", style="grey50"))
            return
        if alert.id != self._shown_id:  # another alert starts at its level and status; a poll keeps the reader's place
            self._shown_id = alert.id
            self.query_one("#alert_text_scroll").scroll_home(animate=False)
        quake = app.store.quake_for(alert)
        quake_line = f"M{quake.mag:.1f} {quake.place} ({quake.id})" if quake else ""
        body.update(
            render_alert(alert, app.store.alert_status(alert, app.clock), quake_line, app.config.view.time_zone)
        )

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
