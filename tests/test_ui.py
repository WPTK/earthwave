"""Dashboard, detail and filter flows with Pilot, QuakeList flashing via render_line, labels and the snapshot."""

import asyncio
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from conftest import FIXTURES, T0, load_json, make_quake
from rich.text import Text

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, with_overrides
from earthwave.models import Batch
from earthwave.net import ReplayTransport, recording_start
from earthwave.notify.inapp import DIM_NEW, FLASH_NEW
from earthwave.poll import Freshness
from earthwave.sources.usgs import parse_collection
from earthwave.state.events import QuakeNew
from earthwave.state.store import SortKey, Store
from earthwave.ui.glyphs import ASCII, UNICODE, resolve_glyphs
from earthwave.ui.labels import filter_summary, fmt_age, fmt_countdown, fmt_distance, fmt_hours, fmt_time, zone_label
from earthwave.ui.screens.dashboard import DashboardScreen
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen, parse_filters
from earthwave.ui.screens.help import HELP_TEXT, HelpScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.theme import mag_color
from earthwave.ui.widgets.quake_list import QuakeList
from earthwave.ui.widgets.status_bar import FRESHNESS_WORDS, SourceLine, StatusInfo, build_status
from earthwave.ui.widgets.too_small import is_too_small

WEEK = load_json("usgs/4.5_week.geojson")
WEEK_NOW = datetime.fromtimestamp(WEEK["metadata"]["generated"] / 1000, tz=UTC)
DETAIL_BODY = (FIXTURES / "usgs" / "detail.geojson").read_bytes()
HOUR_BODY = (FIXTURES / "usgs" / "all_hour.geojson").read_bytes()


def week_app(transport=None, run_scheduler=False, home=True, ticks=False, **kwargs) -> EarthwaveApp:
    """Dashboard over the captured 4.5_week quakes at the instant they were captured (fully deterministic)."""
    clock = ReplayClock(WEEK_NOW, real=lambda: 0.0)
    config = with_overrides(Config(), 4.5, 168, (40.7, -74.0) if home else None, "unicode")
    store = Store(config.alerts, config.home)
    store.merge(Batch("usgs", WEEK_NOW, quakes=parse_collection(WEEK), backfill=True, coverage=(168.0, 4.5)), clock)
    return EarthwaveApp(
        config, store=store, clock=clock, run_scheduler=run_scheduler, ticks=ticks, transport=transport, **kwargs
    )


def mock_transport(requests: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/detail/" in request.url.path:
            return httpx.Response(200, content=DETAIL_BODY)
        if "fdsnws" in request.url.path:
            return httpx.Response(200, content=HOUR_BODY)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


def bgs(strip) -> set[str]:
    return {seg.style.bgcolor.name for seg in strip if seg.style and seg.style.bgcolor}


# --- flashing through render_line -------------------------------------------------------------------------------


async def test_new_row_flash_phases_via_render_line():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        top = quake_list.rows[0]
        assert FLASH_NEW.bg not in bgs(quake_list.render_line(0))
        app.tracker.observe([QuakeNew(top)])
        assert FLASH_NEW.bg in bgs(quake_list.render_line(0))  # t = 0.0 s: flash on
        app.clock.advance(0.5)
        assert FLASH_NEW.bg not in bgs(quake_list.render_line(0))  # t = 0.5 s: between flashes
        app.clock.advance(0.5)
        assert FLASH_NEW.bg in bgs(quake_list.render_line(0))  # t = 1.0 s: second flash
        app.clock.advance(2.5)
        assert DIM_NEW.bg in bgs(quake_list.render_line(0))  # t = 3.5 s: dim highlight until seen
        assert FLASH_NEW.bg not in bgs(quake_list.render_line(1))  # other rows untouched
        assert app.tracker.unseen() == 1
        quake_list.action_cursor_first()  # cursor landing acks
        quake_list.action_cursor_down()
        quake_list.action_cursor_up()
        assert DIM_NEW.bg not in bgs(quake_list.render_line(0)) and app.tracker.unseen() == 0


async def test_rows_show_age_mag_depth_distance_and_flags():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        top = quake_list.rows[0]
        line = quake_list.render_line(0).text
        assert f"{top.mag:4.1f}" in line and f"{top.depth_km:.0f}" in line and "km" in line
        assert top.place[:15] in line
        assert quake_list.render_line(len(quake_list.rows)).text.strip() == ""


# --- Pilot flows ------------------------------------------------------------------------------------------------


async def test_open_detail_loads_pager_and_copies_and_opens_url(monkeypatch):
    opened = []
    monkeypatch.setattr("earthwave.ui.screens.detail.webbrowser.open", opened.append)
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen) and screen.quake.id == top.id
        await wait_for(lambda: screen.detail is not None)
        await pilot.pause()
        rendered = str(screen.query_one("#detail").render())
        assert "PAGER alert" in rendered and "green" in rendered and "ShakeMap MMI" in rendered
        assert "Loading" not in rendered
        await pilot.press("y")
        assert app.clipboard == top.url
        await pilot.press("b")
        assert opened == [top.url]
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard
    assert len(requests) == 1 and "/detail/" in requests[0].url.path


