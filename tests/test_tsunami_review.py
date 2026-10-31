"""Deep-review fixes for the tsunami path: partial feeds, test products, relisting, late retries, JMA areas."""

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from conftest import FIXTURES
from test_tsunami import NOW, TG, JmaFeed, kam, merge, nws_alert, read
from test_ui import wait_for

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import Config, SourcesConfig, with_overrides
from earthwave.models import Batch
from earthwave.sources import jma, levels, nws, tsunamigov
from earthwave.state.events import TsunamiIssued
from earthwave.state.store import Store
from earthwave.ui.widgets.tsunami_panel import build_panel

EMPTY_FEED = "<feed xmlns='http://www.w3.org/2005/Atom'/>"


@pytest.fixture
def kclock() -> ReplayClock:
    return ReplayClock(NOW, real=lambda: 0.0)


def tsunamigov_only_app(handler, *, offline=False) -> EarthwaveApp:
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, sources=SourcesConfig(usgs=False, tsunamigov=True, nws=False, jma=False))
    clock = ReplayClock(NOW, real=lambda: 0.0)
    return EarthwaveApp(config, clock=clock, transport=httpx.MockTransport(handler), offline=offline)


# --- 1. one feed down is partial, never clear ------------------------------------------------------------------


async def test_one_tsunamigov_feed_down_reads_partial_not_clear():
    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=EMPTY_FEED)
        return httpx.Response(500)  # PHEBAtom: the Pacific centre

    app = tsunamigov_only_app(handler)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.scheduler.statuses["tsunamigov"].health.incomplete)
        app.dashboard.refresh_view()
        await pilot.pause()
        down, none_fresh = app.tsunami_feeds_down()
        assert [n for n, _ in down] == ["tsunamigov"] and not none_fresh
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "Partial: tsunamigov incomplete" in panel and "in force" not in panel


def test_panel_with_a_partial_feed_does_not_say_in_force():
    text = build_panel([], NOW, down=("tsunamigov incomplete",)).plain
    assert "Partial: tsunamigov incomplete" in text and "in force" not in text


async def test_offline_panel_says_nothing_was_checked():
    app = tsunamigov_only_app(lambda request: httpx.Response(500), offline=True)
    async with app.run_test(size=(120, 40)) as pilot:
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "not checked" in panel and "No tsunami alerts" not in panel


# --- 2. test and exercise products ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["Test", "Exercise", "System", "Draft"])
def test_a_cap_that_is_not_actual_is_dropped(status):
    xml = read(TG / "PAAQ_0.cap.xml")
    assert tsunamigov.parse_cap(xml, read(TG / "PAAQ_0.txt")) is not None
    assert tsunamigov.parse_cap(xml.replace("<status>Actual</status>", f"<status>{status}</status>")) is None


