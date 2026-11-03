"""Every screen at 80x24, 100x30 (the smallest supported size), 120x40 and 200x60 (#326).

The sizes are checked as text: what the screen draws, cell by cell, so the design rules are assertions instead of a
picture nobody diffs. A few screens also keep an SVG at 120x40 (below) for the colours the text cannot show."""

import pytest
from conftest import footer_keys
from rich.cells import cell_len
from screen_matrix import MIN_SIZE, SCENARIOS, SIZES, render

FEEDS = ("usgs", "tsunamigov", "nws", "jma", "dart", "coops")
# What must be on screen at every supported size. Words, not glyphs: a level or a state is always printed.
MUST_SHOW = {
    "dashboard": ("Latest major", "AGE", "MAG", "DEPTH", "FROM HOME", "PLACE", "151 shown of 151", "loading", *FEEDS),
    "dashboard-warning": ("TSUNAMI WARNING", "acknowledged", "WARNING", "TSUNAMI", "1 shown of 1", *FEEDS),
    "dashboard-offline": ("TSUNAMI WARNING", " OFFLINE", *FEEDS),
    "detail": ("M5.0", "PAGER alert", "green", "ShakeMap MMI", "Event page", "esc Back"),
    "tsunami": ("LEVEL", "STATUS", "WARNING", "active", "WATCH", "superseded", "esc Back"),
    "gauges": ("Stations nearest this quake", "Residual", "South Dutch Harbor", "stale", "esc Back"),
    "help": ("Keys", "Ctrl+P", "Colors", "Esc or ? to close"),
    "filters": ("Filters", "Minimum magnitude", "Hours back", "Apply", "Cancel"),
    "sethome": ("Set home", "Location", "Name (optional)", "Save", "Cancel"),
    "palette": ("Search for commands", "Go to quake list", "Go to tsunami alerts"),
    "map": ("Map", "pan", "151 shown of 151", *FEEDS),
}
# Modal dialogs have no minimum size of their own: they are drawn whole even at 80x24 (the screens under them say
# "too small", the dialog on top stays usable).
MODALS = {"help", "filters", "sethome", "palette"}
# Dialogs the app centres on the screen.
CENTRED = {"filters": "#dialog", "sethome": "#dialog", "help": "#help_box"}

CASES = [pytest.param(name, size, id=f"{name}-{size[0]}x{size[1]}") for name in SCENARIOS for size in SIZES]


@pytest.mark.parametrize(("name", "size"), CASES)
async def test_screen_at_size(name, size):
    shot = await render(SCENARIOS[name], size)
    text = shot.text
    width, height = size
    lines = text.split("\n")
    assert len(lines) == height
    assert max(cell_len(line) for line in lines) <= width, "a line is wider than the terminal"
    if size < MIN_SIZE and name not in MODALS:
        assert "Terminal too small" in text and "needs at least 100x30" in text
        return
    assert "Terminal too small" not in text
    missing = [word for word in MUST_SHOW[name] if word not in text]
    assert not missing, f"{name} at {size}: not drawn: {missing}\n{text}"
    if name in CENTRED:
        region = shot.regions[CENTRED[name]]
        assert abs(region.x + region.width / 2 - width / 2) <= 1, f"{name} is off-centre at {size}: {region}"
        assert region.y >= 0 and region.y + region.height <= height


@pytest.mark.parametrize("size", [s for s in SIZES if s >= MIN_SIZE])
async def test_the_warning_banner_is_the_first_thing_and_the_full_width(size):
    shot = await render(SCENARIOS["dashboard-warning"], size)
    banner, status = shot.regions["#banner"], shot.regions["#status"]
    assert banner.y <= 2 and banner.width == size[0] and banner.y < status.y
    assert "TSUNAMI WARNING" in shot.text.splitlines()[banner.y]
    assert shot.backgrounds["#banner"] != shot.backgrounds["#status"]


# The colours the text cannot show: the banner's ground, the dialogs' borders, the gauge bars.
@pytest.mark.parametrize("name", ["dashboard-warning", "detail", "gauges", "sethome"])
def test_snapshot_120x40(snap_compare, name):
    scenario = SCENARIOS[name]

    async def open_screen(pilot):
        await scenario.open(pilot)
        if scenario.footer:
            await footer_keys(pilot)

    assert snap_compare(scenario.build(), terminal_size=(120, 40), run_before=open_screen)
