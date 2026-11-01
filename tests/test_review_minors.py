"""Minor findings of the deep review: each test fails on the code it replaced."""

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from conftest import make_quake, wait_until
from test_sources_state import SlowSource
from test_tsunami import NOW, TG, JmaFeed, kam, kam_app, read
from test_ui import week_app
from uat.harness import screen_text

from earthwave import poll
from earthwave.config import ConfigError, parse_config
from earthwave.models import Batch
from earthwave.net import RecordingTransport, load_recording
from earthwave.notify.inapp import AttentionTracker
from earthwave.poll import Scheduler
from earthwave.sources import jma, tsunamigov
from earthwave.state.events import QuakeNew, TsunamiIssued
from earthwave.state.store import Store
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.banner import build_banner

EMPTY_ATOM = "<feed xmlns='http://www.w3.org/2005/Atom'/>"


def toasts(app) -> str:
    return " | ".join(n.message for n in app._notifications)


LONG = "Tsunami warning " + "Aleutian Islands and the coast of Alaska " * 5

# --- sources and network ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://www.tsunami.gov/events/PAAQ/x/PAAQCAP.xml", True),
        ("https://tsunami.gov/x", True),
        ("http://www.tsunami.gov/x", False),
        ("https://evil.example/x", False),
        ("https://www.tsunami.gov.evil.example/x", False),
        ("https://eviltsunami.gov/x", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_only_https_on_tsunami_gov_is_followed(url, ok):
    assert tsunamigov.trusted_url(url) is ok


async def test_feed_supplied_links_off_tsunami_gov_are_never_fetched(clock):
    atom = read(TG / "PAAQAtom.xml").replace(
        "https://www.tsunami.gov/events/PAAQ/", "https://evil.example/events/PAAQ/"
    )
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text=atom if str(request.url) == tsunamigov.FEEDS[0] else EMPTY_ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await tsunamigov.TsunamiGovSource(clock).fetch(client, None)
    assert set(hosts) == {"www.tsunami.gov"}  # the two Atom feeds only
    assert len(batch.alerts) == 1  # still shown, from the entry alone
    assert "could not be read" in batch.alerts[0].text


async def test_a_bulletin_link_off_tsunami_gov_is_skipped_but_the_cap_is_used(clock):
    atom = read(TG / "PAAQAtom.xml").replace(
        "https://www.tsunami.gov/events/PAAQ/2026/10/25/tnfxzn/1/WEAK53/WEAK53.txt", "http://evil.example/WEAK53.txt"
    )
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=atom)
        if str(request.url) == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text=EMPTY_ATOM)
        return httpx.Response(200, text=read(TG / "PAAQ_0.cap.xml"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await tsunamigov.TsunamiGovSource(clock).fetch(client, None)
    assert "http://evil.example/WEAK53.txt" in atom
    assert not any("evil.example" in u for u in requested) and len(batch.alerts) == 1


@pytest.mark.parametrize(
    ("header", "sent", "month"),
    [
        (
            "WEAK53 PAAQ 150000",
            datetime(2026, 10, 14, 23, 59, 58, tzinfo=UTC),
            "2026-10",
        ),  # stamped just before midnight
        ("WEAK53 PAAQ 142359", datetime(2026, 10, 15, 0, 0, 2, tzinfo=UTC), "2026-10"),  # stamped just after
        ("WEAK53 PAAQ 010002", datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC), "2026-11"),  # month end
        ("WEAK53 PAAQ 312359", datetime(2026, 11, 1, 0, 0, 3, tzinfo=UTC), "2026-10"),
        ("WEAK53 PAAQ 010001", datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), "2027-01"),  # year end
        ("WEAK53 PAAQ 312358", datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC), "2026-12"),
    ],
)
def test_dedupe_month_follows_the_header_across_any_midnight(header, sent, month):
    assert tsunamigov.dedupe_key(header, sent) == f"{header}|{month}"


