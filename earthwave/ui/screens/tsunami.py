"""Tsunami screen: alert table, the selected alert's text, and the gauge strip."""

from typing import TYPE_CHECKING

from rich.text import Text
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

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="tsunami_body"):
            yield DataTable(id="alert_table", cursor_type="row", zebra_stripes=False)
            with VerticalScroll(id="alert_text_scroll"):
                yield Static("", id="alert_text")
            with Vertical(id="gauges"):
                yield GaugeStrip(id="gauge_strip")
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("LEVEL", "SOURCE", "AREAS", "ISSUED", "EXPIRES", "STATUS")
        table.focus()
        self.refresh_alerts()
        self.refresh_gauges()

    def selected(self) -> TsunamiAlert | None:
        table = self.query_one(DataTable)
        return self._rows[table.cursor_row] if self._rows and table.cursor_row < len(self._rows) else None

    def refresh_alerts(self) -> None:
        app = self.earthwave
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
                SOURCE_NAMES.get(alert.source, alert.source),
                Text("; ".join(alert.areas)[:40]),
                fmt_time(alert.issued, tz) if alert.issued else "",
                "overdue"
                if status == "overdue"
                else fmt_until(app.store.expiry(alert), now)
                if status == "active"
                else "",
                Text(status, STATUS_STYLE.get(status, "grey62")),
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
            body.update(Text("No tsunami products received yet.", style="grey50"))
            return
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
        self.earthwave.tracker.ack_tsunami()
        self.earthwave.refresh_tsunami()

    def action_back(self) -> None:
        self.app.pop_screen()