async def test_detail_failure_is_shown_not_raised():
    app = week_app(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        screen = app.screen
        await wait_for(lambda: "unavailable" in screen.note)
        await pilot.pause()
        assert "Detail unavailable" in str(screen.query_one("#detail").render())


async def test_filters_modal_widens_window_and_backfills_from_fdsnws():
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = len(app.store)
        generation = app.scheduler.generation
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, FiltersScreen)
        app.screen.query_one("#min_mag").value = "1.0"
        app.screen.query_one("#hours").value = "168"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is app.dashboard
        assert (app.view.min_magnitude, app.view.hours) == (1.0, 168)
        assert app.scheduler.generation == generation + 1
        assert app.usgs.steady == "1.0_day"
        await wait_for(lambda: len(app.store) > before)
        await pilot.pause()
        params = dict(requests[-1].url.params)
        assert params["minmagnitude"] == "1" and params["format"] == "geojson"
        assert app.store.is_covered(168, 1.0)
        assert "M1+ 7d" in str(app.dashboard.query_one("#status").render())
        assert len(app.dashboard.query_one(QuakeList).rows) > 0


async def test_narrowing_filters_needs_no_request():
    requests: list = []
    app = week_app(transport=mock_transport(requests))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.apply_filters(6.0, 48)
        await pilot.pause()
        assert requests == []
        assert all(q.mag >= 6.0 for q in app.dashboard.query_one(QuakeList).rows)


async def test_filters_modal_validates_and_cancels():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        app.screen.query_one("#min_mag").value = "abc"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, FiltersScreen)
        assert "number" in str(app.screen.query_one("#error").render())
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard and app.view.min_magnitude == 4.5


def test_parse_filters():
    assert parse_filters("2.5", "24") == (2.5, 24.0)
    for bad in (("x", "24"), ("2", "y"), ("11", "24"), ("2", "0"), ("2", "9000")):
        with pytest.raises(ValueError):
            parse_filters(*bad)


