"""One-line status bar: feed health, counts, next-poll countdown, filter summary, unseen badge."""

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

from earthwave.poll import Freshness
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.labels import fmt_age, fmt_countdown
from earthwave.ui.theme import FRESHNESS_COLORS

ALERT_STATES = ("info", "active", "alarm")
FRESHNESS_WORDS = {Freshness.LOADING: "loading", Freshness.STALE: "stale", Freshness.ERROR: "failed"}


@dataclass(frozen=True, slots=True)
class SourceLine:
    name: str
    freshness: Freshness
    age: float | None  # seconds since the last good poll


@dataclass(frozen=True, slots=True)
class StatusInfo:
    sources: tuple[SourceLine, ...]
    shown: int
    total: int
    next_poll: float | None
    filter_text: str
    unseen: int
    sort: str = "time"
    alert_state: str = "none"  # none | info | active | alarm


def build_status(info: StatusInfo, glyphs: Glyphs) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    if info.alert_state == "alarm":
        text.append(" TSUNAMI ", "bold white on #7f1d1d")
    unhealthy = [line for line in info.sources if line.freshness is not Freshness.FRESH]
    if info.sources and not unhealthy:
        text.append(f" {glyphs.dot}", FRESHNESS_COLORS[Freshness.FRESH])
        text.append(" feeds ok", "grey70")
    for line in unhealthy:
        age = f" {fmt_age(line.age)}" if line.age is not None else ""
        text.append(f" {glyphs.dot}", FRESHNESS_COLORS[line.freshness])
        text.append(f" {line.name} {FRESHNESS_WORDS[line.freshness]}{age}", "grey70")
    text.append(f" {glyphs.bar} ", "grey42")
    text.append(f"{info.shown} shown of {info.total}", "white")
    text.append(f" {glyphs.bar} ", "grey42")
    text.append(info.filter_text, "cyan")
    text.append(f" sort:{info.sort}", "grey62")
    if info.next_poll is not None:
        text.append(f" {glyphs.bar} ", "grey42")
        text.append(f"next {fmt_countdown(info.next_poll)}", "grey62")
    if info.unseen:
        text.append(f" {glyphs.bar} ", "grey42")
        text.append(f" {info.unseen} unseen ", "bold black on cyan")
    return text


class StatusBar(Static):
    def __init__(self, glyphs: Glyphs, **kwargs):
        super().__init__("", **kwargs)
        self._glyphs = glyphs

    def show(self, info: StatusInfo) -> None:
        for state in ALERT_STATES:
            self.set_class(info.alert_state == state, f"alert-{state}")
        self.update(build_status(info, self._glyphs))
