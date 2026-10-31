"""Dashboard tsunami panel: the alerts in force (or a quiet message), pulsing its border while one is unacknowledged."""

from datetime import datetime

from rich.text import Text
from textual.widgets import Static

from earthwave.models import TsunamiAlert
from earthwave.ui.labels import fmt_age
from earthwave.ui.theme import LEVEL_COLORS


def fmt_until(expires: datetime | None, now: datetime) -> str:
    if expires is None:
        return "until cancelled"
    seconds = (expires - now).total_seconds()
    return f"expires in {fmt_age(seconds)}" if seconds > 0 else "expired"


def build_panel(alerts: list[TsunamiAlert], now: datetime, max_lines: int = 8) -> Text:
    if not alerts:
        return Text("No tsunami alerts in force", style="grey50")
    text = Text(no_wrap=True, overflow="ellipsis")
    for alert in alerts[:max_lines]:
        text.append(f"{alert.level:<11}", LEVEL_COLORS[alert.level])
        text.append(f" {alert.headline} ", "white")
        text.append(fmt_until(alert.expires, now), "grey62")
        text.append("\n")
    if len(alerts) > max_lines:
        text.append(f"+{len(alerts) - max_lines} more, t for all", "grey62")
    return text


class TsunamiPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.border_title = "Tsunami"
        self.update(Text("No tsunami alerts in force", style="grey50"))

    def show(self, alerts: list[TsunamiAlert], now: datetime, pulse: bool) -> None:
        self.update(build_panel(alerts, now))
        self.set_class(pulse, "pulse")