async def test_sort_search_map_and_help_keys():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        by_time = [q.id for q in quake_list.rows]
        await pilot.press("s")
        assert app.sort is SortKey.MAG
        assert quake_list.rows[0].mag == max(q.mag for q in quake_list.rows)
        await pilot.press("s", "s")
        assert app.sort is SortKey.TIME and [q.id for q in quake_list.rows] == by_time
        place = quake_list.rows[0].place.split()[-1]
        await pilot.press("slash")
        await pilot.press(*place)
        await pilot.press("enter")
        await pilot.pause()
        assert quake_list.rows and all(place.lower() in q.place.lower() for q in quake_list.rows)
        await pilot.press("slash", "escape")
        await pilot.pause()
        assert app.view.query == "" and len(quake_list.rows) == len(by_time)
        assert not app.dashboard.has_class("no-map")
        await pilot.press("m")
        assert app.dashboard.has_class("no-map")
        await pilot.press("m", "question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_too_small_overlay_and_map_autohide():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not app.dashboard.has_class("too-small") and not app.dashboard.has_class("no-map")
        await pilot.resize_terminal(110, 40)
        await pilot.pause()
        assert app.dashboard.has_class("no-map") and not app.dashboard.has_class("too-small")
        await pilot.resize_terminal(90, 40)
        await pilot.pause()
        assert app.dashboard.has_class("too-small")
        assert "too small" in str(app.dashboard.query_one("#too_small").render())
        await pilot.resize_terminal(120, 25)
        await pilot.pause()
        assert app.dashboard.has_class("too-small")
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert not app.dashboard.has_class("too-small")


def test_is_too_small_boundaries():
    assert is_too_small(99, 30) and is_too_small(100, 29) and not is_too_small(100, 30)


async def test_ack_all_and_unseen_badge():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quakes = app.dashboard.query_one(QuakeList).rows
        app.tracker.observe([QuakeNew(q) for q in quakes[:3]])
        app.dashboard.refresh_status()
        assert "3 unseen" in str(app.dashboard.query_one("#status").render())
        await pilot.press("a")
        assert app.tracker.unseen() == 0


async def test_attention_for_hidden_quakes_never_counts_as_unseen():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        hidden = make_quake("hidden", 2.7, time=WEEK_NOW)
        app.tracker.observe([QuakeNew(hidden)])
        app.dashboard.refresh_view()
        assert app.tracker.unseen() == 0 and app.tracker.kind_of("hidden") is None


async def test_scheduler_exception_is_logged_not_swallowed(caplog):
    app = week_app()

    async def boom(client):
        raise RuntimeError("scheduler blew up")

    app.scheduler.run = boom
    async with app.run_test(size=(120, 40)) as pilot:
        app._run_scheduler()
        await pilot.pause()
        await pilot.pause()
    assert "scheduler stopped unexpectedly" in caplog.text


async def test_live_replay_session_runs_through_the_app():
    path = FIXTURES / "replay" / "usgs_short.jsonl"
    clock = ReplayClock(recording_start(path), speed=600)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    app = EarthwaveApp(config, clock=clock, transport=ReplayTransport(path, clock))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.store.get("replay_new1") is not None)
        await pilot.pause()
        assert app.tracker.kind_of("replay_new1") == "major"
        assert app.scheduler.freshness("usgs") is Freshness.FRESH
        status = str(app.dashboard.query_one("#status").render())
        unhealthy = [n for n in app.scheduler.statuses if app.scheduler.freshness(n) is not Freshness.FRESH]
        assert unhealthy  # the recording has no tsunami traffic, so those feeds fail
        for name in unhealthy:
            assert f"{name} {FRESHNESS_WORDS[app.scheduler.freshness(name)]}" in status
        assert "Replay Test Region" in str(app.dashboard.query_one("Ticker").render())


async def test_source_crash_leaves_app_running():
    def handler(request):
        raise httpx.ConnectError("network down")

    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    app = EarthwaveApp(config, clock=ReplayClock(T0, speed=500), transport=httpx.MockTransport(handler))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.scheduler.statuses["usgs"].health.failures > 0)
        await pilot.pause()
        assert app.scheduler.freshness("usgs") is Freshness.ERROR
        assert app.is_running


# --- labels and bars --------------------------------------------------------------------------------------------


