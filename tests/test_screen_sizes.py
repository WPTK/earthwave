"""Every screen at 80x24, 100x30 (the smallest supported size), 120x40 and 200x60 (#326).

The sizes are checked as text: what the screen draws, cell by cell, so the design rules are assertions instead of a
picture nobody diffs. A few screens also keep an SVG at 120x40 (below) for the colours the text cannot show.
tests/chaos/test_ux.py (a marked, usually skipped suite) checks the dashboard, detail and tsunami layouts at the first
three sizes too; this file adds the other screens, the Warning and failing-feed states, and the design rules."""

import re

import pytest
from conftest import footer_keys
from rich.cells import cell_len
from screen_matrix import FAILING, SCENARIOS, SIZES, render
from textual.color import Color

from earthwave.ui.theme import LEVEL_SURFACES
from earthwave.ui.widgets.too_small import is_too_small

FEEDS = ("usgs", "tsunamigov", "nws", "jma", "dart", "coops")
# What must be drawn at every supported size. A string must be on screen; a tuple must share one row, which pins the
# word to the thing it belongs to (a state beside its feed, a level beside its source), not just to the page.
MUST_SHOW = {
    "dashboard": (("AGE", "MAG", "DEPTH", "FROM HOME", "PLACE"), ("151 shown of 151", "loading:", *FEEDS)),
    "dashboard-warning": (
        ("TSUNAMI WARNING", "acknowledged"),
        ("TSUNAMI", "1 shown of 1", "loading:", *FEEDS),
        ("WARNING", "expires in"),
    ),
    "dashboard-failing": (("TSUNAMI WARNING",), ("TSUNAMI", "STALE", "failed:", *FAILING)),
    "detail": ("PAGER alert", "green", "ShakeMap MMI", "Event page", "esc Back"),
    "tsunami": (
        ("LEVEL", "STATUS", "SOURCE"),
        ("WARNING", "active", "tsunami.gov"),
        ("WATCH", "superseded"),
        "esc Back",
    ),
    "gauges": ("Stations nearest this quake", ("South Dutch Harbor", "stale"), "esc Back"),
    "help": ("Keys", "Ctrl+P", "Colors", "Esc or ? to close"),
    "filters": ("Filters", "Minimum magnitude", "Hours back", ("Apply", "Cancel")),
    "sethome": ("Set home", "Location", "Name (optional)", ("Save", "Cancel")),
    "palette": ("Search for commands", "Go to quake list", "Go to tsunami alerts"),
    "map": ("Map", "pan", ("151 shown of 151", *FEEDS)),
}
# Modal dialogs have no minimum size of their own: they are drawn whole even at 80x24 (the screens under them say
# "too small", the dialog on top stays usable).
MODALS = {"help", "filters", "sethome", "palette"}
# Dialogs the app centres on the screen.
CENTRED = {"filters": "#dialog", "sethome": "#dialog", "help": "#help_box"}

CASES = [pytest.param(name, size, id=f"{name}-{size[0]}x{size[1]}") for name in SCENARIOS for size in SIZES]
SUPPORTED = [size for size in SIZES if not is_too_small(*size)]


@pytest.mark.parametrize(("name", "size"), CASES)
async def test_screen_at_size(name, size):
    shot = await render(SCENARIOS[name], size)
    text = shot.text
    width, height = size
    lines = text.split(chr(10))
    assert len(lines) == height
    assert max(cell_len(line) for line in lines) <= width, "a line is wider than the terminal"
    if is_too_small(width, height) and name not in MODALS:
        assert "Terminal too small" in text and "needs at least 100x30" in text
        return
    assert "Terminal too small" not in text
    for need in MUST_SHOW[name]:
        words = (need,) if isinstance(need, str) else need
        assert any(all(word in line for word in words) for line in lines), f"{name} at {size}: no row has {words}"
    if name in CENTRED:
        region = shot.regions[CENTRED[name]]
        assert abs(region.x + region.width / 2 - width / 2) <= 1, f"{name} is off-centre at {size}: {region}"
        assert region.y >= 0 and region.y + region.height <= height


@pytest.mark.parametrize("size", SUPPORTED, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_failing_feeds_stay_named_beside_their_state_word(size):
    """The status bar names every failing feed after the word `failed:` on the one row, and a healthy bar does not."""
    failing = await render(SCENARIOS["dashboard-failing"], size)
    row = failing.text.splitlines()[failing.regions["#status"].y]
    failed = re.search(r"failed: ([a-z, ]+)", row)
    assert failed and set(failed.group(1).replace(" ", "").strip(",").split(",")) >= set(FAILING), row
    assert "STALE" in row
    healthy = await render(SCENARIOS["dashboard"], size)
    assert "failed:" not in healthy.text.splitlines()[healthy.regions["#status"].y]


@pytest.mark.parametrize("name", ["dashboard-warning", "dashboard-failing"])
@pytest.mark.parametrize("size", SUPPORTED, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_the_warning_banner_is_the_loudest_element(name, size):
    """A Warning's banner takes the bright level colour at the top, full width; the status bar only the dim one."""
    shot = await render(SCENARIOS[name], size)
    banner, status = shot.regions["#banner"], shot.regions["#status"]
    assert banner.y <= 2 and banner.width == size[0] and banner.y < status.y
    assert "TSUNAMI WARNING" in shot.text.splitlines()[banner.y]
    assert "level-warning" in shot.classes["#banner"] and "level-warning" in shot.classes["#status"]
    bright, dim = LEVEL_SURFACES["warning"]
    assert shot.backgrounds["#banner"].hex == Color.parse(bright).hex
    assert shot.backgrounds["#status"].hex == Color.parse(dim).hex


# The colours the text cannot show: the banner's ground, the dialogs' borders, the gauge bars.
@pytest.mark.parametrize("name", ["dashboard-warning", "detail", "gauges", "sethome"])
def test_snapshot_120x40(snap_compare, name):
    scenario = SCENARIOS[name]

    async def open_screen(pilot):
        await scenario.open(pilot)
        if scenario.footer:
            await footer_keys(pilot)

    assert snap_compare(scenario.build(), terminal_size=(120, 40), run_before=open_screen)