async def test_a_test_product_raises_nothing_and_is_not_refetched(kclock):
    test_cap = read(TG / "PHEB_0.cap.xml").replace("<status>Actual</status>", "<status>Test</status>")
    cap_calls = []

    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=EMPTY_FEED)
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text=read(TG / "PHEBAtom.xml"))
        if url.endswith(".xml") and "/events/PHEB/" in url:
            cap_calls.append(url)
            return httpx.Response(200, text=test_cap)
        return httpx.Response(404)  # the bulletin text

    source = tsunamigov.TsunamiGovSource(kclock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await source.fetch(client, None)
        calls = len(cap_calls)
        await source.fetch(client, None)
    assert calls >= 1 and len(cap_calls) == calls  # marked seen: not fetched again
    assert first.alerts == () and not first.meta["incomplete"]
    assert Store().merge(first, kclock) == []


def test_an_nws_feature_that_is_not_actual_is_dropped():
    data = json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson"))
    assert len(nws.parse_features(data)[0]) == 1
    data["features"][0]["properties"]["status"] = "Exercise"
    assert nws.parse_features(data) == ((), 0)


async def test_the_nws_request_asks_for_actual_alerts_only(kclock):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"features": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await nws.NwsSource(kclock).fetch(client, None)
    assert seen[0]["status"] == "actual"


# --- 3. a relisted NWS alert raises its attention again ------------------------------------------------------


def listed(clock, *alerts, **meta):
    return Batch("nws", clock.now(), alerts=tuple(alerts), meta=meta)


def test_a_withdrawn_nws_alert_relisted_raises_an_issued_event(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    assert [type(e) for e in store.merge(listed(kclock, alert), kclock)] == [TsunamiIssued]
    assert store.merge(listed(kclock), kclock) == []
    assert store.merge(listed(kclock), kclock) == []  # omitted twice in a row: withdrawn
    assert store.active_alerts(kclock) == []
    events = store.merge(listed(kclock, alert), kclock)
    assert [type(e) for e in events] == [TsunamiIssued] and events[0].alert.id == alert.id
    assert store.merge(listed(kclock, alert), kclock) == []  # and only once


# --- 4. a product that arrives after its own cancellation ----------------------------------------------------


def test_a_warning_retried_after_its_cancellation_is_never_in_force(kclock):
    warning = kam(1)
    cancel = replace(
        warning,
        id="cancel-b",
        dedupe_key="cancel-b",
        level=levels.CANCELLATION,
        references=(warning.id,),
        issued=warning.issued + timedelta(minutes=30),
    )
    store = Store()
    kclock.advance_to(cancel.issued)
    merge(store, kclock, cancel)  # the cancel got through first: its target was not held yet
    events = merge(store, kclock, warning)  # then the warning's CAP fetch succeeded
    assert events == [] and store.active_alerts(kclock) == []
    assert store.alert_status(warning, kclock) == "cancelled"


def test_a_jma_warning_retried_after_its_cancellation_is_never_in_force(kclock):
    cancel = replace(
        kam(1),
        id="jma:b",
        dedupe_key="jma:b",
        source="jma",
        level=levels.CANCELLATION,
        issued=NOW + timedelta(minutes=30),
        expires=None,
    )
    late = replace(kam(1), id="jma:a", dedupe_key="jma:a", source="jma", references=(cancel.id,), issued=NOW)
    store = Store()
    kclock.advance_to(cancel.issued)
    merge(store, kclock, cancel, source="jma")
    assert merge(store, kclock, late, source="jma") == [] and store.active_alerts(kclock) == []


# --- 5. JMA areas and giving up -------------------------------------------------------------------------------


def test_jma_lists_only_the_areas_at_the_reports_level():
    detail = json.loads(read(FIXTURES / "jma" / "detail.json"))
    entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
    items = detail["Body"]["Tsunami"]["Forecast"]["Item"]
    for item, (code, name) in zip(
        items, [("51", "Ishikawa"), ("62", "Niigata"), ("50", "Toyama"), ("71", "Hokkaido")], strict=False
    ):
        item["Category"]["Kind"]["Code"] = code
        item["Area"]["enName"] = name
    alert = jma.parse_detail(detail, entry)
    assert alert.level == levels.WARNING and alert.areas == ("Ishikawa", "Niigata")  # warning and advisory areas
    for item in items:  # nothing but a lift and forecasts: the report keeps every coded area
        item["Category"]["Kind"]["Code"] = "50"
    assert len(jma.parse_detail(detail, entry).areas) == len(items)


async def test_jma_gives_up_with_an_alert_from_the_list_entry():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00")})
    feed.details["a.json"] = ["not an object"]
    polls = [await feed.poll() for _ in range(jma.MAX_ENTRY_FAILURES + 2)]
    assert all(b.alerts == () for b in polls[: jma.MAX_ENTRY_FAILURES - 1])
    (alert,) = polls[jma.MAX_ENTRY_FAILURES - 1].alerts
    assert alert.source == "jma" and alert.id == "jma:a.json" and "Details unavailable" in alert.text
    assert polls[-1].alerts == ()  # then left alone


# --- 7. the narrow layout keeps the tsunami text ----------------------------------------------------------------


async def test_unknown_status_is_readable_below_120_columns():
    app = tsunamigov_only_app(lambda request: httpx.Response(500))
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(lambda: app.tsunami_feeds_down()[1])
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = app.dashboard.query_one("#tsunami")
        assert app.dashboard.has_class("narrow") and panel.display and panel.size.height > 0
        assert "UNKNOWN" in str(panel.render())


# --- review round 2 ---------------------------------------------------------------------------------------------


def test_one_nws_listing_blip_does_not_touch_an_alert_and_alternating_listings_never_re_alarm(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    assert [type(e) for e in store.merge(listed(kclock, alert), kclock)] == [TsunamiIssued]
    for _ in range(3):  # omitted, listed, omitted, listed ...: never two omissions in a row
        assert store.merge(listed(kclock), kclock) == []
        assert [a.id for a in store.active_alerts(kclock)] == [alert.id]
        assert store.merge(listed(kclock, alert), kclock) == []


def test_a_withdrawn_nws_alert_replaced_in_the_relisting_batch_is_not_revived(kclock):
    store = Store()
    n1 = nws_alert()
    n2 = replace(n1, id="n2", dedupe_key="n2", level=levels.ADVISORY, references=(n1.id,), updated=n1.updated)
    kclock.advance_to(n1.updated)
    store.merge(listed(kclock, n1), kclock)
    store.merge(listed(kclock), kclock)
    store.merge(listed(kclock), kclock)
    assert store.alert_status(n1, kclock) == "withdrawn"
    events = store.merge(listed(kclock, n1, n2), kclock)
    assert [type(e).__name__ for e in events] == ["TsunamiChanged"]  # one event, not Issued as well
    assert store.alert_status(n1, kclock) == "superseded"
    assert [a.id for a in store.active_alerts(kclock)] == ["n2"]


def test_a_relisted_alert_whose_level_changed_emits_one_event(kclock):
    store = Store()
    n1 = replace(nws_alert(), level=levels.ADVISORY)
    kclock.advance_to(n1.updated)
    store.merge(listed(kclock, n1), kclock)
    store.merge(listed(kclock), kclock)
    store.merge(listed(kclock), kclock)
    upgraded = replace(n1, level=levels.WARNING, updated=n1.updated + timedelta(minutes=1))
    kclock.advance_to(upgraded.updated)
    events = store.merge(listed(kclock, upgraded), kclock)
    assert [type(e).__name__ for e in events] == ["TsunamiChanged"]  # one event, not Changed and Issued


def jma_report(rid, level, issued, areas=("Ishikawa",), references=()):
    return replace(
        kam(1),
        id=f"jma:{rid}",
        dedupe_key=f"jma:{rid}",
        source="jma",
        level=level,
        areas=areas,
        issued=issued,
        updated=issued,
        references=references,
        expires=None,
    )


def test_a_jma_warning_retried_after_a_downgrade_is_not_issued(kclock):
    a = jma_report("a", levels.WARNING, NOW)
    b = jma_report("b", levels.ADVISORY, NOW + timedelta(minutes=30))  # a was skipped: b does not name it
    late = replace(a, references=(b.id,))
    store = Store()
    kclock.advance_to(b.issued)
    merge(store, kclock, b, source="jma")  # a timed out, so b merged first
    assert merge(store, kclock, late, source="jma") == []
    assert [x.id for x in store.active_alerts(kclock)] == [b.id]


def test_an_observation_only_report_does_not_close_a_late_warning(kclock):
    a = jma_report("a", levels.WARNING, NOW)
    obs = jma_report("b", levels.INFORMATION, NOW + timedelta(minutes=30), areas=(), references=(a.id,))
    store = Store()
    kclock.advance_to(obs.issued)
    merge(store, kclock, obs, source="jma")
    assert [type(e) for e in merge(store, kclock, replace(a, references=(obs.id,)), source="jma")] == [TsunamiIssued]


def test_a_cancellation_without_a_time_ends_the_warning_in_either_order(kclock):
    warning = kam(1)
    cancel = replace(
        warning, id="cancel-b", dedupe_key="cancel-b", level=levels.CANCELLATION, references=(warning.id,), issued=None
    )
    first = Store()
    merge(first, kclock, cancel)
    assert merge(first, kclock, warning) == [] and first.active_alerts(kclock) == []
    second = Store()
    merge(second, kclock, warning)
    assert second.alert_status(warning, kclock) == "active"
    merge(second, kclock, cancel)
    assert second.active_alerts(kclock) == []  # the same result whichever arrives first


def test_a_jma_fallback_alert_replaces_nothing(kclock):
    real = jma_report("a", levels.WARNING, NOW)
    fallback = replace(
        jma.fallback_alert({"json": "b", "en_ttl": "Tsunami Advisory", "rdt": "2026-10-10T04:00:00+09:00"}),
        references=(real.id,),
        issued=NOW + timedelta(minutes=30),
    )
    store = Store()
    kclock.advance_to(fallback.issued)
    merge(store, kclock, real, source="jma")
    merge(store, kclock, fallback, source="jma")
    assert store.alert_status(real, kclock) == "active"


def test_a_newer_jma_fallback_does_not_close_a_late_real_report(kclock):
    real = jma_report("a", levels.WARNING, NOW)
    fallback = replace(
        jma.fallback_alert({"json": "b", "en_ttl": "Tsunami Warning", "rdt": "2026-10-10T04:00:00+09:00"}),
        references=(real.id,),
        issued=NOW + timedelta(minutes=30),
    )
    store = Store()
    kclock.advance_to(fallback.issued)
    merge(store, kclock, fallback, source="jma")
    late = replace(real, references=(fallback.id,))
    assert [type(e) for e in merge(store, kclock, late, source="jma")] == [TsunamiIssued]
    assert store.alert_status(late, kclock) == "active" and late.areas == ("Ishikawa",)