def test_label_helpers():
    assert [fmt_age(s) for s in (5, 59, 60, 3599, 3600, 86400, 200000)] == ["5s", "59s", "1m", "59m", "1h", "1d", "2d"]
    assert fmt_countdown(75) == "1:15" and fmt_countdown(-3) == "0:00"
    assert fmt_hours(24) == "24h" and fmt_hours(168) == "7d" and fmt_hours(6.5) == "6.5h"
    assert (
        filter_summary(__import__("earthwave.state.store", fromlist=["ViewFilter"]).ViewFilter(2.5, 24, "chile"))
        == 'M2.5+ 24h "chile"'
    )
    assert fmt_time(T0, "UTC") == "2026-10-31 12:00:00 UTC"
    assert fmt_distance(make_quake(lat=1.0, lon=0.0), 0.0, 0.0) == "111km N"
    assert mag_color(2.9) == "grey62" and mag_color(4.5) == "green" and mag_color(7.0) == "bold red"


def test_glyph_auto_selection():
    assert resolve_glyphs("auto", "win32", {}) is ASCII
    assert resolve_glyphs("auto", "win32", {"WT_SESSION": "x"}) is UNICODE
    assert resolve_glyphs("auto", "linux", {}) is UNICODE
    assert resolve_glyphs("ascii", "linux", {}) is ASCII and resolve_glyphs("unicode", "win32", {}) is UNICODE


def test_status_bar_text():
    info = StatusInfo(
        sources=(SourceLine("usgs", Freshness.FRESH, 12), SourceLine("nws", Freshness.ERROR, None)),
        shown=10,
        total=40,
        next_poll=47,
        filter_text="M2.5+ 24h",
        unseen=3,
    )
    plain = build_status(info, UNICODE).plain
    assert "usgs" not in plain and "nws failed" in plain and "10 shown of 40" in plain
    aged = StatusInfo((SourceLine("nws", Freshness.ERROR, 180),), 1, 1, None, "x", 0)
    assert "nws failed, last ok 3m" in build_status(aged, UNICODE).plain  # not 'failed 3m'
    assert "next 0:47" in plain and "3 unseen" in plain and "M2.5+ 24h" in plain
    assert "unseen" not in build_status(StatusInfo((), 0, 0, None, "x", 0), ASCII).plain


def test_status_bar_names_a_feed_that_answers_but_skips_things():
    lines = (SourceLine("usgs", Freshness.FRESH, 5), SourceLine("tsunamigov", Freshness.FRESH, 5, incomplete=True))
    plain = build_status(StatusInfo(lines, 1, 1, None, "x", 0), UNICODE).plain
    assert "tsunamigov incomplete, last ok 5s" in plain and "feeds ok" not in plain


def test_status_bar_healthy_feeds_are_one_indicator():
    fresh = tuple(SourceLine(n, Freshness.FRESH, 5) for n in ("usgs", "nws", "jma"))
    plain = build_status(StatusInfo(fresh, 1, 1, None, "x", 0), UNICODE).plain
    assert "feeds ok" in plain and "usgs" not in plain
    stale = (fresh[0], SourceLine("nws", Freshness.STALE, 900))
    plain = build_status(StatusInfo(stale, 1, 1, None, "x", 0), ASCII).plain
    assert "nws stale, last ok 15m" in plain and "feeds ok" not in plain and "usgs" not in plain


async def test_empty_list_says_what_to_do():
    app = week_app()
    app.store.merge(Batch("usgs", WEEK_NOW, quakes=()), app.clock)  # the first steady poll has landed
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        empty = app.dashboard.query_one("#empty")
        assert not empty.display
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        assert empty.display and "No quakes match" in str(empty.render()) and "Press f" in str(empty.render())


async def test_empty_hint_waits_for_the_first_poll():
    app = week_app()  # backfilled only: no steady poll has answered, so an empty list proves nothing
    assert not app.store.warm
    async with app.run_test(size=(160, 40)) as pilot:
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        assert not app.dashboard.query_one("#empty").display


