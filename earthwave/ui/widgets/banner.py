"""Red banner across the dashboard while an actionable tsunami alert is unacknowledged."""

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import Static

from earthwave.models import TsunamiAlert


def build_banner(alerts: list[TsunamiAlert], width: int = 0) -> Text:
    """The banner line; a headline too long for `width` is shortened so '+N more' and the key hint stay readable."""
    top = alerts[0]
    head = Text(f" TSUNAMI {top.level} ", "bold white on #7f1d1d")
    tail = Text("   t details, a acknowledge", "white")
    if len(alerts) > 1:
        tail = Text(f"  (+{len(alerts) - 1} more)", "white") + tail
    headline = Text(f" {top.headline}", "bold white", no_wrap=True)
    if width:
        headline.truncate(max(width - cell_len(head.plain) - cell_len(tail.plain), 0), overflow="ellipsis")
    return Text.assemble(head, headline, tail, no_wrap=True, overflow="ellipsis")


class Banner(Static):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alerts: list[TsunamiAlert] = []

    def show(self, alerts: list[TsunamiAlert], unacked: int) -> None:
        visible = bool(alerts) and unacked > 0
        self.display = visible
        self._alerts = alerts if visible else []
        if visible:
            self.update(build_banner(alerts, self.size.width))

    def on_resize(self, event: events.Resize) -> None:
        if self._alerts:
            self.update(build_banner(self._alerts, event.size.width))
