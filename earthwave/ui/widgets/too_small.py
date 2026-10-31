"""Overlay shown instead of the dashboard when the terminal is too small to lay it out."""

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
