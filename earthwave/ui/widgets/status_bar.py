"""One-line status bar: per-source freshness dots, counts, next-poll countdown, filter summary, unseen badge."""

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

from earthwave.poll import Freshness
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.labels import fmt_age, fmt_countdown
from earthwave.ui.theme import FRESHNESS_COLORS


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


def build_status(info: StatusInfo, glyphs: Glyphs) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    for line in info.sources:
        text.append(f" {glyphs.dot}", FRESHNESS_COLORS[line.freshness])
        age = f" {fmt_age(line.age)}" if line.age is not None else ""
        text.append(f" {line.name}{age}", "grey70")
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
        self.update(build_status(info, self._glyphs))
