"""Dashboard tsunami panel: the alerts in force (or a quiet message), pulsing its border while one is unacknowledged."""

from collections.abc import Callable
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


def build_panel(
    alerts: list[TsunamiAlert],
    now: datetime,
    max_lines: int = 8,
    overdue: frozenset[str] = frozenset(),
    down: tuple[str, ...] = (),
    unknown: bool = False,
    checking: bool = False,
    expiry: Callable[[TsunamiAlert], datetime | None] = lambda alert: alert.expires,
) -> Text:
    """The alerts in force. `overdue` ids are past their expiry, awaiting a follow-up; `down` names feeds
    that are not answering and `unknown` says every one is, in which case an empty list is not an all-clear.
    `expiry` says when each alert lapses (the store's answer, which covers alerts that state no expiry)."""
    if not alerts and checking:
        return Text("Checking tsunami feeds", style="grey50")
    if not alerts and not down:
        return Text("No tsunami alerts in force", style="grey50")
    text = Text(no_wrap=True, overflow="ellipsis")
    for alert in alerts[:max_lines]:
        text.append(f"{alert.level:<11}", LEVEL_COLORS[alert.level])
        text.append(f" {alert.headline} ", "white")
        if alert.id in overdue:
            text.append("OVERDUE, awaiting follow-up", "bold red")
        else:
            text.append(fmt_until(expiry(alert), now), "grey62")
        text.append("\n")
    if len(alerts) > max_lines:
        text.append(f"+{len(alerts) - max_lines} more, t for all\n", "grey62")
    if not alerts and not unknown:
        text.append("No tsunami alerts in force\n", "grey50")
    if down:
        if unknown:
            text.append(f"Tsunami status UNKNOWN, feeds not answering: {', '.join(down)}", "bold red")
        else:
            text.append(f"Partial: {', '.join(down)}", "yellow")
    return text


class TsunamiPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self.border_title = "Tsunami"
        self.update(Text("No tsunami alerts in force", style="grey50"))

    def show(
        self,
        alerts: list[TsunamiAlert],
        now: datetime,
        pulse: bool,
        overdue: frozenset[str] = frozenset(),
        down: tuple[str, ...] = (),
        unknown: bool = False,
        checking: bool = False,
        expiry: Callable[[TsunamiAlert], datetime | None] = lambda alert: alert.expires,
    ) -> None:
        self.update(
            build_panel(alerts, now, overdue=overdue, down=down, unknown=unknown, checking=checking, expiry=expiry)
        )
        self.set_class(pulse, "pulse")
