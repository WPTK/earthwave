"""Tsunami correctness, wave 1: an impossible feed expiry (#245), a cancellation read before its warning (#162), and
a tsunami screen that must not read as calm while the feeds are down (#227, #248) or hide a date (#254)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from uat.harness import kam, kam_alert_app, screen_text

from earthwave.clock import ReplayClock
from earthwave.config import AlertsConfig
from earthwave.models import Batch, TsunamiAlert
from earthwave.poll import Freshness
from earthwave.state.events import TsunamiIssued
from earthwave.state.store import DEFAULT_TTL, Store
from earthwave.ui.screens.tsunami import TsunamiScreen

NOW = datetime(2026, 11, 1, 11, 35, tzinfo=UTC)


def _store() -> tuple[Store, ReplayClock]:
    return Store(AlertsConfig(), None), ReplayClock(NOW, real=lambda: 0.0)


def _alert(ident: str, level: str, **kw) -> TsunamiAlert:
    base = {
        "id": ident,
        "dedupe_key": ident,
        "source": "tsunamigov",
        "level": level,
        "headline": f"{level} {ident}",
        "issued": NOW - timedelta(minutes=3),
        "updated": NOW - timedelta(minutes=3),
    }
    return TsunamiAlert(**{**base, **kw})


def _merge(store, clock, *alerts, source="tsunamigov"):
    return store.merge(Batch(source, clock.now(), alerts=tuple(alerts)), clock)


# --- #245 ---------------------------------------------------------------------------------------------------------


def test_a_warning_whose_expiry_precedes_its_issue_time_is_still_raised_and_in_force():
    store, clock = _store()
    warning = _alert("w", "WARNING", expires=NOW - timedelta(hours=11))  # sent 11:32, "expires" 00:32
    events = _merge(store, clock, warning)
    assert [type(e) for e in events if isinstance(e, TsunamiIssued)] == [TsunamiIssued]
    assert store.alert_status(warning, clock) == "active"
    assert [a.id for a in store.active_alerts(clock)] == ["w"]
    assert store.expiry(warning) == warning.updated + DEFAULT_TTL  # the default lifetime stands in


def test_an_impossible_expiry_lapses_on_the_default_lifetime_not_never():
    store, clock = _store()
    warning = _alert("w", "WARNING", expires=NOW - timedelta(hours=11))
    _merge(store, clock, warning)
    clock.advance_to(NOW + timedelta(hours=6))
    assert store.alert_status(warning, clock) == "active"
    clock.advance_to(NOW + DEFAULT_TTL + timedelta(hours=1))
    assert store.alert_status(warning, clock) != "active"


def test_a_sane_expiry_is_still_believed():
    store, _clock = _store()
    warning = _alert("w", "WARNING", expires=NOW + timedelta(hours=1))
    assert store.expiry(warning) == NOW + timedelta(hours=1)


# --- #162 ---------------------------------------------------------------------------------------------------------


def test_a_cancellation_without_references_merged_first_still_ends_the_warning_read_after_it():
    store, clock = _store()
    warning = _alert("w", "WARNING", quake_ref="us1", issued=NOW - timedelta(hours=2), expires=NOW + timedelta(hours=4))
    cancel = _alert("c", "CANCELLATION", quake_ref="us1", issued=NOW - timedelta(minutes=5), expires=None)
    _merge(store, clock, cancel)  # newest-first reading: the cancellation is merged before the warning it ends
    events = _merge(store, clock, warning)
    assert not any(isinstance(e, TsunamiIssued) for e in events)
    assert store.alert_status(warning, clock) == "cancelled"
    assert store.active_alerts(clock) == []


def test_a_cancellation_without_references_does_not_end_a_newer_warning_or_another_quakes():
    store, clock = _store()
    cancel = _alert("c", "CANCELLATION", quake_ref="us1", issued=NOW - timedelta(hours=2))
    newer = _alert(
        "w1", "WARNING", quake_ref="us1", issued=NOW - timedelta(minutes=5), expires=NOW + timedelta(hours=4)
    )
    other = _alert(
        "w2", "WARNING", quake_ref="us2", issued=NOW - timedelta(minutes=1), expires=NOW + timedelta(hours=4)
    )
    _merge(store, clock, cancel)
    _merge(store, clock, newer, other)
    assert {a.id for a in store.active_alerts(clock)} == {"w1", "w2"}


# --- #227, #248, #254 ---------------------------------------------------------------------------------------------


def _feeds_failing(app) -> None:
    down = [(n, Freshness.ERROR) for n in ("tsunamigov", "nws", "jma")]
    app.tsunami_feeds_down = lambda: (down, True)


async def test_the_tsunami_screen_says_the_status_is_unknown_when_every_feed_is_failing():
    app = kam_alert_app()
    _feeds_failing(app)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TsunamiScreen)
        text = screen_text(app)
        assert "status UNKNOWN, feeds not answering: tsunamigov failed, nws failed, jma failed" in text, text
        assert "No tsunami products received yet" not in text


async def test_the_unknown_line_also_tops_a_screen_that_has_alerts():
    app = kam_alert_app(1)
    _feeds_failing(app)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        text = screen_text(app)
        assert "status UNKNOWN" in text and "WARNING" in text, text


async def test_offline_the_tsunami_screen_says_nothing_was_checked_not_nothing_received():
    app = kam_alert_app(offline=True)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        text = screen_text(app)
        assert "not checked (offline)" in text, text
        assert "No tsunami products received yet" not in text


async def test_a_healthy_empty_screen_still_reads_as_nothing_received():
    app = kam_alert_app()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        text = screen_text(app)
        assert "No tsunami products received yet" in text and "UNKNOWN" not in text and "offline" not in text


async def test_an_expired_products_expires_cell_shows_the_date_the_detail_shows():
    app = kam_alert_app()
    warning = replace(kam(1), expires=app.clock.now() - timedelta(hours=1), issued=app.clock.now() - timedelta(hours=7))
    app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=(warning,)), app.clock)
    assert app.store.alert_status(warning, app.clock) == "expired"
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        table = app.screen.query_one("#alert_table")
        cell = table.get_cell(warning.id, list(table.columns)[4])
        assert cell.strip() and "2025-07-29" in cell, repr(cell)
        assert "Expires  2025-07-29" in screen_text(app)
