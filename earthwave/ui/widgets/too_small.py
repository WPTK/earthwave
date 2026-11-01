"""Overlay shown instead of the dashboard when the terminal is too small to lay it out."""

from textual import events
from textual.screen import Screen
from textual.widgets import Static

MIN_WIDTH = 100
MIN_HEIGHT = 30


def is_too_small(width: int, height: int) -> bool:
    return width < MIN_WIDTH or height < MIN_HEIGHT


class TooSmall(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)

    def show(self, width: int, height: int) -> None:
        self.update(
            f"Terminal too small: {width}x{height}\n\nearthwave needs at least {MIN_WIDTH}x{MIN_HEIGHT}.\n"
            "Resize the window to continue."
        )


def sync_too_small(screen: Screen, size: events.Resize) -> bool:
    """Show or hide the screen's TooSmall overlay for a resize; True while the terminal is too small."""
    small = is_too_small(size.size.width, size.size.height)
    screen.set_class(small, "too-small")
    if small:
        screen.query_one(TooSmall).show(size.size.width, size.size.height)
    return small
