"""History (`h`): the saved quakes of one calendar day in the ordinary quake list, with a stats line."""

import asyncio
import logging
import sqlite3
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Static

from earthwave.models import Earthquake
from earthwave.sources import levels
from earthwave.state.history import day_bounds
from earthwave.ui.labels import fmt_time
from earthwave.ui.widgets.quake_list import QuakeList, header_text
from earthwave.ui.widgets.too_small import TooSmall, is_too_small

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

log = logging.getLogger(__name__)

REFRESH_SECONDS = 10.0  # today's list picks up quakes saved since it was opened
BIG = 5.0


def stats_text(quakes: list[Earthquake], alert_levels: dict[str, int]) -> str:
    """One line: count, largest, how many reached M5, and the tsunami alerts of the day by level (all in words)."""
    if not quakes:
        return "No saved quakes for this day"
    big = sum(1 for q in quakes if q.mag >= BIG)
    top = max(quakes, key=lambda q: q.mag)
    text = f"{len(quakes)} quakes, largest M{top.mag:.1f}, {big} at M{BIG:g} or more"
    if alert_levels:
        by_severity = sorted(alert_levels.items(), key=lambda kv: -levels.RANK.get(kv[0], -1))
        text += "; tsunami alerts: " + ", ".join(f"{n} {lvl.title()}" for lvl, n in by_severity)
    else:
        text += "; no tsunami alerts recorded"
    return text


class HistoryList(QuakeList):
    """The day's quakes. Its keys are bound here, on the focused widget, so the footer shows them: the list's own
    arrow bindings would shadow the screen's, and the dashboard's footer is too full to show Enter."""

    BINDINGS = [
        Binding("enter", "select", "Detail"),
        Binding("left", "screen.day(-1)", "Previous day"),
        Binding("right", "screen.day(1)", "Next day"),
    ]


class HistoryScreen(Screen):
    DEFAULT_CSS = """
    HistoryScreen #history_head { height: 1; background: $panel; padding: 0 1; }
    HistoryScreen #history_stats { height: 1; color: $text-muted; padding: 0 1; }
    HistoryScreen #history_header { height: 1; }
    HistoryScreen #history_list { height: 1fr; overflow-x: hidden; }
    """
    BINDINGS = [
        Binding("escape,h", "back", "Back"),
    ]

    def __init__(self):
        super().__init__()
        self.day: date | None = None

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        app = self.earthwave
        yield Static("", id="history_head")
        yield Static("", id="history_stats")
        yield Static("", id="history_header")
        yield HistoryList(app.clock, app.tracker, app.glyphs, app.config.home, id="history_list")
        yield Footer(compact=True, show_command_palette=False)
        yield TooSmall(id="too_small")

    def on_resize(self, event: events.Resize) -> None:
        small = is_too_small(event.size.width, event.size.height)
        self.set_class(small, "too-small")
        if small:
            self.query_one(TooSmall).show(event.size.width, event.size.height)
        else:  # hiding the list dropped its focus
            self.call_after_refresh(self.query_one(QuakeList).focus)

    def on_mount(self) -> None:
        self.day = self.today()
        self.query_one(QuakeList).focus()
        self.set_interval(REFRESH_SECONDS, self.refresh_day)
        self.refresh_day()

    def today(self) -> date:
        now = self.earthwave.clock.now()
        return (now if self.earthwave.config.view.time_zone == "UTC" else now.astimezone()).date()

    def refresh_day(self) -> None:
        if self.earthwave.history is not None and self.day is not None and self.query("#history_head"):
            self.run_worker(self._load(self.day), group="history", exclusive=True, exit_on_error=False)

    async def _load(self, day: date) -> None:
        """Read the day off the UI thread; a failed read shows one line and leaves the app running."""
        app, history = self.earthwave, self.earthwave.history
        zone = app.config.view.time_zone
        start, end = day_bounds(day, UTC if zone == "UTC" else None)
        quakes: list[Earthquake] = []
        stats = ""
        try:
            quakes, alerts = await asyncio.to_thread(history.day, start, end)  # type: ignore[union-attr]
            stats = stats_text(quakes, dict(alerts))
        except (sqlite3.Error, KeyError, ValueError) as exc:
            log.warning("history read failed: %r", exc)
            stats = f"History could not be read ({type(exc).__name__}); other screens are unaffected"
        if day != self.day or not self.is_attached:  # the user moved on while the read ran
            return
        self.query_one("#history_head", Static).update(
            f"History {day:%A}  {fmt_time(start, zone)} to {fmt_time(end, zone)}"
        )
        self.query_one("#history_stats", Static).update(stats)
        quake_list = self.query_one(QuakeList)
        width = quake_list.scrollable_content_region.width
        self.query_one("#history_header", Static).update(header_text(width, app.config.home.enabled))
        quake_list.set_rows(quakes)

    def action_day(self, step: int) -> None:
        if self.day is None:
            return
        day = self.day + timedelta(days=step)
        if day > self.today():
            return
        self.day = day
        quake_list = self.query_one(QuakeList)
        quake_list.set_rows([])  # a new day starts at the top, whatever the old day's cursor and scroll were
        quake_list.scroll_to(y=0, animate=False, immediate=True)
        self.refresh_day()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_quake_list_selected(self, event: QuakeList.Selected) -> None:
        self.earthwave.open_detail(event.quake)
