"""Tsunami sources, alert merge, rules, notifiers, attention and the tsunami UI (panel, banner, screen, replay)."""

import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, make_quake
from test_ui import wait_for

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig, Config, with_overrides
from earthwave.models import Batch, TsunamiAlert
from earthwave.net import ReplayTransport, recording_start
from earthwave.notify.desktop import NotifySend, Osascript, RecordingNotifier, WindowsToast, default_notifier
from earthwave.notify.inapp import AttentionKind, AttentionTracker, PulseGate
from earthwave.poll import Freshness, Scheduler
from earthwave.sources import jma, levels, nws, tsunamigov
from earthwave.sources.base import SourceHealth
from earthwave.state.events import (
    QuakeMajor,
    QuakeNew,
    QuakeUpdated,
    TsunamiCancelled,
    TsunamiChanged,
    TsunamiIssued,
)
from earthwave.state.rules import AlertRules, Notification
from earthwave.state.store import LATE_GRACE, Store
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.tsunami_panel import build_panel

TG = FIXTURES / "tsunamigov"
KAM = TG / "kamchatka"
NOW = datetime(2025, 7, 29, 23, 55, tzinfo=UTC)


def read(path) -> str:
    return path.read_text(encoding="utf-8")


def kam(index: int) -> TsunamiAlert:
    stem = sorted({p.name.split(".")[0] for p in KAM.iterdir()})[index]
    return tsunamigov.parse_cap(read(KAM / f"{stem}.cap.xml"), read(KAM / f"{stem}.txt"))


def kam_quake(**kwargs):
    t = datetime(2025, 7, 29, 23, 24, 52, tzinfo=UTC)
    fields = {"time": t, "lat": 52.512, "lon": 160.324, "place": "Kamchatka", **kwargs}
    return make_quake("us6000qw60", 8.8, **fields)