def test_a_non_ascii_contact_is_a_config_error_not_a_startup_crash():
    with pytest.raises(ConfigError, match="ASCII"):
        parse_config({"notify": {"contact": "bill@exämple.com"}})
    assert parse_config({"notify": {"contact": "bill@example.com"}}).notify.contact == "bill@example.com"


async def test_a_fetch_that_never_finishes_is_given_up_on(clock, monkeypatch):
    monkeypatch.setattr(poll, "FETCH_DEADLINE", 0.05)
    source = SlowSource(asyncio.Event())  # never released: a server trickling bytes
    sched = Scheduler([source], Store(), clock, lambda r: None)
    result = await asyncio.wait_for(sched.poll_source("slow", None), 2)
    assert result.error and result.error.startswith("TimeoutError")
    assert sched.statuses["slow"].health.failures == 1 and not sched.statuses["slow"].in_flight


CUT = '{"ts": "2026-10-31T12:00:00+00:00", "url": "u", "status": 200}\n{"ts": "2026-10-3'


def test_load_recording_skips_a_line_cut_off_mid_write(tmp_path):
    path = tmp_path / "rec.jsonl"
    path.write_text(CUT, encoding="utf-8")
    assert [r["url"] for r in load_recording(path)] == ["u"]


async def test_recording_after_a_cut_off_line_starts_on_a_fresh_line(tmp_path, clock):
    path = tmp_path / "rec.jsonl"
    path.write_text(CUT, encoding="utf-8")
    inner = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=RecordingTransport(inner, path, clock)) as client:
        await client.get("https://example.com/new")
    assert [r["url"] for r in load_recording(path)] == ["u", "https://example.com/new"]


def test_jma_remembers_only_the_newest_events():
    source = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00")}).source
    for n in range(jma.SEEN_LIMIT + 50):
        source._link(f"event{n}", f"id{n}")
    assert len(source._ids_by_event) == jma.SEEN_LIMIT
    assert "event0" not in source._ids_by_event and f"event{jma.SEEN_LIMIT + 49}" in source._ids_by_event
    assert source._link("event249", "later") == ("id249",)  # an event still in the window keeps its history


def test_seen_quakes_do_not_pile_up_when_nobody_takes_them(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeNew(make_quake(f"q{n}", 6.0)) for n in range(3)])
    tracker.mark_seen({"q0", "q1", "q2"})
    tracker.retain({"q2"})  # q0 and q1 left the store
    assert tracker.take_seen() == {"q2"}


# --- screens ----------------------------------------------------------------------------------------------------


async def test_pressing_b_does_not_block_the_app_while_the_browser_runs(monkeypatch):
    release, started, returned = threading.Event(), threading.Event(), threading.Event()

    def slow_open(url):
        started.set()
        release.wait(5)  # a console browser: returns only when it exits
        returned.set()
        return True

    monkeypatch.setattr("earthwave.ui.screens.detail.webbrowser.open", slow_open)
    app = week_app()
    quake = replace(app.store.visible(app.view, app.clock)[0], url="https://earthquake.usgs.gov/x")
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            app.open_detail(quake)
            await wait_until(pilot, lambda: isinstance(app.screen, DetailScreen), what="the detail screen")
            await pilot.press("b")
            await wait_until(pilot, started.is_set, what="the browser call")
            await pilot.press("escape")  # the loop is free: the screen closes while the browser is still open
            await wait_until(pilot, lambda: app.screen is app.dashboard, what="going back")
            assert not returned.is_set()  # and it did so without waiting for the browser
    finally:
        release.set()


async def test_filter_error_is_shown_whole():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_filters()
        await wait_until(pilot, lambda: isinstance(app.screen, FiltersScreen), what="the dialog")
        await wait_until(pilot, lambda: app.screen.query("#min_mag"), what="the fields")
        app.screen.query_one("#min_mag").value = "99"
        await pilot.press("enter")
        await wait_until(pilot, lambda: "and 10" in screen_text(app), what="the end of the error")


