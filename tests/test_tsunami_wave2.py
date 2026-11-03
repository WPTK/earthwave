"""Tsunami and data reliability, wave 2: products that stall or leave a bulletin unread are given up on (#158, #161),
a null magnitude is no removal (#159), partial gauge polls say so (#181), the bulletin and USGS magnitudes are
labelled (#269) and the last run's bulletins are shown offline, as history and never in force (#248)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from conftest import FIXTURES, make_quake
from test_gauge_sources import STATIONS_XML, coops_server
from test_gauge_sources import Server as GaugeServer
from test_history import _app, _poll, warn_alert
from test_round3_alerts import NOW, Server, client_for
from test_round3_alerts import now_clock as now_clock
from test_tsunami import JmaFeed
from uat.harness import screen_text

from earthwave.clock import ReplayClock
from earthwave.models import Batch, TsunamiAlert
from earthwave.sources import coops, jma, levels, ndbc, tsunamigov
from earthwave.sources.usgs import listing
from earthwave.state.events import QuakeRemoved
from earthwave.state.history import History
from earthwave.state.store import Store
from earthwave.ui.screens.tsunami import render_alert

# --- #161 / #158: tsunami.gov ---------------------------------------------------------------------------------------


async def test_a_product_that_always_stalls_is_given_up_on_and_the_source_completes(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_LIMIT", 0.05)
    server = Server("stuck1")
    server.hang.add("stuck1")
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        polls = [await source.fetch(client, None) for _ in range(tsunamigov.MAX_ENTRY_FAILURES)]
    assert all(p.meta["incomplete"] and not p.alerts for p in polls[:-1])
    last = polls[-1]
    assert not last.meta["incomplete"] and len(last.alerts) == 1  # a headline from the Atom entry, not a silent loss
    assert last.alerts[0].text.startswith("Details unavailable")


async def test_a_stalled_product_does_not_starve_older_ones_for_ever(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.35)
    monkeypatch.setattr(tsunamigov, "ENTRY_LIMIT", 0.1)
    server = Server("s1", "s2", "s3", "s4", "good")  # newest first: the four stalled ones are read first
    server.hang.update({"s1", "s2", "s3", "s4"})
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        first = await source.fetch(client, None)
        second = await source.fetch(client, None)
    assert [a.id for a in first.alerts] == []  # the stalled ones used the budget
    assert [a.id for a in second.alerts] == ["PAAQ-1-good"]  # after failing, they go last


async def test_a_bulletin_outage_that_never_ends_is_given_up_on_with_the_cap_alert_standing(now_clock):
    server = Server("tnfxzn")
    server.bulletin["tnfxzn"] = 503
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        polls = [await source.fetch(client, None) for _ in range(tsunamigov.MAX_ENTRY_FAILURES)]
    assert all(p.meta["incomplete"] for p in polls[:-1])
    assert not polls[-1].meta["incomplete"] and len(polls[-1].alerts) == 1


async def test_failure_counts_for_products_that_left_the_feed_are_dropped(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_LIMIT", 0.05)
    server = Server("gone01")
    server.hang.add("gone01")
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        await source.fetch(client, None)
        assert source._failures
        server.tags = ("other1",)  # the product has left the feed
        await source.fetch(client, None)
    assert source._failures == {}


# --- #161 / #158: JMA -----------------------------------------------------------------------------------------------


def _jma_client(feed, stuck: set[str]) -> httpx.AsyncClient:
    async def handler(request):
        if request.url.path.endswith("list.json"):
            return httpx.Response(200, json=feed.entries)
        name = request.url.path.rsplit("/", 1)[1]
        if name in stuck:
            await asyncio.sleep(5)
        return httpx.Response(200, json=feed.details[name])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_jma_report_that_always_stalls_is_given_up_on(monkeypatch):
    monkeypatch.setattr(jma, "ENTRY_LIMIT", 0.05)
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00")})
    async with _jma_client(feed, {"a.json"}) as client:
        polls = [await feed.source.fetch(client, None) for _ in range(jma.MAX_ENTRY_FAILURES)]
    assert all(p.meta["incomplete"] for p in polls[:-1])
    assert not polls[-1].meta["incomplete"] and len(polls[-1].alerts) == 1


async def test_jma_failure_counts_for_reports_that_left_the_list_are_dropped(monkeypatch):
    monkeypatch.setattr(jma, "ENTRY_LIMIT", 0.05)
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00")})
    async with _jma_client(feed, {"a.json"}) as client:
        await feed.source.fetch(client, None)
        assert feed.source._failures
        feed.entries = []
        await feed.source.fetch(client, None)
    assert feed.source._failures == {}


# --- #159 -----------------------------------------------------------------------------------------------------------


def test_a_quake_whose_magnitude_is_momentarily_null_is_not_removed(clock):
    store, quake = Store(), make_quake("ak1", 3.4, minutes_ago=300)
    window = (24.0, 2.5)
    first = Batch(
        "usgs", clock.now(), quakes=(quake,), coverage=window, generated=clock.now(), listed=frozenset({"ak1"})
    )
    store.merge(first, clock)
    feed = {
        "features": [
            {"id": "ak1", "properties": {"type": "earthquake", "mag": None}},
            {"id": "other", "properties": {"type": "earthquake", "mag": 3.1}},
        ]
    }
    later = Batch("usgs", clock.now(), coverage=window, generated=clock.now(), listed=listing(feed))
    assert not any(isinstance(e, QuakeRemoved) for e in store.merge(later, clock))
    assert store.get("ak1") is not None


# --- #181 -----------------------------------------------------------------------------------------------------------


async def test_a_dart_poll_where_some_stations_failed_is_incomplete(clock):
    good = (FIXTURES / "ndbc" / "46402.dart").read_text(encoding="utf-8")
    server = GaugeServer({"activestations.xml": STATIONS_XML, "46402.dart": good, "32401.dart": (503, "x")})
    source = ndbc.DartSource(clock)
    source.set_selection(["46402", "32401"])
    async with server.client() as client:
        batch = await source.fetch(client, None)
        assert batch.readings and batch.meta["incomplete"]
        source.set_selection(["46402"])
        assert not (await source.fetch(client, None)).meta["incomplete"]


async def test_a_coops_poll_where_some_stations_failed_is_incomplete(clock):
    healthy = coops_server()
    routes = dict(healthy.routes)
    data = routes["datagetter"]
    routes["datagetter"] = lambda request: (503, "x") if request.url.params["station"] == "0000000" else data(request)
    server = GaugeServer(routes)
    source = coops.CoopsSource(clock)
    source.set_selection(["9414290", "0000000"])  # the second answers 503
    async with server.client() as client:
        batch = await source.fetch(client, None)
        assert batch.readings and batch.meta["incomplete"]
        source.set_selection(["9414290"])
        assert not (await source.fetch(client, None)).meta["incomplete"]


# --- #269 -----------------------------------------------------------------------------------------------------------


def _bulletin(headline: str) -> TsunamiAlert:
    return TsunamiAlert("b", "b", "tsunamigov", levels.INFORMATION, headline, issued=NOW)


def test_differing_bulletin_and_usgs_magnitudes_are_labelled_by_source():
    alert = _bulletin("Tsunami Information Statement: Alaska M4.2")
    plain = render_alert(alert, "active", "M4.3 X (us1)", "UTC", quake_mag=4.3).plain
    assert "tsunami.gov M4.2" in plain and "USGS M4.3" in plain and "own estimate" in plain


def test_equal_or_absent_magnitudes_add_no_note():
    same = render_alert(_bulletin("Statement: Alaska M4.3"), "active", "M4.3 X (us1)", "UTC", quake_mag=4.3)
    none = render_alert(_bulletin("Tsunami Information Statement"), "active", "M4.3 X (us1)", "UTC", quake_mag=4.3)
    unlinked = render_alert(_bulletin("Statement: Alaska M4.2"), "active", "", "UTC")
    assert all("own estimate" not in t.plain for t in (same, none, unlinked))


# --- #248 -----------------------------------------------------------------------------------------------------------


async def test_history_keeps_the_last_bulletins_with_their_text(tmp_path, clock):
    history = History(tmp_path / "h.sqlite", 90, clock)
    history.open()
    history.start()
    alert = replace(
        warn_alert("w1", clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1)),
        text="THE BULLETIN",
        areas=("Alaska",),
    )
    history.record((), set(), (alert,), clock.now())
    await history.close()
    reopened = History(tmp_path / "h.sqlite", 90, clock)
    [back] = reopened.bulletins_since(clock.now() - timedelta(days=3))
    assert (back.id, back.level, back.text, back.areas, back.expires) == (
        "w1",
        "WARNING",
        "THE BULLETIN",
        ("Alaska",),
        alert.expires,
    )
    assert reopened.bulletins_since(clock.now() + timedelta(days=1)) == []


async def test_offline_the_tsunami_screen_shows_the_last_runs_bulletin_as_history_not_in_force(tmp_path):
    path = tmp_path / "h.sqlite"
    clock = ReplayClock(datetime(2026, 11, 1, 12, 0, tzinfo=UTC), real=lambda: 0.0)
    warning = replace(
        warn_alert("w1", clock.now() - timedelta(minutes=30), clock.now() + timedelta(hours=1)),
        headline="Tsunami Warning: Alaska",
        text="THE FULL BULLETIN TEXT",
    )
    app = _app(path, clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.store.warm = True
        _poll(app, [], [warning])
        await pilot.pause()
    later = ReplayClock(clock.now() + timedelta(hours=3), real=lambda: 0.0)
    app2 = _app(path, later, offline=True)
    async with app2.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app2.store.alerts() == ()  # never replayed into the store
        await pilot.press("t")
        await pilot.pause()
        text = screen_text(app2)
        assert "No tsunami products received" not in text
        assert "Tsunami Warning: Alaska" in text and "THE FULL BULLETIN TEXT" in text
        assert "last run" in text and "not current" in text
        assert app2.tracker.tsunami_unacked() == 0
