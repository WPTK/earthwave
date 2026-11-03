"""Polish found by the cold-start UAT: depth, columns, gauge legend, status terms, feed list, history note, toasts."""

from conftest import make_quake
from test_observations import strip_app
from test_tsunami import kam_app
from test_ui import week_app
from textual.widgets import Static

from earthwave.ui.glyphs import UNICODE
from earthwave.ui.labels import fmt_depth, fmt_hours
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.quake_list import QuakeList, header_text


def messages(app) -> list[str]:
    return [n.message for n in app._notifications]


def test_depth_never_prints_minus_zero():
    assert fmt_depth(-0.3) == "0" and fmt_depth(0.0) == "0" and fmt_depth(-1.0) == "-1" and fmt_depth(10.4) == "10"


def test_72_hours_is_not_called_3d():
    assert fmt_hours(72) == "72h" and fmt_hours(48) == "48h" and fmt_hours(168) == "7d" and fmt_hours(24) == "24h"


async def test_distance_is_ruled_off_from_the_place():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        listing = app.dashboard.query_one(QuakeList)
        assert listing._home is not None
        text = "".join(s.text for s in listing._row_strip(make_quake("x", 5.0), False, 100))
        assert f" {UNICODE.bar} " in text and text.index("km") < text.index(UNICODE.bar)
        assert header_text(100, True).cell_len == 100


async def test_help_defines_the_status_bar_and_lists_every_feed():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause(0.3)
        text = str(app.screen.query_one("#help_text", Static).render())
        for word in ("shown of", "next 4:30", "unseen", "DEPTH", "FROM HOME", "sort:time"):
            assert word in text, word
        for name in app.scheduler.statuses:
            assert name and any(label in text for label in ("USGS", "tsunami.gov", "NWS", "JMA", "DART", "CO-OPS"))
        assert text.count("loading") >= len(app.scheduler.statuses)


async def test_gauge_strip_header_gives_the_scale_and_the_max_column_lines_up():
    app = strip_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert str(app.screen.query_one("GaugeHeader .gauge-spark").render()) == "Residual 0-0.05 m"
        head = app.screen.query_one("GaugeHeader .gauge-max")
        value = app.screen.query_one("GaugeRow .gauge-max")
        assert head.region.x == value.region.x
        assert str(head.render()).lstrip() == "24 h max" and head.styles.text_align == "left"


async def test_gauges_screen_explains_residual_and_mode(clock):
    from test_observations import gauge_app

    app = gauge_app(clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause(0.5)
        legend = str(app.screen.query_one(".gauge-legend", Static).render())
        assert "a full bar is" in legend and "Mode:" in legend and "15m every 15 minutes" in legend


async def test_history_says_the_dashboard_filter_does_not_apply(tmp_path):
    from test_history import T0, HistoryScreen, ReplayClock, _app, settled

    clock = ReplayClock(T0, real=lambda: 0.0)
    app = _app(tmp_path / "h.sqlite", clock, offline=True)
    app.history.start()
    app.history.record([make_quake("q1", 3.0, minutes_ago=1)], set(), [], T0)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryScreen)
        await settled(pilot, app, lambda: screen.query_one(QuakeList).rows, reload=screen)
        assert "dashboard filter and search do not apply" in str(screen.query_one("#history_head", Static).render())


async def test_filter_and_search_say_what_they_did():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_filters()
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert "Filters unchanged" in messages(app)
        app.open_filters()
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert any(m.startswith("Showing M") for m in messages(app))
        app.set_query("japan")
        await pilot.press("slash")
        await pilot.pause(0.2)
        app.dashboard.query_one("#search").value = ""
        await pilot.press("enter")  # an empty search submitted
        await pilot.pause(0.3)
        assert "Search cleared" in messages(app) and app.view.query == ""


async def test_hiding_the_map_keeps_the_tsunami_banner_and_panel():
    app = kam_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause(0.3)
        assert app.dashboard.has_class("no-map")
        screen = "\n".join("".join(s.text for s in strip) for strip in app.screen._compositor.render_strips())
        assert "TSUNAMI WARNING" in screen and "Tsunami Warning: Off East Coast" in screen and "expires in" in screen
        assert not isinstance(app.screen, TsunamiScreen)


def test_feeds_ok_names_how_many():
    from earthwave.poll import Freshness
    from earthwave.ui.widgets.status_bar import SourceLine, StatusInfo, build_status

    sources = tuple(SourceLine(n, Freshness.FRESH, 5.0) for n in ("usgs", "jma"))
    plain = build_status(StatusInfo(sources, 5, 9, 30, "M4.5+ 72h", 0), UNICODE).plain
    assert "2 feeds ok" in plain
