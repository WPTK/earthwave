"""Round 3: an alert in hand is shown whatever else is failing. Each test fails on the code it replaced."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, make_quake
from test_tsunami import TG, JmaFeed, read

from earthwave import poll
from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig
from earthwave.models import Batch, TsunamiAlert
from earthwave.poll import Scheduler
from earthwave.sources import coops, jma, levels, tsunamigov
from earthwave.sources.base import ENTRY_BUDGET
from earthwave.sources.usgs import STARTUP_ATTEMPTS, UsgsSource, listing
from earthwave.state.events import QuakeMajor, QuakeRemoved, QuakeUpdated, TsunamiIssued
from earthwave.state.store import Store

NOW = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)
EMPTY_ATOM = "<feed xmlns='http://www.w3.org/2005/Atom'/>"
PAAQ = tsunamigov.FEEDS[0]


@pytest.fixture
def now_clock() -> ReplayClock:
    return ReplayClock(NOW, real=lambda: 0.0)


def atom_of(*tags: str) -> str:
    """The PAAQ Atom fixture with one entry per tag (each with its own CAP and bulletin links)."""
    atom = read(TG / "PAAQAtom.xml")
    start, end = atom.index("<entry>"), atom.index("</entry>") + len("</entry>")
    entries = [
        atom[start:end].replace("tnfxzn", tag).replace("890504a1", f"{n:08d}") for n, tag in enumerate(tags, start=1)
    ]
    return atom[:start] + "".join(entries) + atom[end:]


def cap_of(tag: str, minute: int = 26, event: str = "Tsunami Warning") -> str:
    cap = read(TG / "PAAQ_0.cap.xml").replace("PAAQ-1-tnfxzn", f"PAAQ-1-{tag}")
    cap = cap.replace("T02:26:53", f"T02:{minute}:53")  # each product has its own header, so its own dedupe key
    return cap.replace("<event>Tsunami Information</event>", f"<event>{event}</event>")


class Server:
    """tsunami.gov as a handler. `bulletin` is a status per tag (200 serves the text), `hang` tags never answer."""

    def __init__(self, *tags: str):
        self.tags = tags
        self.bulletin: dict[str, int] = {}
        self.hang: set[str] = set()
        self.cap_calls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == PAAQ:
            return httpx.Response(200, text=atom_of(*self.tags))
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text=EMPTY_ATOM)
        tag = next(t for t in self.tags if f"/{t}/" in url)
        if tag in self.hang and url.endswith("PAAQCAP.xml"):
            await asyncio.sleep(30)
        if url.endswith("PAAQCAP.xml"):
            self.cap_calls.append(tag)
            return httpx.Response(200, text=cap_of(tag, 26 + self.tags.index(tag)))
        status = self.bulletin.get(tag, 200)
        text = read(TG / "PAAQ_0.txt").replace("250226", f"2502{26 + self.tags.index(tag)}")
        return httpx.Response(status, text=text if status == 200 else "")


def client_for(server: Server) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server))


# --- the four-way design: a CAP in hand is shown whatever else fails ----------------------------------------------


async def test_a_warning_is_shown_from_its_cap_during_a_bulletin_outage_and_not_shown_twice_after(now_clock):
    server = Server("tnfxzn")
    server.bulletin["tnfxzn"] = 503
    source, store = tsunamigov.TsunamiGovSource(now_clock), Store()
    async with client_for(server) as client:
        first = await source.fetch(client, None)
        assert first.meta["incomplete"] and len(first.alerts) == 1  # the CAP is in hand: shown now
        events = store.merge(first, now_clock)
        assert [type(e) for e in events] == [TsunamiIssued]
        shown = store.active_alerts(now_clock)
        assert len(shown) == 1 and "TSUNAMI INFORMATION STATEMENT" not in shown[0].text
        again = await source.fetch(client, None)  # the outage goes on: still shown, still not final
        assert again.meta["incomplete"] and store.merge(again, now_clock) == []
        server.bulletin["tnfxzn"] = 200
        last = await source.fetch(client, None)
    assert not last.meta["incomplete"]
    assert store.merge(last, now_clock) == []  # no second Issued, so no second banner, toast or bell
    shown = store.active_alerts(now_clock)
    assert len(shown) == 1 and "TSUNAMI INFORMATION STATEMENT" in shown[0].text  # upgraded to the bulletin text


async def test_the_budget_leaves_the_fetch_deadline_room_to_merge():
    """poll_source puts the store merge inside FETCH_DEADLINE too, so the entry budget must end well before it."""
    assert ENTRY_BUDGET < poll.FETCH_DEADLINE


async def test_a_spent_entry_budget_returns_what_was_read_and_finishes_the_rest_next_poll(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.3)
    server = Server("aaaa01", "bbbb02")  # the feed lists newest first: aaaa01 is read first, bbbb02 last
    server.hang.add("bbbb02")
    source, store = tsunamigov.TsunamiGovSource(now_clock), Store()
    async with client_for(server) as client:
        first = await source.fetch(client, None)
        assert first.meta["incomplete"] and [a.id for a in first.alerts] == ["PAAQ-1-aaaa01"]
        assert len(store.merge(first, now_clock)) == 1
        server.hang.clear()
        second = await source.fetch(client, None)
    assert [a.id for a in second.alerts] == ["PAAQ-1-bbbb02"] and not second.meta["incomplete"]


async def test_deadline_bulletin_outage_and_alert_state_together(now_clock, monkeypatch):
    """A Warning shows with its CAP text while one bulletin is down and another entry stalls past the budget; when
    both recover there is one alert per product, the text upgraded, and nothing announced twice."""
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.3)
    server = Server("aaaa01", "bbbb02")
    server.bulletin["aaaa01"] = 503
    server.hang.add("bbbb02")
    sched = Scheduler([tsunamigov.TsunamiGovSource(now_clock)], store := Store(), now_clock, lambda r: None)
    async with client_for(server) as client:
        first = await sched.poll_source("tsunamigov", client)
        assert first.error is None and first.batch.meta["incomplete"]
        assert [type(e) for e in first.events].count(TsunamiIssued) == 1
        assert [a.id for a in store.active_alerts(now_clock)] == ["PAAQ-1-aaaa01"]
        assert "TSUNAMI INFORMATION STATEMENT" not in store.active_alerts(now_clock)[0].text
        server.hang.clear()
        server.bulletin.clear()
        second = await sched.poll_source("tsunamigov", client)
    assert second.error is None and not second.batch.meta["incomplete"]
    assert [type(e) for e in second.events].count(TsunamiIssued) == 1  # only the product that was never shown
    shown = store.active_alerts(now_clock)
    assert sorted(a.id for a in shown) == ["PAAQ-1-aaaa01", "PAAQ-1-bbbb02"]
    assert all("TSUNAMI INFORMATION STATEMENT" in a.text for a in shown)


async def test_a_feed_redirected_off_site_makes_the_poll_partial_not_failed(now_clock):
    server = Server("tnfxzn")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == tsunamigov.FEEDS[1]:
            return httpx.Response(302, headers={"Location": "https://maintenance.example/down.xml"})
        if request.url.host == "maintenance.example":
            return httpx.Response(200, text=EMPTY_ATOM)
        return await server(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        batch = await tsunamigov.TsunamiGovSource(now_clock).fetch(client, None)
    assert batch.meta["incomplete"] and [a.id for a in batch.alerts] == ["PAAQ-1-tnfxzn"]


async def test_a_bad_link_in_one_entry_does_not_stop_the_others_and_is_given_up_on(now_clock):
    server = Server("aaaa01", "bbbb02")

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await server(request)
        if str(request.url) == PAAQ:  # the newest entry (aaaa01, first in the feed) gets an unparseable link
            return httpx.Response(200, text=response.text.replace("/aaaa01/1/WEAK53/PAAQCAP.xml", "/[x/a.cap", 1))
        return response

    source = tsunamigov.TsunamiGovSource(now_clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await source.fetch(client, None)
        assert [a.id for a in first.alerts] == ["PAAQ-1-bbbb02"] and first.meta["incomplete"]
        for _ in range(tsunamigov.MAX_ENTRY_FAILURES - 1):
            last = await source.fetch(client, None)
    assert len(last.alerts) == 1 and last.alerts[0].text.startswith("Details unavailable")  # given up: headline only


async def test_jma_returns_the_reports_read_before_its_budget_ran_out(monkeypatch):
    monkeypatch.setattr(jma, "ENTRY_BUDGET", 0.3)
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00"), "b.json": ("62", "2026-10-10T03:40:00+09:00")})
    hang = True

    async def handler(request):
        if request.url.path.endswith("list.json"):
            return httpx.Response(200, json=feed.entries)
        name = request.url.path.rsplit("/", 1)[1]
        if hang and name == "b.json":
            await asyncio.sleep(30)
        return httpx.Response(200, json=feed.details[name])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await feed.source.fetch(client, None)
        hang = False
        second = await feed.source.fetch(client, None)
    assert len(first.alerts) == 1 and first.meta["incomplete"]
    assert len(second.alerts) == 1 and not second.meta["incomplete"]


async def test_one_malformed_jma_list_entry_does_not_stop_the_rest():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00")})
    feed.entries = [{**feed.entries[0], "rdt": 12345}, {**feed.entries[0], "json": ["x"]}, *feed.entries]
    batch = await feed.poll()
    assert len(batch.alerts) == 1


# --- usgs ---------------------------------------------------------------------------------------------------------


async def test_a_stalled_startup_feed_is_given_up_on_so_the_steady_feed_is_reached(clock, monkeypatch):
    monkeypatch.setattr(poll, "FETCH_DEADLINE", 0.05)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("2.5_day.geojson"):
            return httpx.Response(200, json={"features": []})
        await asyncio.sleep(30)  # every startup feed trickles past the deadline
        return httpx.Response(200, json={"features": []})

    source = UsgsSource(clock)
    sched = Scheduler([source], Store(), clock, lambda r: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(3 * STARTUP_ATTEMPTS):
            assert (await sched.poll_source("usgs", client)).error
        assert not source.starting
        result = await sched.poll_source("usgs", client)
    assert result.error is None and requested[-1] == "2.5_day.geojson"


def test_a_feature_usgs_retyped_or_left_without_a_magnitude_is_no_longer_listed():
    features = [
        {"id": "eq", "properties": {"type": "earthquake", "mag": 3.0}},
        {"id": "blast", "properties": {"type": "quarry blast", "mag": 2.8, "ids": ",blast,ak9,"}},
        {"id": "nomag", "properties": {"type": "earthquake", "mag": None}},
        {"id": "plain", "properties": {}},  # malformed: still vouched for
    ]
    assert listing({"features": features}) == {"eq", "plain"}


def test_a_stored_quake_usgs_retyped_as_a_blast_is_removed(clock):
    store, quake = Store(), make_quake("ak1", 2.8, minutes_ago=300)
    window = (24.0, 2.5)
    first = Batch("usgs", clock.now(), quakes=(quake,), coverage=window, generated=clock.now())
    first = replace(first, listed=frozenset({"ak1"}))
    store.merge(first, clock)
    feed = {
        "features": [
            {"id": "ak1", "properties": {"type": "quarry blast", "mag": 2.8}},
            {"id": "other", "properties": {"type": "earthquake", "mag": 3.1}},  # an empty listing removes nothing
        ]
    }
    later = Batch("usgs", clock.now(), coverage=window, generated=clock.now(), listed=listing(feed))
    assert any(isinstance(e, QuakeRemoved) for e in store.merge(later, clock))
    assert store.get("ak1") is None


# --- store --------------------------------------------------------------------------------------------------------


def test_segments_of_one_bulletin_at_different_levels_stay_separate_alerts(now_clock):
    from earthwave.models import TsunamiAlert

    def segment(ident, level, area):
        return TsunamiAlert(ident, "WEAK51 PAAQ 292351|2026-10", "nws", level, "h", (area,), issued=NOW, updated=NOW)

    warning, advisory = segment("w", levels.WARNING, "Aleutian Islands"), segment("a", levels.ADVISORY, "Oregon Coast")
    store = Store()
    store.merge(Batch("nws", NOW, alerts=(warning, advisory)), now_clock)
    levels_by_area = {a.areas: a.level for a in store.active_alerts(now_clock)}
    assert levels_by_area == {("Aleutian Islands",): levels.WARNING, ("Oregon Coast",): levels.ADVISORY}


def test_a_revision_in_a_silent_batch_still_alerts_when_it_postdates_the_steady_feed(clock):
    alerts = AlertsConfig()
    store = Store(alerts)
    t = clock.now()
    steady = Batch(
        "usgs",
        t,
        quakes=(make_quake("us1", 5.9, time=t - timedelta(minutes=30)),),
        generated=t,
        meta={"feed": "2.5_day"},
    )
    store.merge(steady, clock)
    assert store.warm
    revised = make_quake(
        "us1", 6.2, time=t - timedelta(minutes=30), updated=t + timedelta(seconds=10), tsunami_flag=True
    )
    switch = Batch("usgs", t + timedelta(seconds=30), quakes=(revised,), meta={"silent": True})
    events = store.merge(switch, clock)
    assert any(isinstance(e, QuakeUpdated) for e in events)
    assert any(isinstance(e, QuakeMajor) for e in events)


def test_a_revision_the_steady_feed_already_delivered_stays_quiet_in_a_silent_batch(clock):
    store, t = Store(AlertsConfig()), clock.now()
    quake = make_quake("us1", 4.0, time=t - timedelta(minutes=30), updated=t - timedelta(minutes=5))
    store.merge(Batch("usgs", t, quakes=(quake,), generated=t), clock)
    revised = replace(quake, mag=4.2, updated=t - timedelta(minutes=2))  # older than the steady feed's answer
    assert store.merge(Batch("usgs", t, quakes=(revised,), meta={"silent": True}), clock) == []


def test_coops_residuals_older_than_the_prediction_cache_are_kept():
    obs = coops.parse_water_level((FIXTURES / "coops" / "water_level_9414290.json").read_text(encoding="utf-8"))
    preds = coops.parse_predictions(
        (FIXTURES / "coops" / "predictions_9414290.json").read_text(encoding="utf-8"), "9414290"
    )
    clock = ReplayClock(obs[-1].ts, real=lambda: 0.0)
    store = Store(AlertsConfig())
    store.predictions_for = lambda station_id: preds
    rows = tuple(coops.to_reading(r) for r in obs)
    store.merge(Batch("coops", clock.now(), readings=rows[:-1]), clock)
    early = store.gauge_residuals("9414290")
    assert len(early) > 10
    cut = early[len(early) // 2].ts
    store.predictions_for = lambda station_id: [p for p in preds if p.ts >= cut]  # the cache has moved on
    store.merge(Batch("coops", clock.now(), readings=rows), clock)
    kept = {r.ts: r.residual_m for r in store.gauge_residuals("9414290")}
    assert all(kept.get(r.ts) == r.residual_m for r in early if r.ts < cut)


# --- app ----------------------------------------------------------------------------------------------------------


async def test_a_backfill_that_hits_the_deadline_is_asked_again_on_the_next_good_poll(monkeypatch):
    from test_ui import week_app

    monkeypatch.setattr(poll, "FETCH_DEADLINE", 0.05)

    async def never(request):
        await asyncio.sleep(30)

    app = week_app(transport=httpx.MockTransport(never))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._coverage_checked = True
        app._backfill_if_needed(force=True)
        await asyncio.wait({app._backfill}, timeout=5)
        await pilot.pause()
        assert app._coverage_checked is False


# --- review of #145: the same product from two sources, stalls and ordering ---------------------------------------

PHEB_CAP = read(TG / "PHEB_0.cap.xml").replace("<event>Tsunami Information</event>", "<event>Tsunami Warning</event>")
PHEB_URL = "https://www.tsunami.gov/events/PHEB/2026/10/24/26297050/1/WEGM42/PHEBCAP.xml"


def pheb_pair():
    """The tsunami.gov alert before and after its bulletin, and the NWS copy keyed by the same bulletin's header. The
    real skew: the CAP says 02:27:01, the WMO header minute is 02:26."""
    cap_only = tsunamigov.parse_cap(PHEB_CAP, "", PHEB_URL)
    full = tsunamigov.parse_cap(PHEB_CAP, read(TG / "PHEB_0.txt"), PHEB_URL)
    assert cap_only.dedupe_key != full.dedupe_key
    nws = replace(full, id="nws-1", source="nws", text="NWS text")
    return cap_only, full, nws


@pytest.mark.parametrize("nws_first", [True, False])
def test_a_cap_only_alert_and_the_nws_copy_are_one_alert_before_and_after_the_bulletin(now_clock, nws_first):
    cap_only, full, nws = pheb_pair()
    store, events = Store(), []
    batches = [
        Batch("nws", NOW, alerts=(nws,)),
        Batch("tsunamigov", NOW, alerts=(cap_only,), meta={"incomplete": True}),
    ]
    for batch in batches if nws_first else batches[::-1]:
        events += store.merge(batch, now_clock)
    assert len(store.active_alerts(now_clock)) == 1
    events += store.merge(Batch("tsunamigov", NOW, alerts=(full,)), now_clock)  # the bulletin lands
    assert len(store.active_alerts(now_clock)) == 1
    assert [type(e) for e in events].count(TsunamiIssued) == 1  # one toast, one bell


def segments():
    def alert(ident, source, level, areas):
        return TsunamiAlert(
            ident,
            "WEAK51 PAAQ 292351|2026-10",
            source,
            level,
            "h",
            areas,
            issued=NOW,
            expires=NOW + timedelta(hours=6),
            updated=NOW,
        )

    return (
        alert("nws-a", "nws", levels.WARNING, ("Aleutian Islands",)),
        alert("nws-b", "nws", levels.ADVISORY, ("Oregon Coast",)),
        alert("PAAQ-1-x", "tsunamigov", levels.WARNING, ("Aleutian Islands to Oregon",)),
    )


@pytest.mark.parametrize("tsunamigov_first", [True, False])
def test_nws_segments_of_different_level_stay_apart_whichever_source_arrives_first(now_clock, tsunamigov_first):
    seg_a, seg_b, tg = segments()
    store, events = Store(), []
    batches = [Batch("tsunamigov", NOW, alerts=(tg,)), Batch("nws", NOW, alerts=(seg_a, seg_b))]
    for batch in batches if tsunamigov_first else batches[::-1]:
        events += store.merge(batch, now_clock)
    shown = store.active_alerts(now_clock)
    assert sorted(a.level for a in shown) == [levels.ADVISORY, levels.WARNING]
    advisory = next(a for a in shown if a.level == levels.ADVISORY)
    assert advisory.areas == ("Oregon Coast",)  # the Advisory was never upgraded or given the Warning's areas
    assert not any(type(e).__name__ == "TsunamiChanged" for e in events)


async def test_two_feeds_that_stall_are_an_outage_not_an_empty_answer(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.3)

    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, text=EMPTY_ATOM)

    sched = Scheduler([tsunamigov.TsunamiGovSource(now_clock)], Store(), now_clock, lambda r: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await sched.poll_source("tsunamigov", client)
    assert result.error and result.batch is None and sched.statuses["tsunamigov"].health.failures == 1


async def test_a_cap_is_kept_when_its_bulletin_trickles_past_the_budget(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.3)

    class BulletinStall(Server):
        async def __call__(self, request):
            if str(request.url).endswith(".txt"):
                await asyncio.sleep(30)
            return await super().__call__(request)

    source, store = tsunamigov.TsunamiGovSource(now_clock), Store()
    async with client_for(BulletinStall("warn01")) as client:
        batch = await source.fetch(client, None)
    assert batch.meta["incomplete"] and [a.id for a in batch.alerts] == ["PAAQ-1-warn01"]
    assert len(store.merge(batch, now_clock)) == 1


async def test_a_stalled_old_entry_does_not_keep_the_newest_warning_out(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_BUDGET", 0.3)
    server = Server("newest", "oldest")
    server.hang.add("oldest")
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        first = await source.fetch(client, None)
        second = await source.fetch(client, None)  # the stalled one costs the same budget every poll
    assert [a.id for a in first.alerts] == ["PAAQ-1-newest"] and second.alerts == ()


async def test_one_stalled_entry_is_given_its_own_limit_so_the_rest_are_read(now_clock, monkeypatch):
    monkeypatch.setattr(tsunamigov, "ENTRY_LIMIT", 0.1)
    server = Server("aaaa01", "bbbb02")
    server.hang.add("aaaa01")  # read first
    source = tsunamigov.TsunamiGovSource(now_clock)
    async with client_for(server) as client:
        batch = await source.fetch(client, None)
    assert [a.id for a in batch.alerts] == ["PAAQ-1-bbbb02"] and batch.meta["incomplete"]
    assert source._failures == {}  # a stall is an outage: it never counts toward giving the product up


async def test_jma_list_json_is_inside_the_budget(monkeypatch):
    monkeypatch.setattr(jma, "ENTRY_BUDGET", 0.2)
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00")})

    async def handler(request):
        await asyncio.sleep(5)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            await feed.source.fetch(client, None)


async def test_one_stalled_jma_report_does_not_use_the_whole_budget(monkeypatch):
    monkeypatch.setattr(jma, "ENTRY_LIMIT", 0.1)
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:30:00+09:00"), "b.json": ("62", "2026-10-10T03:40:00+09:00")})

    async def handler(request):
        if request.url.path.endswith("list.json"):
            return httpx.Response(200, json=feed.entries)
        name = request.url.path.rsplit("/", 1)[1]
        if name == "a.json":
            await asyncio.sleep(5)
        return httpx.Response(200, json=feed.details[name])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await feed.source.fetch(client, None)
    assert len(batch.alerts) == 1 and batch.meta["incomplete"]
