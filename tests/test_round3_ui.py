"""Round 3 UI fixes: no-map layout, empty-list wording, detail and observations following USGS revisions,
tsunami bulletin scroll, and the level classes that colour the banner, status bar and panel."""

import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from test_ui import WEEK_NOW, mock_transport, wait_for, week_app
from uat.harness import kam_alert_app, screen_text
from uat.test_tsunami_states import observe

from earthwave.models import Batch, QuakeDetail
from earthwave.ui.screens.detail import GONE_NOTE, DetailScreen, render_detail
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.quake_list import QuakeList


async def test_hiding_the_map_gives_the_list_the_whole_width():
    app = week_app()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        left = app.dashboard.query_one("#left")
        assert left.size.width < 140  # the map column takes its share
        await pilot.press("m")
        await pilot.pause()
        assert app.dashboard.has_class("no-map") and left.size.width == 140
        assert app.dashboard.query_one("#tsunami").region.y >= left.region.bottom  # the panel is a strip below


async def test_a_run_started_without_the_map_uses_the_whole_width():
    app = week_app()
    app.config = replace(app.config, view=replace(app.config.view, map=False))
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        assert app.dashboard.query_one("#left").size.width == 200


async def test_empty_list_gives_the_feed_reason_not_a_filter_hint():
    app = week_app()  # not warm: no steady batch has landed
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        empty = app.dashboard.query_one("#empty")
        assert empty.display and "No quakes match" in str(empty.render())  # quakes are held: the search emptied it
        app.scheduler.statuses["usgs"].health.failures = 3
        app.dashboard.refresh_view()
        text = str(empty.render())
        assert "USGS is not answering" in text and "No quakes match" not in text and "Press f" not in text
        app.store.merge(Batch("usgs", WEEK_NOW, quakes=()), app.clock)  # warm: the data was checked
        app.scheduler.statuses["usgs"].health.failures = 0  # and the feed answers again
        app.dashboard.refresh_view()
        assert "No quakes match" in str(empty.render())


async def test_offline_empty_list_says_nothing_is_fetched():
    app = week_app(offline=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        text = str(app.dashboard.query_one("#empty").render())
        assert "Quakes are not being fetched" in text and "Press f" not in text


def test_an_older_detail_product_yields_to_the_revised_quake():
    quake = replace(week_app().store.all()[0], pager_alert="orange")
    detail = QuakeDetail(id=quake.id, pager_alert="green")
    now = quake.time + timedelta(hours=1)
    stale = render_detail(quake, detail, "", None, "UTC", now, detail_current=False).plain
    assert "orange" in stale and "green" not in stale
    assert "green" in render_detail(quake, detail, "", None, "UTC", now).plain


async def test_detail_follows_a_preferred_id_change_and_reloads_the_product():
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        await pilot.press("enter")
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        await wait_for(lambda: screen.detail is not None)
        assert len(requests) == 1
        renamed = replace(
            top,
            id="us7000renamed",
            also_ids=(top.id,),
            mag=7.0,
            tsunami_flag=True,
            updated=top.updated + timedelta(minutes=1),
        )
        app.store.merge(Batch("usgs", WEEK_NOW, quakes=(renamed,)), app.clock)
        assert app.store.get(top.id) is None and app.store.resolve(top.id) == renamed
        screen._show()
        assert screen.quake.id == "us7000renamed" and not screen.gone
        rendered = str(screen.query_one("#detail").render())
        assert "M7.0" in rendered and "USGS links a tsunami product" in rendered
        await wait_for(lambda: len(requests) == 2)  # the product for the new revision is fetched
        screen._show()
        await pilot.pause()
        assert len(requests) == 2  # and only once


async def test_detail_says_so_when_the_store_no_longer_holds_the_quake():
    app = week_app(offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        await pilot.press("enter")
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        app.store._drop(top.id)
        screen._show()
        assert screen.gone and GONE_NOTE in str(screen.query_one("#detail").render())


async def test_observations_rereads_the_store():
    app = week_app(offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        app.open_observations(top)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ObservationsScreen)
        header = screen.query_one("#observations_header")
        assert not top.tsunami_flag and "has not flagged" in str(header.render())
        revised = replace(top, tsunami_flag=True, updated=top.updated + timedelta(minutes=2))
        app.store.merge(Batch("usgs", WEEK_NOW, quakes=(revised,)), app.clock)
        screen.refresh_gauges()
        assert "has not flagged" not in str(header.render())
        app.store._drop(top.id)
        screen.refresh_gauges()
        assert GONE_NOTE in str(header.render())


async def test_tsunami_bulletin_returns_to_the_top_for_another_alert_but_keeps_its_place_on_a_poll():
    app = kam_alert_app(0, 1)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TsunamiScreen)
        pane = screen.query_one("#alert_text_scroll")
        assert pane.max_scroll_y > 2
        pane.scroll_to(y=2, animate=False)
        await pilot.pause()
        screen.refresh_alerts()  # a poll re-renders the same alert
        await pilot.pause()
        assert pane.scroll_y == 2
        await pilot.press("j")
        await pilot.pause()
        assert pane.scroll_y == 0
        assert f"{screen.selected().level} (" in screen_text(app)  # the new alert's level and status are in view


def test_stylesheet_takes_its_colours_from_theme_variables():
    tcss = (Path(__file__).parent.parent / "earthwave" / "ui" / "app.tcss").read_text(encoding="utf-8")
    assert re.search(r"#[0-9a-fA-F]{6}\b", tcss) is None


async def test_warning_and_watch_are_coloured_differently():
    colours = {}
    for name, indexes in (("watch", (0,)), ("warning", (0, 1))):
        app = kam_alert_app(*indexes)
        async with app.run_test(size=(120, 30)) as pilot:
            observe(app, indexes[-1])
            await pilot.pause()
            banner, status, panel = (app.dashboard.query_one(i) for i in ("#banner", "#status", "#tsunami"))
            assert all(w.has_class(f"level-{name}") for w in (banner, status, panel))
            colours[name] = (banner.styles.background, status.styles.background)
    assert colours["watch"][0] != colours["warning"][0] and colours["watch"][1] != colours["warning"][1]
