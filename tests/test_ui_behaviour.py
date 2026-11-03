"""Small behaviour fixes found by the cold-start UAT: sort, replay marker, quit keys, double-click, hidden map, hints."""

import pytest
from test_observations import gauge_app  # noqa: F401  (kept for parity with the other UI suites)
from test_ui import week_app
from textual import events
from textual.widgets import Static

from earthwave.poll import Freshness
from earthwave.ui.glyphs import UNICODE
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.quake_list import QuakeList
from earthwave.ui.widgets.status_bar import SourceLine, StatusInfo, build_status
from earthwave.ui.widgets.tsunami_panel import TsunamiPanel


def info(**kw):
    base = {"sources": (), "shown": 5, "total": 9, "next_poll": 30, "filter_text": "M4.5+ 7d", "unseen": 0}
    return StatusInfo(**{**base, **kw})


def test_replay_is_marked_on_the_status_bar_first():
    plain = build_status(info(replay=True), UNICODE).plain
    assert plain.startswith(" REPLAY ")
    assert "REPLAY" not in build_status(info(), UNICODE).plain


def test_failed_feeds_come_before_the_optional_fields():
    sources = tuple(SourceLine(n, Freshness.ERROR, None) for n in ("usgs", "jma", "tsunamigov", "ndbc"))
    plain = build_status(info(sources=sources, unseen=12, next_poll=50), UNICODE).plain
    assert plain.index("failed:") < plain.index("next") < len(plain)
    assert plain[:60].count("failed") == 1


async def test_sort_starts_at_the_head_of_the_new_order():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        listing = app.dashboard.query_one(QuakeList)
        for _ in range(60):
            await pilot.press("j")
        await pilot.pause()
        assert listing.cursor == 60 and listing.scroll_offset.y > 0, (
            listing.cursor,
            listing.scroll_offset,
            listing.size,
            len(listing.rows),
        )
        await pilot.press("s")
        await pilot.pause()
        assert listing.cursor == 0 and listing.scroll_offset.y == 0


async def test_replay_flag_reaches_the_status_bar():
    app = week_app(replay=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "REPLAY" in str(app.dashboard.query_one("#status").render())


@pytest.mark.parametrize("opener", ["t", "o"])
async def test_q_quits_from_the_tsunami_and_gauges_screens(opener):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press(opener)
        await pilot.pause(0.3)
        assert isinstance(app.screen, (TsunamiScreen, ObservationsScreen))
        await pilot.press("q")
        await pilot.pause(0.3)
        assert not app.is_running or app._exit


async def test_escape_then_q_typed_fast_is_back_then_quit():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause(0.3)
        app.post_message(events.Key("alt+q", "q"))  # what a terminal sends for Esc q in one burst
        await pilot.pause(0.3)
        assert app._exit
        assert not isinstance(app.screen, TsunamiScreen) or app._exit


async def test_hiding_the_map_keeps_the_tsunami_panel_on_screen():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panel = app.dashboard.query_one(TsunamiPanel)
        assert panel.region.height > 0
        await pilot.press("m")
        await pilot.pause(0.3)
        assert app.dashboard.has_class("no-map")
        assert panel.display and panel.region.height >= 3 and panel.region.width > 20


async def test_gauges_screen_says_how_to_get_a_station():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause(0.3)
        text = str(app.screen.query_one("#observations_header", Static).render())
        assert "Press Esc and pick" in text
