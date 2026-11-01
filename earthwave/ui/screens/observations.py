"""Observations (`o`): the gauge strip for the quake under the cursor."""

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from earthwave.models import Earthquake
from earthwave.ui.screens.detail import GONE_NOTE
from earthwave.ui.widgets.gauge_strip import GaugeStrip
from earthwave.ui.widgets.too_small import TooSmall, sync_too_small

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp


class ObservationsScreen(Screen):
    BINDINGS = [Binding("escape,o,q", "back", "Back")]

    def __init__(self, quake: Earthquake):
        super().__init__()
        self.quake = quake

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="observations_body"):
            yield Static("", id="observations_header")
            yield GaugeStrip(id="observations_strip")
        yield Footer(compact=True, show_command_palette=False)
        yield TooSmall(id="too_small")

    def on_resize(self, event: events.Resize) -> None:
        sync_too_small(self, event)

    def on_mount(self) -> None:
        self.refresh_gauges()

    def refresh_gauges(self) -> None:
        if not self.query("#observations_header"):  # a poll can land before the screen has composed
            return
        app = self.earthwave
        found = app.store.resolve(self.quake.id)  # follows revisions and preferred-id changes
        gone = found is None
        q = self.quake = found or self.quake
        head = Text()
        head.append(f"M{q.mag:.1f} {q.place}\n", "bold")
        if q.tsunami_flag:
            head.append("Stations nearest this quake, plus any tied to active tsunami alerts.", "grey62")
        else:
            head.append("USGS has not flagged this quake for a tsunami; showing the stations already chosen.", "grey62")
        if gone:
            head.append(f"\n{GONE_NOTE}", "yellow")
        self.query_one("#observations_header", Static).update(head)
        self.query_one(GaugeStrip).show(app.gauge_rows(), app.clock.now())

    def action_back(self) -> None:
        self.earthwave.close_observations()
        self.app.pop_screen()
