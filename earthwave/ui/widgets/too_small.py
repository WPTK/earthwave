"""Overlay shown instead of the dashboard when the terminal is too small to lay it out."""

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.screen import Screen
from textual.widgets import Static

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp

MIN_WIDTH = 100
MIN_HEIGHT = 30


def is_too_small(width: int, height: int) -> bool:
    return width < MIN_WIDTH or height < MIN_HEIGHT


def in_force_line(app: "EarthwaveApp") -> str:
    """One plain line naming the highest actionable tsunami alert in force ("" when none), so a window too small
    to show the banner does not hide that an alert is in force."""
    active = app.store.active_alerts(app.clock, actionable_only=True)
    return f"TSUNAMI {active[0].level} in force - enlarge the window" if active else ""


class TooSmall(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)

    def show(self, width: int, height: int, alert: str = "") -> None:
        text = Text(justify="center")
        if alert:
            text.append(f"{alert}\n\n", "bold")
        text.append(
            f"Terminal too small: {width}x{height}\n\nearthwave needs at least {MIN_WIDTH}x{MIN_HEIGHT}.\n"
            "Resize the window to continue."
        )
        self.update(text)


def refresh_too_small(screen: Screen, width: int, height: int) -> None:
    """Redraw the screen's overlay (it names any alert in force, which can change while the window stays small)."""
    if screen.has_class("too-small"):
        screen.query_one(TooSmall).show(width, height, in_force_line(screen.app))  # type: ignore[arg-type]


def sync_too_small(screen: Screen, size: events.Resize) -> bool:
    """Show or hide the screen's TooSmall overlay for a resize; True while the terminal is too small."""
    small = is_too_small(size.size.width, size.size.height)
    screen.set_class(small, "too-small")
    refresh_too_small(screen, size.size.width, size.size.height)
    return small
