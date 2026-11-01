"""Top line: the most recent major event, or a quiet message."""

from rich.text import Text
from textual.widgets import Static

from earthwave.models import Earthquake
from earthwave.ui.labels import fmt_age
from earthwave.ui.theme import mag_color

MIN_PLACE = 8  # the place is shortened to fit the line, but never below this


class Ticker(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.show(None, 0.0, waiting="Waiting for USGS")

    def show(self, quake: Earthquake | None, age_seconds: float, gauge: str = "", waiting: str = "") -> None:
        """The latest major quake (or a quiet message, or `waiting`: why nothing has loaded), then any gauge anomaly.

        The place is shortened first when the line is too long; with under MIN_PLACE columns left, the end is cut.
        """
        tail = Text()
        if quake is not None:
            tail.append(f" {fmt_age(age_seconds)} ago", "grey62")
        if gauge:
            tail.append("   Gauge anomaly: ", "grey62")
            tail.append(gauge, "bold red")
        if quake is None:
            text = Text(f" {waiting}" if waiting else " No major quakes in the loaded data", style="grey50")
        else:
            text = Text(" Latest major: ", style="grey62")
            text.append(f"M{quake.mag:.1f} ", mag_color(quake.mag))
            place = Text(quake.place, "white")
            if self.size.width:
                place.truncate(max(self.size.width - text.cell_len - tail.cell_len, MIN_PLACE), overflow="ellipsis")
            text.append_text(place)
        text.append_text(tail)
        text.no_wrap = True
        text.overflow = "ellipsis"
        self.update(text)
