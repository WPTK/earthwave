"""Dashboard tsunami panel: the alerts in force (or a quiet message), pulsing its border while one is unacknowledged."""

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from rich.text import Text
from textual.widgets import Static

from earthwave.models import TsunamiAlert
from earthwave.sources import levels
from earthwave.ui.labels import fmt_age, fmt_time
from earthwave.ui.theme import LEVEL_COLORS, YELLOW, mark_level

CANCELLED_SHOWN = timedelta(hours=6)  # how long the panel keeps saying an alert was cancelled


def recent_cancellation(
    alerts: Iterable[TsunamiAlert],
    get_alert: Callable[[str], TsunamiAlert | None],
    now: datetime,
    time_zone: str,
) -> str:
    """ "Warning cancelled <time>" for the latest cancellation product issued within CANCELLED_SHOWN, else "": a
    cancelled alert drops out of the list, and without this the panel would read the same as if none had been issued."""
    recent = [
        (a.issued, a)
        for a in alerts
        if a.level == levels.CANCELLATION and a.issued and now - a.issued <= CANCELLED_SHOWN
    ]
    if not recent:
        return ""
    when, latest = max(recent, key=lambda pair: pair[0])
    targets = [t for ref in latest.references if (t := get_alert(ref)) is not None]
    what = targets[0].level.capitalize() if targets else "Tsunami alert"
    return f"{what} cancelled {fmt_time(when, time_zone)}"


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
    cancelled: str = "",
) -> Text:
    """The alerts in force. `overdue` ids are past their expiry, awaiting a follow-up; `down` names feeds
    that are not answering and `unknown` says every one is, in which case an empty list is not an all-clear.
    `expiry` says when each alert lapses (the store's answer, which covers alerts that state no expiry).
    `cancelled` is the recent-cancellation line, shown in place of "none in force"."""
    if not alerts and checking:
        return Text("Checking tsunami feeds", style="grey50")
    if not alerts and not down:
        return Text(cancelled or "No tsunami alerts in force", style="grey50")
    text = Text(no_wrap=True, overflow="ellipsis")
    for alert in alerts[:max_lines]:
        text.append(f"{alert.level:<11}", LEVEL_COLORS[alert.level])
        if alert.id in overdue:  # the status goes first: the headline is long and the end of the line is what is cut
            text.append(" OVERDUE, awaiting follow-up", "bold red")
        else:
            text.append(f" {fmt_until(expiry(alert), now)}", "grey62")
        text.append(f"  {alert.headline}\n", "white")
    if len(alerts) > max_lines:
        text.append(f"+{len(alerts) - max_lines} more, t for all\n", "grey62")
    if not alerts and not unknown:
        text.append("No tsunami alerts received\n", "grey50")  # not "in force": a feed is not answering in full
    if down:
        if unknown:
            text.append(f"Tsunami status UNKNOWN, feeds not answering: {', '.join(down)}", "bold red")
        else:
            text.append(f"Partial: {', '.join(down)}", YELLOW)
    return text


def offline_panel() -> Text:
    return Text("Tsunami feeds not checked (offline)", style=f"bold {YELLOW}")


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
        cancelled: str = "",
    ) -> None:
        self.update(
            build_panel(
                alerts,
                now,
                overdue=overdue,
                down=down,
                unknown=unknown,
                checking=checking,
                expiry=expiry,
                cancelled=cancelled,
            )
        )
        self.set_class(pulse, "pulse")
        mark_level(self, alerts[0].level if alerts and alerts[0].level in levels.ACTIONABLE else None)  # highest first

    def show_offline(self) -> None:
        self.update(offline_panel())
        mark_level(self, None)
        self.set_class(False, "pulse")
