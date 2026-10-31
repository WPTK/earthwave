"""History (`h`): the saved quakes of one calendar day in the ordinary quake list, with a stats line."""

from datetime import date, timedelta
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from earthwave.models import Earthquake
from earthwave.state.history import day_bounds
from earthwave.ui.labels import fmt_time
from earthwave.ui.widgets.quake_list import QuakeList, header_text

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

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
        text += "; tsunami alerts: " + ", ".join(f"{n} {lvl.title()}" for lvl, n in sorted(alert_levels.items()))
    else:
        text += "; no tsunami alerts"
    return text


class HistoryScreen(Screen):
    DEFAULT_CSS = """
    HistoryScreen #history_head { height: 1; background: $panel; padding: 0 1; }
    HistoryScreen #history_stats { height: 1; color: $text-muted; padding: 0 1; }
    HistoryScreen #history_footer { height: 1; color: $text-muted; padding: 0 1; }
    HistoryScreen #history_header { height: 1; }
    HistoryScreen #history_list { height: 1fr; overflow-x: hidden; }
    """
    BINDINGS = [
        Binding("left", "day(-1)", "Previous day"),
        Binding("right", "day(1)", "Next day"),
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
        yield QuakeList(app.clock, app.tracker, app.glyphs, app.config.home, id="history_list")
        yield Static(" Left/Right day   Enter detail   Esc back", id="history_footer")

    def on_mount(self) -> None:
        self.day = self.today()
        self.query_one(QuakeList).focus()
        self.set_interval(REFRESH_SECONDS, self.refresh_day)
        self.refresh_day()

    def today(self) -> date:
        now = self.earthwave.clock.now()
        return (now if self.earthwave.config.view.time_zone == "UTC" else now.astimezone()).date()

    def refresh_day(self) -> None:
        app, history = self.earthwave, self.earthwave.history
        if history is None or self.day is None or not self.query("#history_head"):
            return
        zone = app.config.view.time_zone
        start, end = day_bounds(self.day, zone)
        quakes = history.quakes_between(start, end)
        self.query_one("#history_head", Static).update(
            f"History {self.day:%A}  {fmt_time(start, zone)} to {fmt_time(end, zone)}"
        )
        self.query_one("#history_stats", Static).update(
            stats_text(quakes, dict(history.alert_levels_between(start, end)))
        )
        quake_list = self.query_one(QuakeList)
        self.query_one("#history_header", Static).update(header_text(quake_list.size.width, app.config.home.enabled))
        quake_list.set_rows(quakes)

    def action_day(self, step: int) -> None:
        if self.day is None:
            return
        day = self.day + timedelta(days=step)
        if day > self.today():
            return
        self.day = day
        self.refresh_day()

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_quake_list_selected(self, event: QuakeList.Selected) -> None:
        self.earthwave.open_detail(event.quake)
