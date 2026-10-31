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
        self.show(None, 0.0)

    def show(self, quake: Earthquake | None, age_seconds: float, gauge: str = "", warm: bool = True) -> None:
        """The latest major quake (or a quiet message), then any gauge anomaly in progress.

        The place is the part that gives way when the line is too long, so the age and the gauge value stay visible.
        """
        tail = Text()
        if quake is not None:
            tail.append(f" {fmt_age(age_seconds)} ago", "grey62")
        if gauge:
            tail.append("   Gauge anomaly: ", "grey62")
            tail.append(gauge, "bold red")
        if quake is None:
            text = Text(" No major quakes in the loaded data" if warm else " Waiting for USGS", style="grey50")
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
