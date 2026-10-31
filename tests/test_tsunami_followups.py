"""Leftovers of the tsunami alert review: fallback alerts, shown expiry, delisted NWS alerts, warm-up, dedupe month."""

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from test_tsunami import NOW, JmaFeed, kam, merge, nws_alert

from earthwave.clock import ReplayClock
from earthwave.models import Batch
from earthwave.sources import jma, levels, tsunamigov
from earthwave.state.store import Store
from earthwave.ui.widgets.tsunami_panel import build_panel


@pytest.fixture
def kclock() -> ReplayClock:
    return ReplayClock(NOW, real=lambda: 0.0)


def atom_with(title: str, updated: str = "2026-10-25T02:26:53Z") -> str:
    return (
        "<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>urn:uuid:bad-1</id>"
        f"<title>{title}</title><updated>{updated}</updated>"
        "<link rel='alternate' href='https://www.tsunami.gov/events/PAAQ/x/WEAK53.txt'/>"
        "<link type='application/cap+xml' href='https://www.tsunami.gov/events/PAAQ/x/PAAQCAP.xml'/>"
        "</entry></feed>"
    )


async def test_an_unreadable_cap_still_raises_an_alert_from_its_atom_entry(clock):
    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=atom_with("Tsunami Warning Number 2"))
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>")
        return httpx.Response(200, text="<not-cap/>")  # parses as XML, is not a CAP product

    source = tsunamigov.TsunamiGovSource(clock)
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(tsunamigov.MAX_ENTRY_FAILURES):
            seen.append((await source.fetch(client, None)).alerts)
        later = await source.fetch(client, None)
    assert all(alerts == () for alerts in seen[:-1])
    (alert,) = seen[-1]
    assert alert.id == "urn:uuid:bad-1" and alert.level == levels.WARNING
    assert alert.headline == "Tsunami Warning Number 2" and alert.issued == datetime(
        2026, 10, 25, 2, 26, 53, tzinfo=UTC
    )
    assert later.alerts == ()  # given up on, then left alone


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
    assert caplog.text.count("giving up") == 1 and caplog.text.count("skipping") == 0


async def test_jma_keeps_retrying_through_an_outage():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00")})
    feed.fail = {"a.json"}
    for _ in range(jma.MAX_ENTRY_FAILURES + 2):
        assert (await feed.poll()).meta["incomplete"]
    feed.fail = set()
    assert len((await feed.poll()).alerts) == 1
