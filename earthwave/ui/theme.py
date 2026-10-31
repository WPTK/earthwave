"""Colors shared across widgets."""

from earthwave.poll import Freshness


def mag_color(mag: float) -> str:
    """Magnitude bands: <3 grey, 3-4.4 cyan, 4.5-5.4 green, 5.5-6.4 yellow, 6.5-6.9 orange, 7+ red."""
    if mag < 3.0:
        return "grey62"
    if mag < 4.5:
        return "cyan"
    if mag < 5.5:
        return "green"
    if mag < 6.5:
        return "yellow"
    if mag < 7.0:
        return "dark_orange"
    return "bold red"


PAGER_COLORS = {"green": "green", "yellow": "yellow", "orange": "dark_orange", "red": "red"}

FRESHNESS_COLORS = {
    Freshness.LOADING: "grey62",
    Freshness.FRESH: "green",
    Freshness.STALE: "yellow",
    Freshness.ERROR: "red",
}