def test_zone_label_spells_long_windows_names_as_an_offset():
    def at(name: str, hours: float) -> datetime:
        return T0.astimezone(timezone(timedelta(hours=hours), name))

    assert zone_label(at("Central Europe Standard Time", 1)) == "UTC+01:00"
    assert zone_label(at("GMT Standard Time", 0)) == "UTC+00:00"
    assert zone_label(at("China Standard Time", 8)) == "UTC+08:00"
    assert zone_label(at("India Standard Time", 5.5)) == "UTC+05:30"
    assert zone_label(at("Pacific Standard Time", -8)) == "UTC-08:00"
    assert zone_label(at("EDT", -4)) == "EDT" and zone_label(at("UTC", 0)) == "UTC"


async def test_help_lists_every_visible_binding_exactly_once():
    names = {"slash": "/", "question_mark": "?", "escape": "Esc"}
    keys = {
        names.get(key, key)
        for screen in (DashboardScreen, TsunamiScreen, DetailScreen, EarthwaveApp)
        for binding in screen.BINDINGS
        if binding.show
        for key in binding.key.split(",")
    }
    assert keys
    # Each row is "key  what it does" pairs; a key is what stands where a key should, never a word of a description.
    listed: Counter[str] = Counter()
    for row in HELP_TEXT.split("[b]Keys[/b]")[1].split("[b]Colors[/b]")[0].splitlines():
        if row.startswith("  Detail:"):
            listed.update(re.findall(r"(\S+) (?:open page|copy URL|back)", row))
            continue
        cells = re.split(r"\s{2,}", row.strip())
        for cell in cells[::2]:
            listed.update([cell] if cell == "/" else (part.strip() for part in cell.split("/") if part.strip()))
    for key in keys:
        assert listed[key] == 1, (key, listed[key])
    assert all(len(Text.from_markup(line).plain) <= 74 for line in HELP_TEXT.splitlines())


async def test_footer_is_built_from_bindings_and_leaves_the_status_bar_visible():
    from textual.widgets import Footer
    from textual.widgets._footer import FooterKey

    app = week_app()
    async with app.run_test(size=(100, 30)) as pilot:  # the smallest supported terminal
        await pilot.pause()
        footer = app.screen.query_one(Footer)
        status = app.screen.query_one("#status")
        assert footer.display and status.display
        assert footer.region.y == 29 and status.region.y == 28  # one row each, the footer last
        await pilot.pause()  # the footer composes its keys after the screen is up
        await pilot.pause()
        keys = list(app.screen.query(FooterKey))
        # Every shown dashboard binding is in the footer, in BINDINGS order, and none is cut off by the width.
        assert [k.description for k in keys[:-1]] == [b.description for b in DashboardScreen.BINDINGS if b.show]
        assert keys[-1].description == "Quit"
        assert all(k.region.right <= 100 and k.region.width > 0 for k in keys)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen)
        assert [k.description for k in app.screen.query(FooterKey)] == ["Back", "Browser", "Copy URL", "Quit"]


# --- snapshot ---------------------------------------------------------------------------------------------------


def test_dashboard_snapshot_120x40(snap_compare):
    assert snap_compare(week_app(), terminal_size=(120, 40))


def test_replay_fixture_is_valid_jsonl():
    rows = [
        json.loads(line) for line in (FIXTURES / "replay" / "usgs_short.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["status"] for r in rows] == [200, 200, 200, 200, 304, 200]


@pytest.mark.parametrize(
    ("url", "opens"),
    [
        ("https://earthquake.usgs.gov/earthquakes/eventpage/x", True),
        ("http://example.org/x", True),
        (r"C:\Windows\System32\calc.exe", False),
        ("file:///C:/Windows/System32/calc.exe", False),
        ("-flag", False),
        ("calc.exe", False),
        ("", False),
    ],
)
def test_detail_opens_only_web_urls(monkeypatch, url, opens):
    from types import SimpleNamespace

    opened = []
    monkeypatch.setattr("earthwave.ui.screens.detail.webbrowser.open", opened.append)
    DetailScreen.action_open_browser(SimpleNamespace(quake=SimpleNamespace(url=url)))
    assert opened == ([url] if opens else [])
