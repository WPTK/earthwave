"""Red banner across the dashboard while an actionable tsunami alert is unacknowledged."""

from rich.text import Text
from textual.widgets import Static

from earthwave.models import TsunamiAlert


def build_banner(alerts: list[TsunamiAlert]) -> Text:
    top = alerts[0]
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f" TSUNAMI {top.level} ", "bold white on #7f1d1d")
    text.append(f" {top.headline}", "bold white")
    if len(alerts) > 1:
        text.append(f"  (+{len(alerts) - 1} more)", "white")
    text.append("   t details, a acknowledge", "white")
    return text


class Banner(Static):
    def show(self, alerts: list[TsunamiAlert], unacked: int) -> None:
        visible = bool(alerts) and unacked > 0
        self.display = visible
        if visible:
            self.update(build_banner(alerts))