def test_banner_keeps_the_count_and_key_hint_after_a_long_headline():
    alerts = [replace(kam(1), headline=LONG), kam(0)]
    plain = build_banner(alerts, 100).plain
    assert len(plain) <= 100 and "…" in plain
    assert plain.endswith("(+1 more)   t details, a acknowledge")
    assert "…" not in build_banner(alerts[1:], 100).plain  # a short headline is untouched


async def test_banner_on_screen_ends_with_the_key_hint():
    app = kam_app()
    app.store.merge(Batch("tsunamigov", NOW, alerts=(replace(kam(1), headline=LONG),)), app.clock)
    async with app.run_test(size=(100, 30)) as pilot:
        app.tracker.observe([TsunamiIssued(kam(1))])
        app.dashboard.refresh_view()
        await wait_until(pilot, lambda: "a acknowledge" in screen_text(app), what="the key hint")


async def test_tsunami_screen_shows_status_at_100_columns_and_keeps_scroll_across_a_poll():
    app = kam_app()
    wide = replace(kam(1), id="wide", dedupe_key="wide", areas=("Alaska Aleutian Islands " * 3,))
    app.store.merge(Batch("nws", NOW, alerts=(wide,)), app.clock)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("t")
        await wait_until(pilot, lambda: isinstance(app.screen, TsunamiScreen), what="the tsunami screen")
        await pilot.pause()
        text = screen_text(app)
        alert = app.screen.selected()
        status = app.store.alert_status(alert, app.clock)
        row = next(line for line in text.splitlines() if line.lstrip().startswith(alert.level))
        assert "STATUS" in text and status in row
        table = app.screen.query_one("DataTable")
        table.scroll_to(x=8, animate=False, immediate=True)
        await pilot.pause()
        before = table.scroll_x
        assert before > 0
        app.screen.refresh_alerts()
        await pilot.pause()
        assert table.scroll_x == before


async def test_a_on_the_tsunami_screen_says_what_it_did():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.tracker.observe([TsunamiIssued(kam(1))])
        await pilot.press("t")
        await wait_until(pilot, lambda: isinstance(app.screen, TsunamiScreen), what="the tsunami screen")
        await pilot.press("a")
        await wait_until(pilot, lambda: "Acknowledged 1 tsunami alert" in toasts(app), what="the toast")
        assert app.tracker.tsunami_unacked() == 0
        app.clear_notifications()
        await pilot.press("a")
        await wait_until(pilot, lambda: "No unacknowledged tsunami alerts" in toasts(app), what="the toast")


async def test_m_and_z_say_why_nothing_happens_below_120_columns():
    app = week_app()
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert app.dashboard.show_map
        await pilot.press("m")
        await wait_until(pilot, lambda: "needs a terminal 120 columns" in toasts(app), what="the m toast")
        assert app.dashboard.show_map  # the hidden preference did not flip behind the user's back
        app.clear_notifications()
        await pilot.press("z")
        await wait_until(pilot, lambda: "nothing to zoom" in toasts(app), what="the z toast")


async def test_r_does_not_claim_to_refresh_when_nothing_polls():
    app = week_app()  # no scheduler, as with --offline
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_until(pilot, lambda: "nothing to refresh" in toasts(app), what="the toast")
        assert "Refreshing" not in toasts(app)


async def test_empty_hint_names_whichever_of_search_and_filter_emptied_the_list():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        empty = app.dashboard.query_one("#empty")
        app.set_query("zzzz-no-such-place")
        await pilot.pause()
        assert "Press / to change the search" in str(empty.render())
        app.view = replace(app.view, query="", min_magnitude=9.9)
        app.dashboard.refresh_view()
        await pilot.pause()
        assert "Press f to change filters" in str(empty.render())
