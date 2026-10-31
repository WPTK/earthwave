"""Tsunami sources, alert merge, rules, notifiers, attention and the tsunami UI (panel, banner, screen, replay)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, T0, make_quake
from test_ui import wait_for

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig, Config, with_overrides
from earthwave.models import Batch, TsunamiAlert
from earthwave.net import ReplayTransport, recording_start
from earthwave.notify.desktop import RecordingNotifier, default_notifier
from earthwave.notify.inapp import AttentionKind, AttentionTracker, PulseGate
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
from earthwave.state.rules import AlertRules
from earthwave.state.store import Store
from earthwave.ui.screens.tsunami import TsunamiScreen

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


def test_quake_and_areas_fallback_dedupe(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    cap = replace(kam(1), dedupe_key="other-header")
    merge(store, kclock, cap)
    local = replace(nws_alert(), dedupe_key="another-header", areas=("ALEUTIAN ISLANDS",), origin=cap.origin)
    merge(store, kclock, local, source="nws")
    assert len(store.alerts()) == 1
    elsewhere = replace(local, id="x2", dedupe_key="k3", areas=("Hawaii",))
    merge(store, kclock, elsewhere, source="nws")
    assert len(store.alerts()) == 2


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
    kclock.advance(4 * 3600)
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


def test_cancel_without_references_closes_the_quakes_open_alerts(kclock):
    store = Store()
    store.merge(Batch("usgs", kclock.now(), quakes=(kam_quake(),)), kclock)
    merge(store, kclock, kam(1))
    bare = replace(kam(2), references=())
    events = merge(store, kclock, bare)
    assert [type(e) for e in events] == [TsunamiCancelled]


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
    app.clock.advance_to(info.issued + timedelta(minutes=5))
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(replace(info, id="i1"),)), app.clock)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.dashboard.tsunami_state()[3] in ("info", "none")


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
    path = FIXTURES / "replay" / "kamchatka_watch_to_warning.jsonl"
    clock = ReplayClock(recording_start(path), speed=900)
    config = with_overrides(Config(), 2.5, 24, None, "ascii")
    fake = RecordingNotifier()
    app = EarthwaveApp(config, clock=clock, transport=ReplayTransport(path, clock), notifier=fake)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: any(a.level == levels.WARNING for a in app.store.alerts()))
        await pilot.pause()
        warning = next(a for a in app.store.alerts() if a.level == levels.WARNING)
        assert warning.quake_ref == "us6000qw60"
        assert app.store.alert_status(warning, clock) in ("active", "cancelled")  # live, not expired
        await wait_for(lambda: any(a.level == levels.CANCELLATION for a in app.store.alerts()))
        await pilot.pause()
        await asyncio.sleep(0.05)
        titles = [n.title for n in fake.sent]
        assert "Tsunami Watch" in titles and "Tsunami Warning (was Watch)" in titles
        assert "Major earthquake" in titles
        assert titles.index("Tsunami Watch") < titles.index("Tsunami Warning (was Watch)")
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
    assert T0.year == 2026  # fixtures replay as 2025 events on the replay clock, not the test clock
