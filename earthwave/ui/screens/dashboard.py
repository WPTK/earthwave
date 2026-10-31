"""Main screen: ticker, quake list, map panel, status bar, search box and the too-small overlay."""

from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, Static

from earthwave.ui.labels import filter_summary
from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList, header_text
from earthwave.ui.widgets.status_bar import SourceLine, StatusBar, StatusInfo
from earthwave.ui.widgets.ticker import Ticker
from earthwave.ui.widgets.too_small import TooSmall, is_too_small

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

MAP_MIN_WIDTH = 120  # below this the map hides itself and the list takes the full width


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("f", "filters", "Filters"),
        Binding("s", "sort", "Sort"),
        Binding("slash", "search", "Search"),
        Binding("m", "toggle_map", "Map"),
        Binding("a", "ack_all", "Ack all"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self):
        super().__init__()
        self.show_map = True

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        app = self.earthwave
        yield Ticker(id="ticker")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("", id="header")
                yield QuakeList(app.clock, app.tracker, app.glyphs, app.config.home, id="quakes")
            yield MapPanel(id="map")
        yield Input(placeholder="search place, Enter to apply, Esc to clear", id="search")
        yield StatusBar(app.glyphs, id="status")
        yield TooSmall(id="too_small")

    def on_mount(self) -> None:
        self.show_map = self.earthwave.config.view.map
        self.query_one(QuakeList).focus()
        self._layout(self.size.width, self.size.height)
        self.refresh_view()

    def on_resize(self, event: events.Resize) -> None:
        self._layout(event.size.width, event.size.height)
        self.call_after_refresh(self.refresh_view)

    def _layout(self, width: int, height: int) -> None:
        small = is_too_small(width, height)
        self.set_class(small, "too-small")
        if small:
            self.query_one(TooSmall).show(width, height)
        self.set_class(not self.show_map or width < MAP_MIN_WIDTH, "no-map")

    # --- view ---------------------------------------------------------------------------------------------------

    def refresh_view(self) -> None:
        """Recompute everything shown from the store; nothing is cached between calls."""
        app = self.earthwave
        quake_list = self.query_one(QuakeList)
        rows = app.store.visible(app.view, app.clock, app.sort)
        quake_list.set_rows(rows)
        self.query_one("#header", Static).update(header_text(quake_list.size.width, app.config.home.enabled))
        self.refresh_ticker()
        self.refresh_status(shown=len(rows))

    def refresh_ticker(self) -> None:
        app = self.earthwave
        major = [q for q in app.store.all() if q.mag >= app.config.alerts.major_magnitude]
        latest = max(major, key=lambda q: q.time, default=None)
        age = (app.clock.now() - latest.time).total_seconds() if latest else 0.0
        self.query_one(Ticker).show(latest, age)

    def refresh_status(self, shown: int | None = None) -> None:
        app = self.earthwave
        if shown is None:
            shown = len(self.query_one(QuakeList).rows)
        sched = app.scheduler
        lines = tuple(SourceLine(name, sched.freshness(name), sched.age(name)) for name in sched.statuses)
        next_poll = (
            min((sched.next_poll_in(n) for n in sched.statuses), default=None) if app.scheduler_running else None
        )
        self.query_one(StatusBar).show(
            StatusInfo(
                sources=lines,
                shown=shown,
                total=len(app.store),
                next_poll=next_poll,
                filter_text=filter_summary(app.view),
                unseen=app.tracker.unseen(),
                sort=app.sort.value,
            )
        )

    # --- actions ------------------------------------------------------------------------------------------------

    def action_filters(self) -> None:
        self.earthwave.open_filters()

    def action_sort(self) -> None:
        app = self.earthwave
        app.sort = app.sort.next(app.config.home.enabled)
        self.refresh_view()

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.display = True
        box.value = self.earthwave.view.query
        box.focus()

    def action_toggle_map(self) -> None:
        self.show_map = not self.show_map
        self._layout(self.size.width, self.size.height)
        self.call_after_refresh(self.refresh_view)

    def action_ack_all(self) -> None:
        self.earthwave.tracker.ack_all()
        self.refresh_view()

    def action_refresh_now(self) -> None:
        self.earthwave.scheduler.refresh_now()
        self.notify("Refreshing", timeout=1.5)

    def action_help(self) -> None:
        self.earthwave.open_help()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        event.stop()
        self._close_search(event.value)

    def on_key(self, event: events.Key) -> None:
        box = self.query_one("#search", Input)
        if event.key == "escape" and box.display:
            event.stop()
            self._close_search("")

    def _close_search(self, query: str) -> None:
        box = self.query_one("#search", Input)
        box.display = False
        app = self.earthwave
        app.set_query(query)
        self.query_one(QuakeList).focus()

    def on_quake_list_selected(self, event: QuakeList.Selected) -> None:
        self.earthwave.open_detail(event.quake)
