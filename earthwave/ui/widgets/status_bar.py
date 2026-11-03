"""One-line status bar: feed health, counts, next-poll countdown, filter summary, unseen badge."""

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

from earthwave.poll import Freshness
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.labels import fmt_age, fmt_countdown
from earthwave.ui.theme import CYAN, FRESHNESS_COLORS, mark_level

ALERT_STATES = ("info", "active", "alarm")
FRESHNESS_WORDS = {Freshness.LOADING: "loading", Freshness.STALE: "stale", Freshness.ERROR: "failed"}


def down_feeds(feeds: list[tuple[str, Freshness]]) -> tuple[str, ...]:
    """Feeds in trouble as "name word", the one wording of the dashboard panel and the tsunami screen."""
    return tuple(f"{name} {FRESHNESS_WORDS.get(fresh, 'incomplete')}" for name, fresh in feeds)


@dataclass(frozen=True, slots=True)
class SourceLine:
    name: str
    freshness: Freshness
    age: float | None  # seconds since the last good poll
    incomplete: bool = False  # answering, but the last poll skipped something


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
    alert_level: str | None = None  # the highest actionable level in force, which colours the bar
    replay: bool = False  # the data is a recording being played back
    offline: bool = False  # nothing is fetched, so the feeds have no health to report


def build_status(info: StatusInfo, glyphs: Glyphs) -> Text:
    """One line that is clipped on the right, so what must not be missed comes first: the alert chip, the replay marker,
    the count, then any feed that is not healthy (#228), then the optional fields."""
    text = Text(no_wrap=True, overflow="ellipsis")
    if info.alert_state in ("active", "alarm"):  # an acknowledged alert is still in force
        text.append(" TSUNAMI ", "bold reverse")
    if info.replay:
        text.append(" REPLAY ", "bold black on #ffd700")  # a recording, not live data (#249)
    text.append(f" {info.shown} shown of {info.total}", "white")
    unhealthy = [line for line in info.sources if line.freshness is not Freshness.FRESH or line.incomplete]
    groups: dict[tuple[Freshness, str], list[SourceLine]] = {}  # one phrase per state: six loading feeds fit the bar
    for line in unhealthy:
        groups.setdefault((line.freshness, FRESHNESS_WORDS.get(line.freshness, "incomplete")), []).append(line)
    for index, ((freshness, word), lines) in enumerate(groups.items()):
        text.append(f" {glyphs.bar} " if index == 0 else "  ", "grey50")
        text.append(f"{glyphs.dot}", FRESHNESS_COLORS[freshness])
        names = ", ".join(line.name for line in lines)
        age = f", last ok {fmt_age(lines[0].age)}" if len(lines) == 1 and lines[0].age is not None else ""
        text.append(f" {names} {word}{age}" if len(lines) == 1 else f" {word}: {names}", "grey70")
    text.append(f" {glyphs.bar} ", "grey50")
    text.append(info.filter_text, CYAN)
    text.append(f" sort:{info.sort}", "grey62")
    if info.next_poll is not None:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append(f"next {fmt_countdown(info.next_poll)}", "grey62")
    if info.unseen:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append(f" {info.unseen} unseen ", f"bold black on {CYAN}")
    if info.offline:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append("offline, showing saved history", "grey70")
    if info.sources and not unhealthy:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append(f"{glyphs.dot}", FRESHNESS_COLORS[Freshness.FRESH])
        text.append(f" {len(info.sources)} feeds ok", "grey70")
    return on_warning_ground(text) if info.alert_level == "WARNING" else text


def on_warning_ground(text: Text) -> Text:
    """A Warning's status ground is the loudest red on screen (theme.LEVEL_SURFACES), which no light grey or cyan reads
    on (1.3 to 2:1): the words go black and the TSUNAMI chip white on black. Colour meaning in the bar (filter, feed
    dots) gives way to the alert; the words still carry it."""
    plain = Text(text.plain, style="#000000", no_wrap=True, overflow="ellipsis")
    if text.plain.startswith(" TSUNAMI "):
        plain.stylize("bold #ffffff on #000000", 0, len(" TSUNAMI "))
    if (at := text.plain.find(" REPLAY ")) >= 0:
        plain.stylize("bold #ffffff on #000000", at, at + len(" REPLAY "))
    return plain


class StatusBar(Static):
    def __init__(self, glyphs: Glyphs, **kwargs):
        super().__init__("", **kwargs)
        self._glyphs = glyphs

    def show(self, info: StatusInfo) -> None:
        for state in ALERT_STATES:
            self.set_class(info.alert_state == state, f"alert-{state}")
        mark_level(self, info.alert_level)
        self.update(build_status(info, self._glyphs))
