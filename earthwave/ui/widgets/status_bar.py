"""One-line status bar: feed health, counts, next-poll countdown, filter summary, unseen badge."""

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

from earthwave.poll import DataState, Freshness
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.labels import fmt_age, fmt_countdown
from earthwave.ui.theme import BLACK, CYAN, FRESHNESS_COLORS, INK, TEXT, WHITE, YELLOW, mark_level

ALERT_STATES = ("info", "active", "alarm")
FRESHNESS_WORDS = {Freshness.LOADING: "loading", Freshness.STALE: "stale", Freshness.ERROR: "failed"}


def feed_word(freshness: Freshness, incomplete: bool = False) -> str:
    """One word for a source's state, shared by the status bar and Help so they cannot disagree."""
    if freshness is Freshness.FRESH and not incomplete:
        return "ok"
    return FRESHNESS_WORDS.get(freshness, "incomplete")


# The data-state chip: the level word is always printed, the CVD palette's freshness colours only add to it.
DATA_STATE_COLORS = {
    DataState.LOADING: Freshness.LOADING,
    DataState.LIVE: Freshness.FRESH,
    DataState.STALE: Freshness.STALE,
    DataState.OFFLINE: Freshness.ERROR,
}


def data_chip(state: DataState, age: float | None) -> str:
    """` LIVE 8s `, ` STALE 3m `, ` OFFLINE 12m `: the word, then the age of its data (none if unknown)."""
    return (
        f" {state.value} {fmt_age(age)} " if age is not None and state is not DataState.LOADING else f" {state.value} "
    )


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
    data_state: DataState | None = None  # None: no live feeds to judge (a replay, or no source enabled)
    data_age: float | None = None  # seconds since the data the state speaks for was good


def build_status(info: StatusInfo, glyphs: Glyphs) -> Text:
    """One line that is clipped on the right, so what must not be missed comes first: the alert chip, the replay marker,
    the count, then any feed that is not healthy (#228), then the optional fields."""
    text = Text(no_wrap=True, overflow="ellipsis")
    chips: tuple[tuple[int, int], ...] = ()
    if info.alert_state in ("active", "alarm"):  # an acknowledged alert is still in force
        text.append(" TSUNAMI ", "bold reverse")
    if info.replay:
        text.append(" REPLAY ", f"bold {INK} on {YELLOW}")  # a recording, not live data (#249)
    state = DataState.OFFLINE if info.offline else info.data_state
    if state is not None and not info.replay:  # a recording says REPLAY; its feeds' health means nothing
        start = len(text.plain)
        text.append(
            data_chip(state, None if info.offline else info.data_age),
            f"bold {INK} on {FRESHNESS_COLORS[DATA_STATE_COLORS[state]]}",
        )
        chips = ((start, len(text.plain)),)
    text.append(f" {info.shown} shown of {info.total}", TEXT)
    unhealthy = [line for line in info.sources if line.freshness is not Freshness.FRESH or line.incomplete]
    groups: dict[tuple[Freshness, str], list[SourceLine]] = {}  # one phrase per state: six loading feeds fit the bar
    for line in unhealthy:
        groups.setdefault((line.freshness, feed_word(line.freshness, line.incomplete)), []).append(line)
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
        text.append(f" {info.unseen} unseen ", f"bold {INK} on {CYAN}")
    if info.offline:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append("offline, showing saved history", "grey70")
    if info.sources and not unhealthy:
        text.append(f" {glyphs.bar} ", "grey50")
        text.append(f"{glyphs.dot}", FRESHNESS_COLORS[Freshness.FRESH])
        text.append(f" {len(info.sources)} feeds ok", "grey70")
    return on_warning_ground(text, chips) if info.alert_level == "WARNING" else text


def on_warning_ground(text: Text, chips: tuple[tuple[int, int], ...] = ()) -> Text:
    """A Warning's status ground is the loudest red on screen (theme.LEVEL_SURFACES), which no light grey or cyan reads
    on (1.3 to 2:1): the words go black and the TSUNAMI chip white on black. Colour meaning in the bar (filter, feed
    dots) gives way to the alert; the words still carry it."""
    plain = Text(text.plain, style=BLACK, no_wrap=True, overflow="ellipsis")
    if text.plain.startswith(" TSUNAMI "):
        plain.stylize(f"bold {WHITE} on {BLACK}", 0, len(" TSUNAMI "))
    if (at := text.plain.find(" REPLAY ")) >= 0:
        plain.stylize(f"bold {WHITE} on {BLACK}", at, at + len(" REPLAY "))
    for at, end in chips:  # the data chip keeps a ground of its own, so a Warning cannot hide an outage
        plain.stylize(f"bold {WHITE} on {BLACK}", at, end)
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
