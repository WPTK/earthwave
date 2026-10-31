"""Top line: the most recent major event, or a quiet message."""

from rich.text import Text
from textual.widgets import Static

from earthwave.models import Earthquake
from earthwave.ui.labels import fmt_age
from earthwave.ui.theme import mag_color


class Ticker(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.show(None, 0.0)

    def show(self, quake: Earthquake | None, age_seconds: float) -> None:
        if quake is None:
            self.update(Text(" No major quakes in this window", style="grey50"))
            return
        text = Text(" Latest major: ", style="grey62")
        text.append(f"M{quake.mag:.1f}", mag_color(quake.mag))
        text.append(f" {quake.place} ", "white")
        text.append(f"{fmt_age(age_seconds)} ago", "grey62")
        self.update(text)
