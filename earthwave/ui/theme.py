"""Colors shared across widgets."""

from rich.segment import Segment
from rich.style import Style
from textual.color import Color
from textual.filter import Monochrome

from earthwave.poll import Freshness

# ANSI "yellow" is not yellow on screen: Textual maps the sixteen ANSI colours through its own terminal theme (Monokai),
# where yellow is (253,151,31), orange, next to dark_orange. Anything that must read as yellow uses this truecolor.
# app.tcss defines the same value as $watch.
YELLOW = "#ffd700"


def mag_color(mag: float) -> str:
    """Magnitude bands: <3 grey, 3-4.4 cyan, 4.5-5.4 green, 5.5-6.4 yellow, 6.5-6.9 orange, 7+ red."""
    if mag < 3.0:
        return "grey62"
    if mag < 4.5:
        return "cyan"
    if mag < 5.5:
        return "green"
    if mag < 6.5:
        return YELLOW
    if mag < 7.0:
        return "dark_orange"
    return "bold red"


PAGER_COLORS = {"green": "green", "yellow": YELLOW, "orange": "dark_orange", "red": "red"}

FRESHNESS_COLORS = {
    Freshness.LOADING: "grey62",
    Freshness.FRESH: "green",
    Freshness.STALE: YELLOW,
    Freshness.ERROR: "red",
}

# Tsunami levels: Warning red, Advisory orange, Watch yellow, Threat and Information blue (what each renders as, with
# the app's ANSI theme applied, is pinned in tests/uat/test_screens.py). These colour the level word wherever it is
# printed; the banner, status bar and panel border take theirs from level-* classes in app.tcss.
LEVEL_COLORS = {
    "WARNING": "bold red",
    "ADVISORY": "dark_orange",
    "WATCH": YELLOW,
    "THREAT": "sky_blue1",
    "INFORMATION": "sky_blue1",
    "CANCELLATION": "grey62",
}


def mark_level(widget, level: str | None) -> None:
    """Give a widget the level-<name> class of the alert it is showing (none when it shows no actionable alert), so
    app.tcss can colour it from theme variables: a Warning and a Watch call for different actions."""
    for name in LEVEL_COLORS:
        widget.set_class(name == level, f"level-{name.lower()}")


def _luminance(c: Color) -> float:
    def lin(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b)


def contrast(a: Color, b: Color) -> float:
    """WCAG contrast ratio between two colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def legible_grey(fg: Color, bg: Color, minimum: float = 4.5) -> Color:
    """The grey nearest fg (lighter on a dark ground, darker on a light one) that reads on bg at the minimum ratio."""
    step = 1 if _luminance(bg) < 0.18 else -1
    grey = fg.r
    while contrast(Color(grey, grey, grey), bg) < minimum and 0 < grey < 255:
        grey += step
    return Color(grey, grey, grey)


class LegibleMonochrome(Monochrome):
    """Textual's NO_COLOR filter keeps only luminance, and ANSI red and magenta (our Warning and M7+ colours) come out
    near-black on the dark background (1.1:1). Words and numbers below 4.5:1 are lifted to the nearest grey that reads;
    borders, map dots and sparklines (no letters or digits) keep the plain conversion."""

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        out = []
        for seg in super().apply(segments, background):
            style = seg.style
            if style and style.color and not style.reverse and any(c.isalnum() for c in seg.text):
                fg = Color.from_rich_color(style.color)
                bg = Color.from_rich_color(style.bgcolor) if style.bgcolor else background
                if contrast(fg, bg) < 4.5:
                    seg = Segment(seg.text, style + Style(color=legible_grey(fg, bg).rich_color), None)
            out.append(seg)
        return out
