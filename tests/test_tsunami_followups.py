"""Leftovers of the tsunami alert review: fallback alerts, shown expiry, delisted NWS alerts, warm-up, dedupe month."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FIXTURES
from test_tsunami import NOW, TG, JmaFeed, kam, merge, nws_alert, read

from earthwave.clock import ReplayClock
from earthwave.models import Batch
from earthwave.sources import jma, levels, nws, tsunamigov
from earthwave.state.store import Store
from earthwave.ui.widgets.tsunami_panel import build_panel


@pytest.fixture
def kclock() -> ReplayClock:
    return ReplayClock(NOW, real=lambda: 0.0)


def test_panel_shows_the_store_expiry_for_an_alert_without_one(kclock):
    store = Store()
    alert = replace(kam(1), expires=None)
    merge(store, kclock, alert)
    text = build_panel([alert], kclock.now(), expiry=store.expiry).plain
    assert "until cancelled" not in text
    assert f"expires in {int((store.expiry(alert) - kclock.now()).total_seconds() // 3600)}h" in text


def nws_batch(clock, *alerts, **meta) -> Batch:
    return Batch("nws", clock.now(), alerts=tuple(alerts), meta=meta)


def test_an_nws_alert_missing_from_a_complete_listing_is_withdrawn(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    store.merge(nws_batch(kclock, alert), kclock)
    assert [a.id for a in store.active_alerts(kclock)] == [alert.id]
    store.merge(nws_batch(kclock, incomplete=True), kclock)  # a partial answer proves nothing
    assert [a.id for a in store.active_alerts(kclock)] == [alert.id]
    store.merge(nws_batch(kclock), kclock)
    assert store.active_alerts(kclock) == [] and store.alert_status(alert, kclock) == "withdrawn"


def test_a_listing_does_not_withdraw_other_sources_alerts(kclock):
    store = Store()
    merge(store, kclock, kam(1))
    store.merge(nws_batch(kclock), kclock)
    assert [a.id for a in store.active_alerts(kclock)] == [kam(1).id]


def test_empty_alert_batches_do_not_warm_the_quake_store(kclock):
    for source in ("nws", "jma", "tsunamigov"):
        store = Store()
        store.merge(Batch(source, kclock.now()), kclock)
        assert not store.warm
    store.merge(Batch("usgs", kclock.now()), kclock)
    assert store.warm


def test_dedupe_key_survives_a_month_boundary():
    before = datetime(2026, 10, 31, 23, 59, 58, tzinfo=UTC)
    after = datetime(2026, 11, 1, 0, 0, 3, tzinfo=UTC)
    keys = {tsunamigov.dedupe_key("WEAK51 PAAQ 312359", sent) for sent in (before, after)}
    assert keys == {"WEAK51 PAAQ 312359|2026-10"}
    assert tsunamigov.dedupe_key("WEAK51 PAAQ 012359", after) == "WEAK51 PAAQ 012359|2026-11"
    assert (
        tsunamigov.dedupe_key("WEAK51 PAAQ 012359", datetime(2026, 11, 2, tzinfo=UTC)) == "WEAK51 PAAQ 012359|2026-11"
    )
    assert tsunamigov.dedupe_key("urn:oid:1", before) == "urn:oid:1|2026-10"  # no DDHHMM: the sent month


async def test_jma_gives_up_on_a_report_that_never_parses(caplog):
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00")})
    feed.details["a.json"] = ["not an object"]
    polls = [await feed.poll() for _ in range(jma.MAX_ENTRY_FAILURES + 3)]
    assert all(b.meta["incomplete"] for b in polls[: jma.MAX_ENTRY_FAILURES - 1])
    assert not any(b.meta["incomplete"] for b in polls[jma.MAX_ENTRY_FAILURES - 1 :])
    assert len(polls[jma.MAX_ENTRY_FAILURES - 1].alerts) == 1  # a headline from the list entry, not a silent loss
    assert caplog.text.count("giving up") == 1 and caplog.text.count("skipping") == 0


async def test_jma_keeps_retrying_through_an_outage():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00")})
    feed.fail = {"a.json"}
    for _ in range(jma.MAX_ENTRY_FAILURES + 2):
        assert (await feed.poll()).meta["incomplete"]
    feed.fail = set()
    assert len((await feed.poll()).alerts) == 1


async def fallback_for(clock, category: str):
    atom = read(TG / "PAAQAtom.xml").replace("</strong> Information<br/>", f"</strong> {category}<br/>")

    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=atom)
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>")
        return httpx.Response(200, text="<not-cap/>")  # parses as XML, is not a CAP product

    source = tsunamigov.TsunamiGovSource(clock)
    batches = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(tsunamigov.MAX_ENTRY_FAILURES + 1):
            batches.append(await source.fetch(client, None))
    return batches


async def test_an_unreadable_cap_still_raises_an_alert_from_its_atom_entry(clock):
    batches = await fallback_for(clock, "Information")
    assert all(b.alerts == () for b in batches[: tsunamigov.MAX_ENTRY_FAILURES - 1])
    (alert,) = batches[tsunamigov.MAX_ENTRY_FAILURES - 1].alerts
    assert batches[-1].alerts == ()  # given up on, then left alone
    assert alert.level == levels.INFORMATION  # the title is a region name; the level is the Category line
    assert "Bonin Islands" in alert.headline
    assert alert.issued == alert.updated == datetime(2026, 10, 25, 2, 26, 53, tzinfo=UTC)
    assert alert.dedupe_key == "WEAK53 PAAQ 250226|2026-10"  # what a CAP-built alert without a bulletin gets


async def test_the_fallback_alert_takes_its_level_from_the_category_line(clock):
    (alert,) = (await fallback_for(clock, "Warning"))[tsunamigov.MAX_ENTRY_FAILURES - 1].alerts
    assert alert.level == levels.WARNING


def listed(clock, *alerts, **meta):
    return Batch("nws", clock.now(), alerts=tuple(alerts), meta=meta)


def test_a_withdrawn_nws_alert_comes_back_when_it_is_listed_again(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    store.merge(listed(kclock, alert), kclock)
    store.merge(listed(kclock), kclock)
    assert store.active_alerts(kclock) == []
    store.merge(listed(kclock, alert), kclock)
    assert [a.id for a in store.active_alerts(kclock)] == [alert.id]


def test_relisting_never_reopens_a_cancelled_alert(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    store.merge(listed(kclock, alert), kclock)
    store._closed[alert.id] = "cancelled"
    store.merge(listed(kclock, alert), kclock)
    assert store.alert_status(alert, kclock) == "cancelled"


def test_a_record_another_source_confirmed_is_not_withdrawn_by_nws(kclock):
    store = Store()
    alert = nws_alert()
    kclock.advance_to(alert.updated)
    store.merge(listed(kclock, alert), kclock)
    store.merge(Batch("tsunamigov", kclock.now(), alerts=(replace(alert, id="cap-1", source="tsunamigov"),)), kclock)
    store.merge(listed(kclock), kclock)
    assert len(store.active_alerts(kclock)) == 1


def nws_reply(clock, payload):
    return nws.NwsSource(clock), httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


async def test_an_nws_reply_without_a_features_list_is_an_error_not_an_empty_listing(clock):
    for payload in ({}, {"features": None}, [], {"features": {}}):
        source, transport = nws_reply(clock, payload)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await source.fetch(client, None)


async def test_an_nws_reply_with_a_skipped_feature_is_incomplete(clock):
    good = json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson"))
    good["features"].append({"properties": None})
    source, transport = nws_reply(clock, good)
    async with httpx.AsyncClient(transport=transport) as client:
        batch = await source.fetch(client, None)
    assert batch.meta["incomplete"] and len(batch.alerts) == 1
    source, transport = nws_reply(clock, json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson")))
    async with httpx.AsyncClient(transport=transport) as client:
        assert not (await source.fetch(client, None)).meta["incomplete"]