def nws_alert() -> TsunamiAlert:
    return nws.parse_alerts(json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson")))[0]


@pytest.fixture
def kclock() -> ReplayClock:
    return ReplayClock(NOW, real=lambda: 0.0)


def merge(store: Store, clock, *alerts, source="tsunamigov", **meta) -> list:
    return store.merge(Batch(source, clock.now(), alerts=tuple(alerts), meta=meta), clock)


# --- parsing ----------------------------------------------------------------------------------------------------


def test_level_mapping():
    pairs = {
        "Tsunami Warning": levels.WARNING,
        "Tsunami Advisory": levels.ADVISORY,
        "Tsunami Watch": levels.WATCH,
        "Tsunami Threat Message": levels.THREAT,
        "Tsunami Information Statement": levels.INFORMATION,
        "Tsunami Warning Cancellation": levels.CANCELLATION,
    }
    assert {text: levels.level_from_text(text) for text in pairs} == pairs
    assert levels.level_from_text("Flood Watch") == levels.WATCH
    assert levels.level_from_text("nothing") is None


def test_atom_entries_carry_cap_and_bulletin_links():
    entries = tsunamigov.parse_atom(read(TG / "PAAQAtom.xml"))
    assert len(entries) == 1
    assert entries[0]["cap_url"].endswith("/WEAK53/PAAQCAP.xml")
    assert entries[0]["bulletin_url"].endswith("/WEAK53/WEAK53.txt")
    assert entries[0]["updated"] == datetime(2026, 10, 25, 2, 26, 53, tzinfo=UTC)
    # PHEB's rel=self points at PAAQ; only the entry links matter.
    assert tsunamigov.parse_atom(read(TG / "PHEBAtom.xml"))[0]["cap_url"].endswith("/WEGM42/PHEBCAP.xml")


def test_bonin_information_statement_parses():
    alert = tsunamigov.parse_cap(read(TG / "PAAQ_0.cap.xml"), read(TG / "PAAQ_0.txt"))
    assert alert.level == levels.INFORMATION
    assert alert.dedupe_key == "WEAK53 PAAQ 250226|2026-10"
    assert alert.geometry == ("circle", 28.0, 139.9, 0.0)
    assert alert.origin == (datetime(2026, 10, 25, 2, 9, 24, tzinfo=UTC), 28.0, 139.9)
    assert "Bonin" in alert.headline and "M7.0" in alert.headline
    assert alert.expires == datetime(2026, 10, 25, 3, 26, 53, tzinfo=UTC)
    ptwc = tsunamigov.parse_cap(read(TG / "PHEB_0.cap.xml"), read(TG / "PHEB_0.txt"))
    assert ptwc.dedupe_key == "WEGM42 PHEB 250226|2026-10"


def test_cap_without_bulletin_builds_the_header_from_the_url():
    xml = read(TG / "PAAQ_0.cap.xml")
    url = "https://www.tsunami.gov/events/PAAQ/2026/10/25/tnfxzn/1/WEAK53/PAAQCAP.xml"
    assert tsunamigov.parse_cap(xml, "", url).dedupe_key == "WEAK53 PAAQ 250226|2026-10"


def test_kamchatka_products_parse_as_watch_warning_cancel():
    watch, warning, cancel = kam(0), kam(1), kam(2)
    assert [a.level for a in (watch, warning, cancel)] == [levels.WATCH, levels.WARNING, levels.CANCELLATION]
    assert warning.references == ("PAAQ-1-kam001",) and cancel.references == ("PAAQ-2-kam002",)
    assert warning.dedupe_key == "WEAK51 PAAQ 292351|2025-07"
    assert "Aleutian Islands" in warning.areas and warning.text.startswith("ZCZC")


def test_nws_fixture_parses_and_shares_the_dedupe_key_with_tsunamigov():
    alert = nws_alert()
    assert alert.level == levels.WARNING and alert.source == "nws"
    assert alert.dedupe_key == kam(1).dedupe_key
    assert alert.geometry[0] == "polygon" and alert.areas[0] == "Aleutian Islands"
    assert nws.parse_geometry(None) is None


def test_nws_cancel_and_unknown_events():
    feature = json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson"))["features"][0]
    cancel = json.loads(json.dumps(feature))
    cancel["properties"]["messageType"] = "Cancel"
    cancel["properties"]["references"] = [{"identifier": feature["properties"]["id"]}]
    assert nws.parse_feature(cancel).level == levels.CANCELLATION
    assert nws.parse_feature(cancel).references == (feature["properties"]["id"],)
    other = json.loads(json.dumps(feature))
    other["properties"]["event"] = "Flood Statement"
    assert nws.parse_feature(other) is None


def test_jma_detail_maps_the_code_table():
    entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
    detail = json.loads(read(FIXTURES / "jma" / "detail.json"))
    alert = jma.parse_detail(detail, entry)
    assert alert.level == levels.INFORMATION  # code 71: slight sea-level change
    assert alert.source == "jma" and "Central America" in alert.headline and "M8.0" in alert.headline
    assert alert.origin is not None and alert.origin[1:] == (7.5, -80.8)
    assert alert.areas[0] == "EASTERN PART OF PACIFIC COAST OF HOKKAIDO"
    assert [jma.KIND_LEVELS[c] for c in ("51", "52", "53", "62", "71", "50")] == [
        levels.WARNING,
        levels.WARNING,
        levels.WARNING,
        levels.ADVISORY,
        levels.INFORMATION,
        levels.CANCELLATION,
    ]
    warning = json.loads(json.dumps(detail))
    warning["Body"]["Tsunami"]["Forecast"]["Item"][0]["Category"]["Kind"]["Code"] = "51"
    assert jma.parse_detail(warning, entry).level == levels.WARNING
    warning["Head"]["InfoType"] = jma.CANCEL_INFO_TYPE
    assert jma.parse_detail(warning, entry).level == levels.CANCELLATION
    assert jma.parse_coordinate("+07.5-080.8/") == (7.5, -80.8)
    assert jma.parse_coordinate("garbage") is None


def test_jma_coordinate_with_and_without_depth():
    assert jma.parse_coordinate("+38.1+142.9-24000/") == (38.1, 142.9)  # ISO 6709 with a depth in metres
    assert jma.parse_coordinate("+38.1+142.9/") == (38.1, 142.9)
    assert jma.parse_coordinate("-12.5-076.25-10000/") == (-12.5, -76.25)
    assert jma.parse_coordinate("") is None


def test_jma_code_table_covers_no_tsunami_and_real_forecast_items():
    entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
    detail = json.loads(read(FIXTURES / "jma" / "detail.json"))
    assert jma.KIND_LEVELS["00"] == levels.INFORMATION  # "no tsunami expected"
    item = {
        "Area": {"Name": "北海道太平洋沿岸東部", "Code": "101", "enName": "EASTERN HOKKAIDO"},
        "Category": {"Kind": {"Name": "津波注意報", "Code": "62"}},
    }
    detail["Body"]["Tsunami"]["Forecast"]["Item"] = [item]
    alert = jma.parse_detail(detail, entry)
    assert alert.level == levels.ADVISORY and alert.areas == ("EASTERN HOKKAIDO",)
    item["Category"]["Kind"]["Code"] = "00"
    quiet = jma.parse_detail(detail, entry)
    assert quiet.level == levels.INFORMATION and quiet.areas == ("EASTERN HOKKAIDO",)
    detail["Body"]["Tsunami"]["Forecast"]["Item"] = item  # a lone object instead of a list
    assert jma.parse_detail(detail, entry).level == levels.INFORMATION


# --- sources over a mock transport ------------------------------------------------------------------------------


async def test_tsunamigov_source_follows_cap_links_once(clock):
    calls: list[str] = []
    bodies = {
        tsunamigov.FEEDS[0]: read(TG / "PAAQAtom.xml"),
        tsunamigov.FEEDS[1]: read(TG / "PHEBAtom.xml"),
        "https://www.tsunami.gov/events/PAAQ/2026/10/25/tnfxzn/1/WEAK53/PAAQCAP.xml": read(TG / "PAAQ_0.cap.xml"),
        "https://www.tsunami.gov/events/PAAQ/2026/10/25/tnfxzn/1/WEAK53/WEAK53.txt": read(TG / "PAAQ_0.txt"),
        "https://www.tsunami.gov/events/PHEB/2026/10/24/26297050/1/WEGM42/PHEBCAP.xml": read(TG / "PHEB_0.cap.xml"),
        "https://www.tsunami.gov/events/PHEB/2026/10/24/26297050/1/WEGM42/WEGM42.txt": read(TG / "PHEB_0.txt"),
    }

    def handler(request):
        calls.append(str(request.url))
        body = bodies.get(str(request.url))
        return httpx.Response(200, text=body) if body else httpx.Response(404)

    source = tsunamigov.TsunamiGovSource(clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await source.fetch(client, None)
        assert not first.meta["incomplete"]
        assert sorted(a.id for a in first.alerts) == ["PAAQ-1-tnfxzn", "PHEB-1-26297050"]
        count = len(calls)
        second = await source.fetch(client, None)
    assert second.alerts == () and len(calls) == count + 2  # only the two Atom feeds are fetched again
    assert source.interval(SourceHealth()) == 60.0


async def test_tsunamigov_survives_one_feed_failing_and_a_missing_bulletin(clock):
    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[1] or url.endswith(".txt"):
            return httpx.Response(500)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=read(TG / "PAAQAtom.xml"))
        return httpx.Response(200, text=read(TG / "PAAQ_0.cap.xml"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await tsunamigov.TsunamiGovSource(clock).fetch(client, None)
    assert batch.meta["incomplete"]  # one Atom feed was down
    assert [a.id for a in batch.alerts] == ["PAAQ-1-tnfxzn"]
    assert batch.alerts[0].dedupe_key.startswith("WEAK53 PAAQ")  # header rebuilt from the URL


async def test_tsunamigov_raises_when_every_feed_fails(clock):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await tsunamigov.TsunamiGovSource(clock).fetch(client, None)


async def test_nws_source_sends_event_filter_and_retries_a_429_once(clock):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, text=read(FIXTURES / "nws" / "tsunami_warning.geojson"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await nws.NwsSource(clock).fetch(client, None)
    assert len(batch.alerts) == 1 and len(seen) == 2
    assert "Tsunami+Warning" in str(seen[0].url) or "Tsunami%20Warning" in str(seen[0].url)
    assert seen[0].headers["accept"] == "application/geo+json"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await nws.NwsSource(clock).fetch(client, None)


async def test_tsunamigov_gives_up_on_a_cap_that_keeps_answering_4xx(clock, caplog):
    cap_calls = []

    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=read(TG / "PAAQAtom.xml"))
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>")
        cap_calls.append(url)
        return httpx.Response(404)

    source = tsunamigov.TsunamiGovSource(clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(tsunamigov.MAX_ENTRY_FAILURES + 3):
            await source.fetch(client, None)
    assert len(cap_calls) == tsunamigov.MAX_ENTRY_FAILURES  # then the entry is marked seen and left alone
    assert caplog.text.count("giving up") == 1


async def test_tsunamigov_keeps_retrying_through_a_cap_outage(clock):
    attempts = []

    def handler(request):
        url = str(request.url)
        if url == tsunamigov.FEEDS[0]:
            return httpx.Response(200, text=read(TG / "PAAQAtom.xml"))
        if url == tsunamigov.FEEDS[1]:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>")
        if url.endswith("CAP.xml"):
            attempts.append(url)
            if len(attempts) <= tsunamigov.MAX_ENTRY_FAILURES * 2:
                if len(attempts) % 2:
                    raise httpx.ConnectError("host down", request=request)
                return httpx.Response(503)
            return httpx.Response(200, text=read(TG / "PAAQ_0.cap.xml"))
        return httpx.Response(404)

    source = tsunamigov.TsunamiGovSource(clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(tsunamigov.MAX_ENTRY_FAILURES * 2):
            outage = await source.fetch(client, None)
            assert outage.alerts == () and outage.meta["incomplete"]  # a skipped CAP is not an answer
        batch = await source.fetch(client, None)
    assert [a.id for a in batch.alerts] == ["PAAQ-1-tnfxzn"]  # the outage outlasted the give-up count, still delivered


def test_nws_retry_after_accepts_seconds_and_http_dates():
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    assert nws.retry_after("7", now) == 7.0
    assert nws.retry_after("Sat, 10 Oct 2026 12:00:09 GMT", now) == 9.0
    assert nws.retry_after("Sat, 10 Oct 2026 11:00:00 GMT", now) == 0.0
    assert nws.retry_after(None, now) == 5.0 and nws.retry_after("soon", now) == 5.0


def test_nws_sorts_alerts_with_and_without_timestamps():
    collection = json.loads(read(FIXTURES / "nws" / "tsunami_warning.geojson"))
    bare = json.loads(json.dumps(collection["features"][0]))
    for key in ("sent", "effective", "onset"):
        bare["properties"].pop(key, None)
    bare["id"] = "bare"
    collection["features"].append(bare)
    alerts = nws.parse_alerts(collection)
    assert len(alerts) == 2 and alerts[0].issued is None  # undated sorts first, without a naive/aware comparison error


async def test_nws_source_waits_for_an_http_date_retry_after(clock):
    sleeps = []
    clock.sleep = lambda seconds: sleeps.append(seconds) or asyncio.sleep(0)
    when = (clock.now() + timedelta(seconds=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": when})
        return httpx.Response(200, text=read(FIXTURES / "nws" / "tsunami_warning.geojson"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await nws.NwsSource(clock).fetch(client, None)
    assert sleeps == [3.0]


async def test_jma_source_skips_old_reports_and_chains_references():
    entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
    detail = read(FIXTURES / "jma" / "detail.json")
    issued = datetime(2026, 10, 10, 3, 37, tzinfo=UTC)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("list.json"):
            newer = {**entry, "json": "newer.json", "ctt": "20261010040000", "rdt": "2026-10-10T04:00:00+09:00"}
            return httpx.Response(200, json=[newer, entry])
        return httpx.Response(200, text=detail)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        old_clock = ReplayClock(issued + timedelta(days=5), real=lambda: 0.0)
        assert (await jma.JmaSource(old_clock).fetch(client, None)).alerts == ()
        source = jma.JmaSource(ReplayClock(issued + timedelta(hours=1), real=lambda: 0.0))
        batch = await source.fetch(client, None)
        assert [a.id for a in batch.alerts] == [f"jma:{entry['json']}", "jma:newer.json"]
        assert batch.alerts[1].references == (f"jma:{entry['json']}",)
        assert (await source.fetch(client, None)).alerts == ()


async def test_jma_source_tolerates_missing_event_id_and_odd_payloads():
    entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
    detail = read(FIXTURES / "jma" / "detail.json")
    issued = datetime(2026, 10, 10, 3, 37, tzinfo=UTC)
    no_eid = {k: v for k, v in entry.items() if k != "eid"}

    def handler(request):
        if request.url.path.endswith("list.json"):
            return httpx.Response(200, json=["junk", 5, no_eid])
        return httpx.Response(200, text=detail)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = jma.JmaSource(ReplayClock(issued + timedelta(hours=1), real=lambda: 0.0))
        assert [a.id for a in (await source.fetch(client, None)).alerts] == [f"jma:{entry['json']}"]
        not_list = httpx.MockTransport(lambda r: httpx.Response(200, json={"oops": 1}))
    async with httpx.AsyncClient(transport=not_list) as client:
        with pytest.raises(ValueError):
            await source.fetch(client, None)
    odd = httpx.MockTransport(lambda r: httpx.Response(200, json=[entry] if r.url.path.endswith("list.json") else [1]))
    async with httpx.AsyncClient(transport=odd) as client:
        fresh = jma.JmaSource(ReplayClock(issued + timedelta(hours=1), real=lambda: 0.0))
        assert (await fresh.fetch(client, None)).alerts == ()  # a detail that is not an object is skipped


# --- merge: dedupe, supersede, cancel, expiry, quake link ---------------------------------------------------------


def test_watch_warning_cancellation_walk(kclock):
    store = Store()
    watch, warning, cancel = kam(0), kam(1), kam(2)
    assert [type(e) for e in merge(store, kclock, watch)] == [TsunamiIssued]
    events = merge(store, kclock, warning)
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.WATCH
    assert store.alert_status(watch, kclock) == "superseded"
    assert [a.id for a in store.active_alerts(kclock)] == [warning.id]
    events = merge(store, kclock, cancel)
    assert [type(e) for e in events] == [TsunamiCancelled] and events[0].alert.id == warning.id
    assert store.active_alerts(kclock) == []
    assert store.alert_status(store.get_alert(warning.id), kclock) == "cancelled"
    assert store.alert_status(store.get_alert(cancel.id), kclock) == "cancellation"


def test_same_product_from_two_sources_is_one_alert(kclock):
    store = Store()
    warning = kam(1)
    merge(store, kclock, warning)
    events = merge(store, kclock, nws_alert(), source="nws")
    assert events == []
    assert len(store.alerts()) == 1
    merged = store.get_alert(warning.id)
    assert merged.geometry[0] == "polygon"  # the NWS polygon fills the CAP circle
    assert "Kodiak Island" in merged.areas
    assert store.get_alert(nws_alert().id) is merged
    assert merge(store, kclock, nws_alert(), source="nws") == []  # polled again every minute: still nothing


def test_higher_level_wins_across_sources(kclock):
    store = Store()
    advisory = replace(nws_alert(), level=levels.ADVISORY)
    merge(store, kclock, advisory, source="nws")
    events = merge(store, kclock, kam(1))
    assert len(store.alerts()) == 1 and store.alerts()[0].level == levels.WARNING
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.ADVISORY


def jma_warning(**kwargs) -> TsunamiAlert:
    """A JMA-style warning for the Kamchatka quake: it names its origin, as the JMA parser does."""
    base = replace(kam(1), id="jma:w1", dedupe_key="jma:w1", source="jma", references=(), areas=("ALEUTIAN ISLANDS",))
    return replace(base, **kwargs)


def test_quake_and_areas_fallback_dedupe(kclock):
    # The fallback needs a quake link, which only products naming their origin (CAP, JMA) get; NWS repeats of a
    # tsunami.gov product match on the WMO header instead (test_same_product_from_two_sources_is_one_alert).
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    cap = replace(kam(1), dedupe_key="other-header", areas=("Aleutian Islands", "Hawaii"))
    merge(store, kclock, cap)
    merge(store, kclock, jma_warning(areas=("ALEUTIAN ISLANDS",)), source="jma")
    assert len(store.alerts()) == 1
    elsewhere = jma_warning(id="jma:w2", dedupe_key="jma:w2", areas=("Japan",))
    merge(store, kclock, elsewhere, source="jma")
    assert len(store.alerts()) == 2
    assert nws_alert().origin is None  # the NWS parser gives no origin, so it never takes the fallback


def test_cancellations_and_information_never_merge_into_a_warning(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    merge(store, kclock, kam(1))
    cancel = jma_warning(id="jma:c", dedupe_key="jma:c", level=levels.CANCELLATION)
    info = jma_warning(id="jma:i", dedupe_key="jma:i", level=levels.INFORMATION)
    merge(store, kclock, cancel, info, source="jma")
    assert len(store.alerts()) == 3
    assert [a.id for a in store.active_alerts(kclock, actionable_only=True)] == [kam(1).id]


def test_same_level_replacement_still_raises_changed_and_keeps_the_alarm(kclock):
    store, tracker = Store(), AttentionTracker(kclock)
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    second = kam(1)
    third = replace(
        second, id="PAAQ-4-kam004", dedupe_key="WEAK51 PAAQ 300010|2025-07", references=(second.id,), issued=NOW
    )
    events = merge(store, kclock, kam(0), second)
    tracker.observe(events)
    again = merge(store, kclock, third)
    assert [type(e) for e in again] == [TsunamiChanged]  # identical level and areas, still a replacement
    tracker.observe(again)
    tracker.tsunami_sync({a.id for a in store.active_alerts(kclock, actionable_only=True)})
    assert store.alert_status(second, kclock) == "superseded"
    assert tracker.tsunami_unacked() == 1


def test_bare_cancel_closes_only_matching_open_alerts_of_its_quake(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    merge(store, kclock, kam(1))
    elsewhere = replace(kam(2), references=(), areas=("Hawaii",))
    assert merge(store, kclock, elsewhere) == []  # other region: nothing to close, no event
    assert [a.id for a in store.active_alerts(kclock)] == [kam(1).id]
    bare = replace(kam(2), id="c2", dedupe_key="c2", references=())
    events = merge(store, kclock, bare)
    assert [type(e) for e in events] == [TsunamiCancelled]
    assert store.active_alerts(kclock) == []
    assert store.alert_status(kam(1), kclock) == "cancelled"


def test_bare_cancel_without_areas_still_ends_the_warning(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    merge(store, kclock, kam(1))
    cancel = replace(kam(2), references=(), areas=())  # empty areaDesc
    assert [type(e) for e in merge(store, kclock, cancel)] == [TsunamiCancelled]
    assert store.active_alerts(kclock) == []


def test_replacing_an_expired_alert_is_issued_not_changed(kclock):
    store = Store()
    merge(store, kclock, kam(1))
    kclock.advance(5 * 3600)  # the Warning expired at 03:51Z
    later = replace(
        kam(1), id="late", dedupe_key="late", references=(kam(1).id,), expires=kclock.now() + timedelta(hours=2)
    )
    assert [type(e) for e in merge(store, kclock, later)] == [TsunamiIssued]


def test_same_level_follow_up_toasts_without_was_or_bell():
    rules = AlertRules(AlertsConfig())
    follow = replace(kam(1), id="w3")
    plain = rules.evaluate([TsunamiChanged(follow, levels.WARNING)])[0]
    assert plain.title == "Tsunami Warning (update)" and not plain.bell and plain.urgency == "critical"
    raised = rules.evaluate([TsunamiChanged(replace(kam(1), id="w4"), levels.WATCH)])[0]
    assert raised.title == "Tsunami Warning (was Watch)" and raised.bell


def test_bare_cancel_leaves_information_alone(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    info = replace(kam(1), id="info1", dedupe_key="info1", level=levels.INFORMATION, references=())
    merge(store, kclock, info)
    assert merge(store, kclock, replace(kam(2), references=())) == []
    assert store.alert_status(info, kclock) == "active"


def test_cancel_naming_an_unknown_alert_closes_nothing(kclock):
    store = Store()
    merge(store, kclock, kam(1))
    ghost = replace(kam(2), references=("no-such-alert",), quake_ref=None, origin=None)
    assert merge(store, kclock, ghost) == []
    assert [a.id for a in store.active_alerts(kclock)] == [kam(1).id]


def test_cancel_with_a_past_expiry_still_closes_its_target(kclock):
    store = Store()
    merge(store, kclock, kam(1))
    cancel = replace(kam(2), expires=NOW - timedelta(minutes=1))
    events = merge(store, kclock, cancel)
    assert [type(e) for e in events] == [TsunamiCancelled]
    assert store.alert_status(kam(1), kclock) == "cancelled"


def test_information_referencing_a_warning_does_not_end_it(kclock):
    store = Store()
    merge(store, kclock, kam(1))
    info = replace(kam(1), id="info2", dedupe_key="info2", level=levels.INFORMATION, references=(kam(1).id,))
    assert [type(e) for e in merge(store, kclock, info)] == [TsunamiIssued]
    assert store.alert_status(kam(1), kclock) == "active"
    assert [a.id for a in store.active_alerts(kclock, actionable_only=True)] == [kam(1).id]


def test_alerts_without_an_expiry_lapse_after_the_default_ttl(kclock):
    store = Store()
    open_ended = replace(kam(1), expires=None)
    bare = replace(open_ended, id="bare", dedupe_key="bare", issued=None, updated=None, areas=("Hawaii",))
    merge(store, kclock, open_ended, bare)
    assert store.alert_status(open_ended, kclock) == "active" and store.alert_status(bare, kclock) == "active"
    kclock.advance(13 * 3600)
    assert store.alert_status(open_ended, kclock) == "expired" and store.alert_status(bare, kclock) == "expired"
    assert store.active_alerts(kclock) == []
    kclock.advance(4 * 86400)
    merge(store, kclock)
    assert store.alerts() == ()  # an alert with neither issued nor updated is pruned from when it was first seen


def test_alert_links_to_usgs_quake_in_either_order(kclock):
    store = Store()
    warning = kam(1)
    merge(store, kclock, warning)
    assert store.get_alert(warning.id).quake_ref is None
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    linked = store.get_alert(warning.id)
    assert linked.quake_ref == "us6000qw60"
    assert store.quake_for(linked).id == "us6000qw60"
    assert [a.id for a in store.alerts_for_quake("us6000qw60")] == [warning.id]
    far = kam_quake(lat=10.0, lon=20.0)
    other = Store()
    other.merge(Batch("usgs", kclock.now(), quakes=(replace(far, id="far"),)), kclock)
    merge(other, kclock, warning)
    assert other.get_alert(warning.id).quake_ref is None  # 200 km / 5 min limits


def test_alert_expires_on_the_injected_clock(kclock):
    store = Store()
    warning = kam(1)
    merge(store, kclock, warning)
    assert store.alert_status(warning, kclock) == "active"
    kclock.advance(5 * 3600)
    assert store.alert_status(warning, kclock) == "expired"
    assert store.active_alerts(kclock) == []


def test_expired_and_backfilled_products_raise_no_events(kclock):
    store = Store()
    kclock.advance(5 * 3600)
    assert merge(store, kclock, kam(1)) == []  # already expired when read (a restart)
    fresh = Store()
    kclock.advance(-5 * 3600)
    assert merge(fresh, kclock, kam(0), silent=True) == []
    assert [a.id for a in fresh.active_alerts(kclock)] == ["PAAQ-1-kam001"]


def test_alert_batches_do_not_warm_the_quake_store(kclock):
    store = Store()
    merge(store, kclock, kam(0))
    assert not store.warm


def test_old_alerts_are_pruned(kclock):
    store = Store()
    merge(store, kclock, kam(0))
    kclock.advance(4 * 86400)
    store.merge(Batch("usgs", kclock.now()), kclock)
    assert store.alerts() == ()


# --- rules, notifiers, attention --------------------------------------------------------------------------------


def test_rules_toast_tsunami_levels_but_never_information():
    rules = AlertRules(AlertsConfig())
    watch, warning = kam(0), kam(1)
    info = tsunamigov.parse_cap(read(TG / "PAAQ_0.cap.xml"), read(TG / "PAAQ_0.txt"))
    assert rules.evaluate([TsunamiIssued(info)]) == []
    notes = rules.evaluate([TsunamiIssued(watch), TsunamiChanged(warning, levels.WATCH), TsunamiCancelled(warning)])
    assert [n.title for n in notes] == ["Tsunami Watch", "Tsunami Warning (was Watch)"]
    assert all(n.bell for n in notes) and notes[1].urgency == "critical"
    assert rules.evaluate([TsunamiIssued(watch)]) == []  # one toast per alert and level
    assert AlertRules(AlertsConfig(tsunami=False)).evaluate([TsunamiIssued(watch)]) == []


def test_rules_quake_toasts_and_burst_collapse():
    rules = AlertRules(AlertsConfig(major_magnitude=6.0, pager_min="orange"))
    big = make_quake("big", 7.1)
    assert [n.title for n in rules.evaluate([QuakeNew(big), QuakeMajor(big)])] == ["Major earthquake"]
    assert rules.evaluate([QuakeMajor(big)]) == []
    yellow = make_quake("y", 5.0, pager_alert="yellow")
    orange = make_quake("o", 5.0, pager_alert="orange")
    assert rules.evaluate([QuakeNew(yellow)]) == []
    assert [n.title for n in rules.evaluate([QuakeUpdated(orange, ("pager",))])] == ["PAGER orange earthquake"]
    burst = [QuakeMajor(make_quake(f"m{i}", 6.5)) for i in range(4)]
    notes = rules.evaluate(burst)
    assert len(notes) == 1 and "4 notable quakes" in notes[0].body


async def test_recording_notifier_and_default_notifier():
    fake = RecordingNotifier()
    note = AlertRules(AlertsConfig()).evaluate([TsunamiIssued(kam(0))])[0]
    await fake.send(note)
    assert fake.sent == [note]
    assert default_notifier("win32") is not None and default_notifier("darwin") is not None


async def test_threaded_notifier_swallows_failures(caplog):
    from earthwave.notify.desktop import _ThreadedNotifier

    class Broken(_ThreadedNotifier):
        def _show(self, note):
            raise RuntimeError("blocked by policy")

    note = AlertRules(AlertsConfig()).evaluate([TsunamiIssued(kam(0))])[0]
    await Broken().send(note)
    assert "desktop notification failed" in caplog.text


def test_pulse_gate_cooldown(clock):
    gate = PulseGate(clock, cooldown=120)
    assert gate.allow("q1", "updated") and not gate.allow("q1", "updated")
    assert gate.allow("q1", "major") and gate.allow("q2", "updated")
    clock.advance(121)
    assert gate.allow("q1", "updated")


def test_repeated_updates_do_not_reflash(clock):
    tracker = AttentionTracker(clock)
    quake = make_quake("q1")
    tracker.observe([QuakeUpdated(quake, ("mag",))])
    clock.advance(5)
    tracker.prune()
    assert tracker.kind_of("q1") is None
    tracker.observe([QuakeUpdated(quake, ("mag",))])
    assert tracker.kind_of("q1") is None  # inside the 120 s cooldown
    clock.advance(130)
    tracker.observe([QuakeUpdated(quake, ("mag",))])
    assert tracker.kind_of("q1") is AttentionKind.UPDATED


def test_tsunami_attention_until_acked_or_cancelled(clock):
    tracker = AttentionTracker(clock)
    watch, warning = kam(0), kam(1)
    tracker.observe([TsunamiIssued(watch)])
    assert tracker.tsunami_unacked() == 1 and tracker.unseen() == 0
    tracker.retain(set())  # quake view changes never drop tsunami attention
    assert tracker.tsunami_unacked() == 1
    clock.advance(600)
    tracker.prune()
    assert tracker.tsunami_unacked() == 1  # no timeout
    tracker.ack_all()
    tracker.prune()
    assert tracker.tsunami_unacked() == 0
    tracker.observe([TsunamiChanged(warning, levels.WATCH)])
    assert tracker.tsunami_unacked() == 1
    tracker.observe([TsunamiCancelled(warning)])
    assert tracker.tsunami_unacked() == 0
    tracker.observe([TsunamiIssued(warning)])
    tracker.tsunami_sync(set())  # expired or superseded
    assert tracker.tsunami_unacked() == 0


BS = chr(92)
TITLE = 'Tsunami "Warning"'
BODY = f'Back{BS}slash "quoted"'


def note(**kwargs) -> Notification:
    return Notification(**{"key": "k", "title": TITLE, "body": BODY, "urgency": "critical", **kwargs})


async def test_notify_send_builds_the_command(monkeypatch):
    calls = []
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", lambda cmd, **kw: calls.append((cmd, kw)))
    await NotifySend().send(note())
    await NotifySend().send(note(urgency="warning"))
    assert calls[0][0] == ["notify-send", "-u", "critical", "-a", "earthwave", "--", TITLE, BODY]
    assert calls[1][0][2] == "normal" and calls[0][1]["timeout"] == 10


async def test_osascript_escapes_quotes_and_backslashes(monkeypatch):
    calls = []
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", lambda cmd, **kw: calls.append(cmd))
    await Osascript().send(note())
    assert calls[0][:2] == ["osascript", "-e"]
    body = f'"Back{BS}{BS}slash {BS}"quoted{BS}""'
    title = f'"Tsunami {BS}"Warning{BS}""'
    assert calls[0][2] == f"display notification {body} with title {title}"


HOSTILE = 'Quake $(Start-Process calc) `whoami` "quoted" \'single\' ]]> <b>x</b>\n"@\nStart-Process calc\x00'


def fake_run(calls, returncode=0, stderr=""):
    import types

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return types.SimpleNamespace(returncode=returncode, stderr=stderr)

    return run


async def test_windows_toast_keeps_feed_text_out_of_the_script(monkeypatch):
    from earthwave.notify.desktop import TOAST_SCRIPT

    calls = []
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", fake_run(calls))
    await WindowsToast().send(note(title=HOSTILE, body=HOSTILE))
    ((cmd, kw),) = calls
    assert cmd[-1] == TOAST_SCRIPT  # the script is the fixed constant
    assert "Start-Process" not in " ".join(cmd) and "whoami" not in " ".join(cmd)
    assert kw["env"]["EARTHWAVE_TOAST_TITLE"].startswith("Quake $(Start-Process calc)")  # the text rides in env
    assert "\x00" not in kw["env"]["EARTHWAVE_TOAST_BODY"]
    assert kw["timeout"] > 0


async def test_windows_toast_uses_the_system_powershell_and_never_touches_the_console(monkeypatch):
    calls = []
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", fake_run(calls))
    await WindowsToast().send(note())
    ((cmd, kw),) = calls
    assert cmd[0].replace("\\", "/").lower() == "c:/windows/system32/windowspowershell/v1.0/powershell.exe"
    assert "-WindowStyle" not in cmd  # it hides the user's shared console
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


async def test_windows_toast_logs_a_failing_powershell(monkeypatch, caplog):
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", fake_run([], returncode=1, stderr="blocked"))
    await WindowsToast().send(note())
    assert "blocked" in caplog.text


def test_toast_text_keeps_unicode_spaces():
    from earthwave.notify.desktop import _plain

    assert _plain("M7.0\u00a0Kuril\u3000Islands\x00\x07") == "M7.0 Kuril Islands"


async def test_windows_toast_times_out(monkeypatch, caplog):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", hang)
    await WindowsToast().send(note())
    assert "desktop notification failed" in caplog.text


async def test_notify_send_ends_options_before_feed_text(monkeypatch):
    calls = []
    monkeypatch.setattr("earthwave.notify.desktop.subprocess.run", lambda cmd, **kw: calls.append(cmd))
    await NotifySend().send(note(title="--hint=x", body="-u critical"))
    assert calls[0][-3:] == ["--", "--hint=x", "-u critical"]


def test_missing_notify_send_is_logged(monkeypatch, caplog):
    monkeypatch.setattr("earthwave.notify.desktop.shutil.which", lambda name: None)
    assert default_notifier("linux") is None
    assert "no desktop notifier" in caplog.text


def test_rules_forget_old_toast_keys(monkeypatch):
    monkeypatch.setattr("earthwave.state.rules.SENT_LIMIT", 3)
    rules = AlertRules(AlertsConfig())
    for n in range(6):
        rules.evaluate([TsunamiIssued(replace(kam(0), id=f"a{n}"))])
    assert len(rules._sent) == 3
    assert rules.evaluate([TsunamiIssued(replace(kam(0), id="a0"))])  # forgotten, so it may toast again
    assert rules.evaluate([TsunamiIssued(replace(kam(0), id="a5"))]) == []  # recent: still deduped


def test_pulse_gate_drops_expired_entries(clock):
    gate = PulseGate(clock, cooldown=120)
    for n in range(10):
        gate.allow(f"q{n}", "updated")
    clock.advance(121)
    gate.allow("fresh", "updated")
    assert len(gate._last) == 1


def test_ack_tsunami_leaves_quake_attention_alone(clock):
    tracker = AttentionTracker(clock)
    tracker.observe([QuakeNew(make_quake("q1")), TsunamiIssued(kam(1))])
    tracker.ack_tsunami()
    assert tracker.tsunami_unacked() == 0 and tracker.unseen() == 1


# --- UI ---------------------------------------------------------------------------------------------------------


def kam_app(**kwargs) -> EarthwaveApp:
    clock = ReplayClock(NOW, real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    config = replace(config, view=replace(config.view, time_zone="UTC"))  # snapshots must not depend on the machine
    store = Store(config.alerts, config.home)
    store.merge(Batch("usgs", NOW, quakes=(kam_quake(),), backfill=True), clock)
    store.merge(Batch("tsunamigov", NOW, alerts=(kam(0), kam(1))), clock)
    return EarthwaveApp(config, store=store, clock=clock, run_scheduler=False, ticks=False, **kwargs)


async def test_banner_panel_and_status_follow_the_alert():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.tracker.observe([TsunamiIssued(kam(1))])
        app.dashboard.refresh_view()
        await pilot.pause()
        banner = app.dashboard.query_one("#banner")
        assert banner.display and "TSUNAMI WARNING" in str(banner.render())
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "WARNING" in panel and "Kamchatka" in panel
        status = app.dashboard.query_one("#status")
        assert status.has_class("alert-alarm") and "TSUNAMI" in str(status.render())
        await pilot.press("a")
        await pilot.pause()
        assert not banner.display and status.has_class("alert-active") and not status.has_class("alert-alarm")


async def test_information_statements_stay_blue_without_banner():
    app = kam_app()
    info = tsunamigov.parse_cap(read(TG / "PAAQ_0.cap.xml"), read(TG / "PAAQ_0.txt"))
    assert info.level == levels.INFORMATION
    app.store = Store(app.config.alerts, app.config.home)  # only the statement: no watch or warning in force
    app.clock.advance_to(info.issued + timedelta(minutes=5))
    events = app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(info,)), app.clock)
    assert [type(e) for e in events] == [TsunamiIssued]
    assert AlertRules(app.config.alerts).evaluate(events) == []  # no toast
    async with app.run_test(size=(120, 40)) as pilot:
        app.tracker.observe(events)
        app.sync_tsunami()
        app.dashboard.refresh_view()
        await pilot.pause()
        assert app.tracker.tsunami_unacked() == 0
        assert app.dashboard.tsunami_state()[3] == "info"
        assert not app.dashboard.query_one("#banner").display
        assert not app.dashboard.query_one("#status").has_class("alert-alarm")


async def test_tsunami_state_is_a_pure_read_and_the_tick_syncs():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.tracker.observe([TsunamiIssued(replace(kam(1), id="gone"))])  # attention for an alert not in the store
        app.dashboard.tsunami_state()
        assert app.tracker.tsunami_unacked() == 1  # reading the state does not prune
        app._tick()
        await pilot.pause()
        assert app.tracker.tsunami_unacked() == 0


async def test_tick_leaves_the_tsunami_screen_table_alone():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TsunamiScreen)
        calls = []
        screen.refresh_alerts = lambda: calls.append(1)
        app._tick()
        app._tick()
        assert calls == []
        app.on_poll_done(type("M", (), {"result": type("R", (), {"events": ()})()})())
        assert calls == [1]  # refreshed on a poll


async def test_tsunami_screen_ack_only_acknowledges_tsunami_attention():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.tracker.observe([QuakeNew(kam_quake()), TsunamiIssued(kam(1))])
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert app.tracker.tsunami_unacked() == 0 and app.tracker.unseen() == 1


async def test_desktop_notifications_go_out_one_at_a_time_in_order():
    running, peak, order = 0, 0, []

    class Slow:
        async def send(self, note):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            order.append(note.key)
            running -= 1

    app = kam_app(notifier=Slow())
    async with app.run_test(size=(120, 40)) as pilot:
        events = [TsunamiIssued(replace(kam(0), id=f"t{n}")) for n in range(4)]
        app._notify_desktop(events)
        await wait_for(lambda: len(order) == 4)
        await pilot.pause()
    assert peak == 1 and order == [f"tsunami:t{n}:WATCH" for n in range(4)]


async def test_a_hung_or_raising_notifier_does_not_block_later_toasts(monkeypatch):
    monkeypatch.setattr("earthwave.app.SEND_TIMEOUT", 0.05)
    sent = []

    class Flaky:
        async def send(self, note):
            if note.key.endswith("t0:WATCH"):
                await asyncio.sleep(60)  # hung
            if note.key.endswith("t1:WATCH"):
                raise RuntimeError("blocked")
            sent.append(note.key)

    app = kam_app(notifier=Flaky())
    async with app.run_test(size=(120, 40)) as pilot:
        app._notify_desktop([TsunamiIssued(replace(kam(0), id=f"t{n}")) for n in range(3)])
        await asyncio.wait_for(app._outbox.join(), 5)
        await pilot.pause()
    assert sent == ["tsunami:t2:WATCH"]


async def test_tsunami_screen_lists_alerts_and_shows_text():
    app = kam_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TsunamiScreen)
        table = screen.query_one("DataTable")
        assert table.row_count == 2
        assert screen.selected().level == levels.WARNING  # in force first
        text = str(screen.query_one("#alert_text").render())
        assert "ZCZC" in text and "Kamchatka" in text and "us6000qw60" in text
        await pilot.press("j")
        await pilot.pause()
        assert screen.selected().level == levels.WATCH
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.dashboard


async def test_replay_walks_watch_warning_cancellation():
    """Drives the recording poll by poll on a clock that only the recording moves: no real time, no speed factor."""
    path = FIXTURES / "replay" / "kamchatka_watch_to_warning.jsonl"
    clock = ReplayClock(recording_start(path), real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    fake = RecordingNotifier()
    app = EarthwaveApp(
        config, clock=clock, transport=ReplayTransport(path, clock), notifier=fake, run_scheduler=False, ticks=False
    )
    async with app.run_test(size=(120, 40)) as pilot:

        async def poll(*names):
            for name in names:
                await app.scheduler.poll_source(name, app.client)
            await pilot.pause()
            await app._outbox.join()

        def titles():
            return [n.title for n in fake.sent]

        await poll("usgs", "usgs", "usgs", "usgs")  # the three startup backfills, then 2.5_day at 23:28
        await poll("usgs")  # 23:33
        assert clock.now() == datetime(2025, 7, 29, 23, 33, 10, tzinfo=UTC) and titles() == ["Major earthquake"]
        await poll("tsunamigov", "nws", "jma")  # 23:36 Watch
        assert titles() == ["Major earthquake", "Tsunami Watch"]
        assert [a.level for a in app.store.active_alerts(clock)] == [levels.WATCH]
        await poll("usgs")  # 23:50
        await poll("tsunamigov", "nws")  # 23:51:30 Warning, 23:52 the NWS repeat of it
        assert titles().index("Tsunami Watch") < titles().index("Tsunami Warning (was Watch)")
        assert "Major earthquake" in titles()
        warning = next(a for a in app.store.alerts() if a.level == levels.WARNING)
        assert warning.quake_ref == "us6000qw60"
        assert app.store.alert_status(warning, clock) == "active"
        assert [a.level for a in app.store.active_alerts(clock)] == [levels.WARNING]
        await poll("tsunamigov", "nws")  # 01:41 Cancel
        assert any(a.level == levels.CANCELLATION for a in app.store.alerts())
        assert app.store.alert_status(warning, clock) == "cancelled"
        assert len(app.store.alerts()) == 3  # NWS repeated the CAP warning: merged, not duplicated
        assert not app.dashboard.query_one("#banner").display
        assert app.store.active_alerts(clock) == []


def test_tsunami_screen_snapshot_120x40(snap_compare):
    async def open_screen(pilot):
        await pilot.press("t")
        await pilot.pause()

    assert snap_compare(kam_app(), terminal_size=(120, 40), run_before=open_screen)


def test_replay_fixture_covers_every_url_once_per_step():
    rows = [json.loads(line) for line in read(FIXTURES / "replay" / "kamchatka_watch_to_warning.jsonl").splitlines()]
    stamps = [r["ts"] for r in rows]
    assert stamps == sorted(stamps) and all(r["status"] == 200 for r in rows)
    assert sum(r["url"] == tsunamigov.FEEDS[0] for r in rows) == 3
    assert {r["ts"][:7] for r in rows} == {"2025-07"}  # replays as 2025 events, whatever the test clock says


# --- alert correctness: lift reports, downgrades, restarts, silent feeds ------------------------------------------


JMA_NOW = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)


class JmaFeed:
    """One JMA event served through the real source. `reports` maps a file name to (Kind code or None, report time);
    None is an observation-only report with no forecast block. Names in `fail` answer 500."""

    def __init__(self, reports: dict[str, tuple[str | None, str]]):
        entry = json.loads(read(FIXTURES / "jma" / "list.json"))[0]
        self.entries = [{**entry, "json": n, "ctt": f"2026101003374{i}"} for i, n in enumerate(reports)]
        self.details = {}
        for name, (code, reported) in reports.items():
            detail = json.loads(read(FIXTURES / "jma" / "detail.json"))
            detail["Head"]["ReportDateTime"] = reported
            if code is None:
                del detail["Body"]["Tsunami"]
            else:
                detail["Body"]["Tsunami"]["Forecast"]["Item"][0]["Category"]["Kind"]["Code"] = code
            self.details[name] = detail
        self.fail: set[str] = set()
        self.source = jma.JmaSource(ReplayClock(JMA_NOW, real=lambda: 0.0))

    async def poll(self) -> Batch:
        def handler(request):
            if request.url.path.endswith("list.json"):
                return httpx.Response(200, json=self.entries)
            name = request.url.path.rsplit("/", 1)[1]
            if name in self.fail:
                return httpx.Response(500)
            return httpx.Response(200, json=self.details[name])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await self.source.fetch(client, None)


def jma_store() -> tuple[Store, ReplayClock]:
    clock = ReplayClock(JMA_NOW, real=lambda: 0.0)
    return Store(), clock


async def test_jma_lift_report_ends_the_warning():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00"), "b.json": ("71", "2026-10-10T04:37:00+09:00")})
    feed.fail = {"b.json"}
    store, clock = jma_store()
    tracker = AttentionTracker(clock)
    first = await feed.poll()
    warning = first.alerts[0]
    assert warning.level == levels.WARNING and first.meta["incomplete"]  # b.json is still to come
    events = store.merge(first, clock)
    tracker.observe(events)
    assert [type(e) for e in events] == [TsunamiIssued]
    feed.fail = set()
    second = await feed.poll()
    lift = second.alerts[0]
    assert lift.level == levels.INFORMATION and lift.areas  # code 71 only: nothing left to warn about
    assert not second.meta["incomplete"]
    events = store.merge(second, clock)
    tracker.observe(events)
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.WARNING
    assert store.alert_status(warning, clock) == "superseded"
    assert store.active_alerts(clock, actionable_only=True) == []
    tracker.tsunami_sync({a.id for a in store.active_alerts(clock, actionable_only=True)})
    assert tracker.tsunami_unacked() == 0
    assert AlertRules(AlertsConfig()).evaluate(events) == []  # a lift is not a toast


async def test_jma_observation_only_report_does_not_end_a_live_warning():
    feed = JmaFeed({"a.json": ("51", "2026-10-10T03:37:00+09:00"), "b.json": (None, "2026-10-10T04:37:00+09:00")})
    store, clock = jma_store()
    batch = await feed.poll()
    observation = batch.alerts[1]
    assert observation.level == levels.INFORMATION and observation.areas == ()
    store.merge(batch, clock)
    assert store.alert_status(batch.alerts[0], clock) == "active"
    assert [a.id for a in store.active_alerts(clock, actionable_only=True)] == [batch.alerts[0].id]


async def test_jma_report_retried_late_does_not_end_the_newer_warning():
    feed = JmaFeed({"a.json": ("71", "2026-10-10T03:37:00+09:00"), "b.json": ("51", "2026-10-10T04:37:00+09:00")})
    feed.fail = {"a.json"}  # the older report's detail is down at first, so the newer one is merged without it
    store, clock = jma_store()
    first = await feed.poll()
    assert first.meta["incomplete"]
    store.merge(first, clock)
    feed.fail = set()
    retried = await feed.poll()
    assert retried.alerts[0].references == (first.alerts[0].id,)  # it names the newer report
    assert retried.alerts[0].level == levels.INFORMATION and retried.alerts[0].areas
    store.merge(retried, clock)
    assert store.alert_status(first.alerts[0], clock) == "active"


def test_information_from_another_source_still_ends_nothing(kclock):
    # Only a source whose reports are snapshots (JMA) lets an information statement end its predecessors.
    store = Store()
    merge(store, kclock, kam(1))
    info = replace(kam(1), id="info3", dedupe_key="info3", level=levels.INFORMATION, references=(kam(1).id,))
    merge(store, kclock, info)
    assert store.alert_status(kam(1), kclock) == "active"


def test_downgrade_is_not_swallowed_by_the_record_it_updates(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    merge(store, kclock, nws_alert(), source="nws")  # N1
    merge(store, kclock, kam(1))  # W1 repeats N1 (same WMO header) and gives the record its quake
    assert len(store.alerts()) == 1 and store.alerts()[0].quake_ref == "us6000qw60"
    downgrade = replace(
        kam(1),
        id="w2",
        dedupe_key="w2",
        level=levels.ADVISORY,
        references=(kam(1).id,),
        areas=store.alerts()[0].areas,
        issued=NOW,
    )
    events = merge(store, kclock, downgrade)
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.WARNING
    assert [(a.id, a.level) for a in store.active_alerts(kclock)] == [("w2", levels.ADVISORY)]
    assert store.alert_status(store.get_alert(kam(1).id), kclock) == "superseded"


def test_a_repeat_of_a_follow_up_still_closes_what_it_names(kclock):
    # NWS repeats a follow-up whose reference ids it spells differently; the tsunami.gov copy then names the target.
    store = Store()
    merge(store, kclock, kam(1))
    first = replace(kam(2), id="c-nws", references=("urn:not-known",), quake_ref=None, origin=None, source="nws")
    merge(store, kclock, first, source="nws")
    assert store.alert_status(kam(1), kclock) == "active"  # nothing it names is known yet
    again = replace(kam(2), id="c-cap", references=(kam(1).id,))
    events = merge(store, kclock, again)
    assert [type(e) for e in events] == [TsunamiCancelled] and events[0].alert.id == kam(1).id
    assert store.alert_status(kam(1), kclock) == "cancelled"


def test_a_batch_replaying_watch_warning_cancel_raises_no_alarm(kclock):
    store, tracker = Store(), AttentionTracker(kclock)
    events = merge(store, kclock, kam(0), kam(1), kam(2))  # a feed that keeps its entries, read after a restart
    assert not [e for e in events if isinstance(e, TsunamiIssued | TsunamiChanged)]
    assert AlertRules(AlertsConfig()).evaluate(events) == []
    tracker.observe(events)
    assert tracker.tsunami_unacked() == 0 and store.active_alerts(kclock) == []


def test_a_batch_reports_only_the_alert_that_is_live_at_its_end(kclock):
    store = Store()
    events = merge(store, kclock, kam(0), kam(1))
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].alert.id == kam(1).id
    assert events[0].previous_level == levels.WATCH


class ScriptedFeed:
    name = "tsunamigov"
    min_interval = 60.0

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    def interval(self, health):
        return 60.0

    async def fetch(self, client, since):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome() if callable(outcome) else outcome


async def test_warning_stays_in_force_until_its_feed_answers_after_expiry(kclock):
    store = Store()
    feed = ScriptedFeed([Batch("tsunamigov", NOW, alerts=(kam(1),)), RuntimeError("down"), Batch("tsunamigov", NOW)])
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since  # what Scheduler.run wires up
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        assert store.alert_status(kam(1), kclock) == "active"
        kclock.advance(5 * 3600)  # past the 03:51Z expiry with the feed erroring
        await sched.poll_source("tsunamigov", client)
        assert sched.freshness("tsunamigov") is not Freshness.FRESH
        assert store.alert_status(kam(1), kclock) == "overdue"
        assert [a.id for a in store.active_alerts(kclock, actionable_only=True)] == [kam(1).id]
        await sched.poll_source("tsunamigov", client)  # the feed answers, and nothing follows up
    assert store.alert_status(kam(1), kclock) == "expired" and store.active_alerts(kclock) == []


async def test_a_poll_that_skipped_something_or_began_before_expiry_does_not_confirm(kclock):
    store = Store()

    def slow_answer():
        kclock.advance(5 * 3600)  # the expiry passes while the request is in flight
        return Batch("tsunamigov", NOW)

    feed = ScriptedFeed(
        [
            Batch("tsunamigov", NOW, alerts=(kam(1),)),
            slow_answer,
            Batch("tsunamigov", NOW, meta={"incomplete": True}),
            Batch("tsunamigov", NOW),
        ]
    )
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        await sched.poll_source("tsunamigov", client)
        assert store.alert_status(kam(1), kclock) == "overdue"  # it began before the expiry
        await sched.poll_source("tsunamigov", client)
        assert store.alert_status(kam(1), kclock) == "overdue"  # it skipped a product
        await sched.poll_source("tsunamigov", client)
    assert store.alert_status(kam(1), kclock) == "expired"


async def test_follow_up_of_an_overdue_alert_is_a_change_and_its_cancel_a_cancellation(kclock):
    store = Store()
    feed = ScriptedFeed([Batch("tsunamigov", NOW, alerts=(kam(1),)), RuntimeError("down")])
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        kclock.advance(5 * 3600)
        await sched.poll_source("tsunamigov", client)
    assert store.alert_status(kam(1), kclock) == "overdue"
    later = replace(
        kam(1), id="w3", dedupe_key="w3", references=(kam(1).id,), expires=kclock.now() + timedelta(hours=1)
    )
    events = merge(store, kclock, later)
    assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.WARNING
    early = ReplayClock(NOW, real=lambda: 0.0)
    store2 = Store()
    store2.answered_since = lambda source, when: False  # a feed that has said nothing since
    merge(store2, early, kam(1))
    early.advance(5 * 3600)
    assert store2.alert_status(kam(1), early) == "overdue"
    events = merge(store2, early, replace(kam(2), expires=None))
    assert [type(e) for e in events] == [TsunamiCancelled] and events[0].alert.id == kam(1).id


def test_panel_never_reads_as_an_all_clear_while_feeds_are_down(kclock):
    assert build_panel([], NOW).plain == "No tsunami alerts in force"
    every = build_panel([], NOW, down=("tsunamigov failed", "nws stale"), unknown=True).plain
    assert "UNKNOWN" in every and "tsunamigov failed, nws stale" in every and "No tsunami alerts" not in every
    one = build_panel([], NOW, down=("jma failed",)).plain
    assert "UNKNOWN" not in one and "Partial: jma failed" in one and "No tsunami alerts in force" in one
    late = build_panel([kam(1)], NOW + timedelta(hours=5), overdue=frozenset({kam(1).id}), down=("nws failed",)).plain
    assert "OVERDUE" in late and "Partial: nws failed" in late


def failing_app(handler=None, start=NOW + timedelta(hours=5)) -> EarthwaveApp:
    """An app on the replay clock whose requests go to `handler`, by default all failing."""

    def down(request):
        raise httpx.ConnectError("network down")

    clock = ReplayClock(start, speed=500)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    return EarthwaveApp(config, clock=clock, transport=httpx.MockTransport(handler or down))


async def test_offline_start_does_not_say_all_clear():
    app = failing_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.tsunami_feeds_down()[0] and app.tsunami_feeds_down()[1])
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "UNKNOWN" in panel and "tsunamigov failed" in panel and "No tsunami alerts" not in panel


async def test_one_failing_feed_is_a_partial_notice_not_an_unknown():
    nws_body = read(FIXTURES / "nws" / "tsunami_warning.geojson")

    def handler(request):
        if "weather.gov" in str(request.url):
            return httpx.Response(200, text=nws_body)
        raise httpx.ConnectError("network down")

    app = failing_app(handler, start=NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.scheduler.freshness("jma") is Freshness.ERROR)
        await wait_for(lambda: app.store.alerts())
        app.dashboard.refresh_view()
        await pilot.pause()
        down, every = app.tsunami_feeds_down()
        assert "nws" not in [n for n, _ in down]
        assert not every  # nws answered
        assert "UNKNOWN" not in str(app.dashboard.query_one("#tsunami").render())


async def test_warning_stays_on_screen_when_its_feed_dies_past_expiry():
    nws_body = read(FIXTURES / "nws" / "tsunami_warning.geojson")
    state = {"up": True}

    def handler(request):
        if state["up"] and "weather.gov" in str(request.url):
            return httpx.Response(200, text=nws_body)
        raise httpx.ConnectError("network down")

    app = failing_app(handler, start=NOW)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.tracker.tsunami_unacked() == 1)  # issued while running, through the merge path
        warning = app.store.alerts()[0]
        state["up"] = False
        app.clock.advance(5 * 3600)  # past the 03:51Z expiry
        await wait_for(lambda: app.scheduler.statuses["nws"].health.failures > 0)
        app.sync_tsunami()
        app.dashboard.refresh_view()
        await pilot.pause()
        assert app.store.alert_status(warning, app.clock) == "overdue"
        assert app.tracker.tsunami_unacked() == 1
        assert app.dashboard.query_one("#banner").display
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "WARNING" in panel and "OVERDUE" in panel


async def test_restart_replay_in_an_incomplete_batch_raises_no_events(kclock):
    kclock.advance(5 * 3600)  # the Warning expired long before this start
    store = Store()
    feed = ScriptedFeed([Batch("tsunamigov", NOW, alerts=(kam(1),), meta={"incomplete": True})])
    results = []
    sched = Scheduler([feed], store, kclock, results.append)
    store.answered_since = sched.answered_since
    async with httpx.AsyncClient() as client:
        result = await sched.poll_source("tsunamigov", client)
    assert result.events == ()
    assert store.alert_status(kam(1), kclock) == "expired" and store.active_alerts(kclock) == []


async def test_overdue_ends_even_if_the_feed_never_completes_a_poll(kclock):
    store = Store()
    incomplete = Batch("tsunamigov", NOW, meta={"incomplete": True})
    feed = ScriptedFeed([Batch("tsunamigov", NOW, alerts=(kam(1),), meta={"incomplete": True}), incomplete, incomplete])
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        assert sched.statuses["tsunamigov"].health.incomplete
        kclock.advance(5 * 3600)
        await sched.poll_source("tsunamigov", client)
        assert store.alert_status(kam(1), kclock) == "overdue"
        kclock.advance(7 * 3600)  # more than OVERDUE_MAX after the 03:51Z expiry
        await sched.poll_source("tsunamigov", client)
    assert store.alert_status(kam(1), kclock) == "expired"


async def test_jma_advisory_retried_late_does_not_end_the_newer_warning():
    feed = JmaFeed({"a.json": ("62", "2026-10-10T03:37:00+09:00"), "b.json": ("51", "2026-10-10T04:37:00+09:00")})
    feed.fail = {"a.json"}
    store, clock = jma_store()
    first = await feed.poll()
    store.merge(first, clock)
    feed.fail = set()
    retried = await feed.poll()
    assert retried.alerts[0].level == levels.ADVISORY and retried.alerts[0].references == (first.alerts[0].id,)
    store.merge(retried, clock)
    assert store.alert_status(first.alerts[0], clock) == "active"
    assert store.alert_status(retried.alerts[0], clock) == "active"


def test_panel_checks_before_it_clears():
    assert build_panel([], NOW, checking=True).plain == "Checking tsunami feeds"


async def test_no_fresh_feed_never_reads_as_all_clear():
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    app = EarthwaveApp(config, clock=ReplayClock(NOW, real=lambda: 0.0), run_scheduler=False, ticks=False)
    async with app.run_test(size=(120, 40)) as pilot:
        app.scheduler_running = True  # polling is on, but nothing has answered yet
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "Checking tsunami feeds" in panel and "No tsunami alerts" not in panel
        app.scheduler.statuses["nws"].health.failures = 1  # one feed fails while the others are still loading
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = str(app.dashboard.query_one("#tsunami").render())
        assert "UNKNOWN" in panel and "nws failed" in panel and "No tsunami alerts" not in panel


async def test_warning_survives_a_healthy_feed_answering_a_little_late_and_its_follow_up_is_a_change(kclock):
    store = Store()
    feed = ScriptedFeed(
        [Batch("tsunamigov", NOW, alerts=(kam(1),)), Batch("tsunamigov", NOW), Batch("tsunamigov", NOW)]
    )
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    expiry = store.expiry(kam(1))
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        kclock.advance((expiry - kclock.now()).total_seconds() + 300)  # five minutes past the expiry
        await sched.poll_source("tsunamigov", client)  # the feed answers healthily; no follow-up yet
        assert sched.freshness("tsunamigov") is Freshness.FRESH
        assert store.alert_status(kam(1), kclock) == "overdue"
        assert [a.id for a in store.active_alerts(kclock, actionable_only=True)] == [kam(1).id]
        later = replace(
            kam(1), id="w2", dedupe_key="w2", references=(kam(1).id,), expires=kclock.now() + timedelta(hours=1)
        )
        events = merge(store, kclock, later)
        assert [type(e) for e in events] == [TsunamiChanged] and events[0].previous_level == levels.WARNING
        assert store.alert_status(kam(1), kclock) == "superseded"


async def test_warning_lapses_for_real_once_the_grace_has_passed(kclock):
    store = Store()
    feed = ScriptedFeed(
        [Batch("tsunamigov", NOW, alerts=(kam(1),)), Batch("tsunamigov", NOW), Batch("tsunamigov", NOW)]
    )
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    expiry = store.expiry(kam(1))
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        kclock.advance((expiry - kclock.now()).total_seconds() + 60)
        await sched.poll_source("tsunamigov", client)
        assert store.alert_status(kam(1), kclock) == "overdue"
        kclock.advance(LATE_GRACE.total_seconds())
        await sched.poll_source("tsunamigov", client)  # healthy, answered, nothing followed up
    assert store.alert_status(kam(1), kclock) == "expired" and store.active_alerts(kclock) == []


def test_restart_replay_of_an_alert_first_merged_inside_the_grace_raises_nothing(kclock):
    store = Store()
    store.answered_since = lambda source, when: True
    kclock.advance((store.expiry(kam(1)) - kclock.now()).total_seconds() + 300)  # replay lands 5 min past expiry
    assert merge(store, kclock, kam(1)) == []
    assert store.alert_status(kam(1), kclock) == "expired"


async def test_panel_says_awaiting_follow_up_during_the_grace_with_a_fresh_feed(kclock):
    store = Store()
    feed = ScriptedFeed([Batch("tsunamigov", NOW, alerts=(kam(1),)), Batch("tsunamigov", NOW)])
    sched = Scheduler([feed], store, kclock, lambda result: None)
    store.answered_since = sched.answered_since
    async with httpx.AsyncClient() as client:
        await sched.poll_source("tsunamigov", client)
        kclock.advance((store.expiry(kam(1)) - kclock.now()).total_seconds() + 300)
        await sched.poll_source("tsunamigov", client)
    assert sched.freshness("tsunamigov") is Freshness.FRESH
    alert = kam(1)
    overdue = frozenset(a.id for a in store.active_alerts(kclock) if store.alert_status(a, kclock) == "overdue")
    panel = build_panel([alert], kclock.now(), overdue=overdue, expiry=store.expiry).plain
    assert "OVERDUE, awaiting follow-up" in panel and "not confirmed" not in panel
