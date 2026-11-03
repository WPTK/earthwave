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

# The status colours differ in lightness as well as hue, so a red-green colour-blind reader still tells adjacent bands
# apart (worst pair under protanopia and deuteranopia: CIE76 dE >= 20, pinned in tests/test_cvd_palette.py). Same hue
# families as before: green, orange and red are lighter or darker steps of the old ANSI/256-colour names.
GREEN = "#52cc7a"
ORANGE = "#e68a00"
RED = "#f25f49"
CYAN = "#58d1eb"
THREAT_BLUE = "#87d7ff"
INFO_VIOLET = "#9682e6"


def mag_color(mag: float) -> str:
    """Magnitude bands: <3 grey, 3-4.4 cyan, 4.5-5.4 green, 5.5-6.4 yellow, 6.5-6.9 orange, 7+ red."""
    if mag < 3.0:
        return "grey62"
    if mag < 4.5:
        return CYAN
    if mag < 5.5:
        return GREEN
    if mag < 6.5:
        return YELLOW
    if mag < 7.0:
        return ORANGE
    return f"bold {RED}"


PAGER_COLORS = {"green": GREEN, "yellow": YELLOW, "orange": ORANGE, "red": RED}

FRESHNESS_COLORS = {
    Freshness.LOADING: "grey62",
    Freshness.FRESH: GREEN,
    Freshness.STALE: YELLOW,
    Freshness.ERROR: RED,
}

# Tsunami levels: Warning red, Advisory orange, Watch yellow, Threat blue, Information violet (what each renders as,
# with the app's ANSI theme applied, is pinned in tests/uat/test_screens.py). These colour the level word wherever it is
# printed; the banner, status bar and panel border take theirs from level-* classes in app.tcss.
LEVEL_COLORS = {
    "WARNING": f"bold {RED}",
    "ADVISORY": ORANGE,
    "WATCH": YELLOW,
    "THREAT": THREAT_BLUE,
    "INFORMATION": INFO_VIOLET,
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


def _carries_meaning(ch: str) -> bool:
    """A letter, digit or marker glyph: anything but a blank, a box line or block bar (U+2500-259F) or a braille dot."""
    return not ch.isspace() and not "─" <= ch <= "▟" and not "⠀" <= ch <= "⣿"


class LegibleMonochrome(Monochrome):
    """Textual's NO_COLOR filter keeps only luminance, and ANSI red and magenta (our Warning and M7+ colours) come out
    near-black on the dark background (1.1:1). Words, numbers and map markers below 4.5:1 are lifted to the nearest grey
    that reads; borders, map dots and sparklines keep the plain conversion."""

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        out = []
        for seg in super().apply(segments, background):
            style = seg.style
            if style and style.color and not style.reverse and any(_carries_meaning(c) for c in seg.text):
                fg = Color.from_rich_color(style.color)
                bg = Color.from_rich_color(style.bgcolor) if style.bgcolor else background
                if contrast(fg, bg) < 4.5:
                    seg = Segment(seg.text, style + Style(color=legible_grey(fg, bg).rich_color), None)
            out.append(seg)
        return out
