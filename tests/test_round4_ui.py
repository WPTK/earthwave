"""Round 4 UI fixes: detail of a quake the store never held, focus after a too-small round trip, the offline status
bar, the observations header and selection, the history header on resize, and the empty-list reason with a warm store."""

from dataclasses import replace
from datetime import timedelta

from conftest import make_quake
from test_observations import gauge_app
from test_ui import WEEK_NOW, mock_transport, wait_for, week_app
from uat.harness import screen_text

from earthwave.models import Batch, QuakeDetail
from earthwave.ui.screens.detail import GONE_NOTE, DetailScreen
from earthwave.ui.screens.history import HistoryScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.widgets.quake_list import QuakeList, header_text


async def test_detail_of_a_quake_older_than_the_store_loads_and_does_not_claim_usgs_dropped_it():
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    old = replace(app.store.all()[0], id="us-forty-days", detail_url=None, time=WEEK_NOW - timedelta(days=40), updated=None)
    assert app.store.resolve(old.id) is None  # what a history row older than the store's 35 days looks like
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_detail(old)  # what HistoryScreen does for the selected row
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        await wait_for(lambda: screen.detail is not None)
        await pilot.pause()
        text = screen_text(app)
        assert len(requests) >= 1 and "/detail/us-forty-days" in str(requests[0].url)
        assert GONE_NOTE not in str(screen.query_one("#detail").render()) and "no longer lists" not in text
        assert "Loading detail" not in text and "PAGER" in text
        screen._show()  # the next one-second refresh neither claims it is gone nor fetches again
        assert not screen.gone and len([r for r in requests if "/detail/" in r.url.path]) == 1


async def test_a_quake_the_store_held_and_then_dropped_is_gone_and_loads_once():
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        app.open_detail(top)
        await pilot.pause()
        screen = app.screen
        await wait_for(lambda: screen.detail is not None)
        app.store._drop(top.id)
        screen._show()
        assert screen.gone and GONE_NOTE in str(screen.query_one("#detail").render())
        assert len([r for r in requests if "/detail/" in r.url.path]) == 1


async def _round_trip(app, pilot, key, scroll_id):
    await pilot.press(key)
    await pilot.pause()
    screen = app.screen
    scroll = screen.query_one(scroll_id)
    await pilot.resize_terminal(80, 24)
    await pilot.pause()
    assert "Terminal too small" in screen_text(app)
    await pilot.resize_terminal(100, 30)
    await pilot.pause()
    await pilot.pause()
    assert "Terminal too small" not in screen_text(app)
    assert app.focused is scroll  # hiding the scroll area had dropped the focus
    return screen, scroll


async def test_detail_scrolls_by_key_after_a_too_small_round_trip():
    app = week_app(offline=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen, scroll = await _round_trip(app, pilot, "enter", "#detail_scroll")
        screen.detail = QuakeDetail(id="x", nearby_cities=tuple(f"City {n}" for n in range(40)))
        screen._show()
        await pilot.pause()
        assert scroll.max_scroll_y > 0 and scroll.scroll_y == 0
        await pilot.press("end")
        await pilot.pause()
        assert scroll.scroll_y > 0  # the scroll keys are live again


async def test_observations_scrolls_by_key_after_a_too_small_round_trip():
    app = week_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        _, scroll = await _round_trip(app, pilot, "o", "#observations_body")
        assert app.focused is scroll


async def test_offline_status_bar_says_so_once_and_lists_no_loading_feeds():
    app = week_app(offline=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        status = str(app.dashboard.query_one("#status").render())
        assert "offline, showing saved history" in status
        assert "loading" not in status and "usgs" not in status and "feeds ok" not in status


async def test_observations_header_and_stations_follow_a_quake_flagged_while_open(clock):
    app = gauge_app(clock)
    quiet = make_quake("us2", 7.4, lat=52.5, lon=160.3, time=clock.now() - timedelta(minutes=1))  # newest, unflagged
    app.store.merge(Batch("usgs", clock.now(), quakes=(quiet,), backfill=True), clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ObservationsScreen) and screen.quake.id == "us2"
        header = screen.query_one("#observations_header")
        assert "has not flagged" in str(header.render()) and app.gauge_selection.ids == ()
        flagged = replace(quiet, tsunami_flag=True, updated=quiet.updated + timedelta(minutes=2))
        app.store.merge(Batch("usgs", clock.now(), quakes=(flagged,)), clock)
        screen.refresh_gauges()
        assert "Stations nearest this quake" in str(header.render())
        assert app.gauge_selection.dart_ids  # and they are the stations the header describes


async def test_history_header_is_rebuilt_to_the_new_width_on_resize():
    app = week_app()
    app.history = None  # the screen is pushed directly below; its reads need no database
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.push_screen(HistoryScreen())
        await pilot.pause()
        screen = app.screen
        header = screen.query_one("#history_header")
        for size in ((120, 30), (160, 40), (100, 30)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            await pilot.pause()
            width = screen.query_one(QuakeList).scrollable_content_region.width
            assert str(header.render()).rstrip() == header_text(width, app.config.home.enabled).plain.rstrip(), size
            assert "FL" in str(header.render()) and "FL" in screen_text(app)


async def test_empty_list_says_usgs_is_failing_even_when_the_store_is_warm():
    app = week_app()
    app.store.merge(Batch("usgs", WEEK_NOW, quakes=()), app.clock)  # warm: a steady batch has landed
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        empty = app.dashboard.query_one("#empty")
        assert "No quakes match" in str(empty.render())  # USGS is answering: the filter emptied it
        app.scheduler.statuses["usgs"].health.failures = 3
        app.dashboard.refresh_view()
        text = str(empty.render())
        assert "USGS is not answering" in text and "No quakes match" not in text
