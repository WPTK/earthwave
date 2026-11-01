"""Banner across the dashboard while an actionable tsunami alert is in force: red for a Warning, and it stays after
the alert is acknowledged (reading "acknowledged"), so the alert in force is never out of sight."""

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import Static

from earthwave.models import TsunamiAlert
from earthwave.ui.theme import mark_level


def build_banner(alerts: list[TsunamiAlert], width: int = 0, overdue: bool = False, acked: bool = False) -> Text:
    """The banner line; a headline too long for `width` is shortened so '+N more' and the key hint stay readable.
    `overdue`: the top alert is past its expiry, awaiting a follow-up. `acked`: nothing is left to acknowledge."""
    top = alerts[0]
    head = Text(f" TSUNAMI {top.level} ", "bold reverse")  # the level word is the cue without colour
    if overdue:
        head.append(" OVERDUE ", "bold")
    tail = Text("   acknowledged, t details" if acked else "   t details, a acknowledge")
    if len(alerts) > 1:
        tail = Text(f"  (+{len(alerts) - 1} more)") + tail
    headline = Text(f" {top.headline}", "bold", no_wrap=True)
    if width:
        headline.truncate(max(width - cell_len(head.plain) - cell_len(tail.plain), 0), overflow="ellipsis")
    return Text.assemble(head, headline, tail, no_wrap=True, overflow="ellipsis")


class Banner(Static):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alerts: list[TsunamiAlert] = []
        self._overdue = False
        self._acked = False

    def show(self, alerts: list[TsunamiAlert], unacked: int, overdue: bool = False) -> None:
        self.display = bool(alerts)
        self._alerts = alerts
        self._overdue = overdue
        self._acked = unacked == 0
        mark_level(self, alerts[0].level if alerts else None)
        if alerts:
            self.update(self._line(self.size.width))

    def _line(self, width: int) -> Text:
        return build_banner(self._alerts, width, self._overdue, self._acked)

    def on_resize(self, event: events.Resize) -> None:
        if self._alerts:
            self.update(self._line(event.size.width))
