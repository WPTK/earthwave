"""LIVE, STALE and OFFLINE (#321): one data-freshness word, from the scheduler's per-source health on the injected clock."""

from datetime import timedelta

import pytest
from conftest import T0, wait_until
from test_ui import mock_transport, week_app

from earthwave.models import Batch
from earthwave.poll import STALE_AFTER, DataState, Freshness, Scheduler
from earthwave.state.store import Store
from earthwave.ui.glyphs import UNICODE
from earthwave.ui.theme import FRESHNESS_COLORS
from earthwave.ui.widgets.status_bar import SourceLine, StatusInfo, build_status


class Quiet:
    min_interval = 60.0

    def __init__(self, name):
        self.name = name

    def interval(self, health):
        return 60.0

    async def fetch(self, client, since):
        return Batch(self.name, T0)


def scheduler(clock, *names):
    return Scheduler([Quiet(n) for n in names], Store(), clock, lambda r: None)


def good(sched, name, clock):
    health = sched.statuses[name].health
    health.failures, health.last_ok, health.incomplete = 0, clock.now(), False


def fail(sched, name):
    sched.statuses[name].health.failures += 1


def test_nothing_answered_yet_is_loading_never_live(clock):
    sched = scheduler(clock, "usgs", "nws")
    assert sched.data_state() == (DataState.LOADING, None)
    assert scheduler(clock).data_state() == (DataState.LOADING, None)


def test_live_turns_stale_after_120_s_without_a_good_poll(clock):
    sched = scheduler(clock, "usgs", "nws")
    good(sched, "usgs", clock)
    good(sched, "nws", clock)
    clock.advance(STALE_AFTER)
    assert sched.data_state() == (DataState.LIVE, STALE_AFTER)  # "more than" 120 s
    clock.advance(1)
    state, age = sched.data_state()
    assert state is DataState.STALE and age == STALE_AFTER + 1
    good(sched, "usgs", clock)
    good(sched, "nws", clock)
    assert sched.data_state() == (DataState.LIVE, 0.0)


def test_stale_age_is_the_oldest_feed_that_is_not_fine(clock):
    sched = scheduler(clock, "usgs", "nws")
    good(sched, "nws", clock)
    clock.advance(300)
    good(sched, "usgs", clock)
    assert sched.data_state() == (DataState.STALE, 300.0)  # nws is old, usgs is fresh: the set is not current


def test_a_partly_failing_set_is_stale_and_all_failing_is_offline(clock):
    sched = scheduler(clock, "usgs", "nws")
    good(sched, "usgs", clock)
    good(sched, "nws", clock)
    clock.advance(30)
    fail(sched, "nws")
    assert sched.data_state() == (DataState.STALE, 30.0)
    fail(sched, "usgs")
    assert sched.data_state() == (DataState.OFFLINE, 30.0)  # the newest good data anywhere
    good(sched, "usgs", clock)
    assert sched.data_state()[0] is DataState.STALE
    good(sched, "nws", clock)
    assert sched.data_state()[0] is DataState.LIVE


def test_never_answered_and_failing_is_offline_with_no_age(clock):
    sched = scheduler(clock, "usgs")
    fail(sched, "usgs")
    assert sched.data_state() == (DataState.OFFLINE, None)


def test_an_incomplete_feed_is_not_live(clock):
    sched = scheduler(clock, "usgs", "jma")
    good(sched, "usgs", clock)
    good(sched, "jma", clock)
    sched.statuses["jma"].health.incomplete = True
    assert sched.data_state()[0] is DataState.STALE


def test_a_slow_feed_is_late_after_two_of_its_intervals_not_120_s(clock):
    sched = scheduler(clock, "usgs")
    sched.statuses["usgs"].interval = 300.0
    good(sched, "usgs", clock)
    clock.advance(599)
    assert sched.data_state()[0] is DataState.LIVE
    clock.advance(2)
    assert sched.data_state()[0] is DataState.STALE


def line(name, fresh=Freshness.FRESH, age=5.0):
    return SourceLine(name, fresh, age)


def test_the_chip_prints_the_word_and_age_in_the_palette_colours():
    for state, age, word, fresh in (
        (DataState.LIVE, 8.0, " LIVE 8s ", Freshness.FRESH),
        (DataState.STALE, 200.0, " STALE 3m ", Freshness.STALE),
        (DataState.OFFLINE, 700.0, " OFFLINE 11m ", Freshness.ERROR),
        (DataState.OFFLINE, None, " OFFLINE ", Freshness.ERROR),
        (DataState.LOADING, None, " LOADING ", Freshness.LOADING),
    ):
        text = build_status(StatusInfo((line("usgs"),), 5, 9, 30, "M4.5+", 0, data_state=state, data_age=age), UNICODE)
        assert text.plain.startswith(word), text.plain
        assert any(f"on {FRESHNESS_COLORS[fresh]}" in str(span.style) for span in text.spans)


def test_no_chip_for_a_replay_and_offline_is_the_same_word():
    base = {"sources": (), "shown": 1, "total": 1, "next_poll": None, "filter_text": "f", "unseen": 0}
    replay = build_status(StatusInfo(**base, replay=True, data_state=DataState.LIVE), UNICODE).plain
    assert "LIVE" not in replay and "REPLAY" in replay
    offline = build_status(StatusInfo(**base, offline=True, data_state=None), UNICODE).plain
    assert offline.startswith(" OFFLINE ") and "offline, showing saved history" in offline  # as the toast says
    assert not build_status(StatusInfo(**base), UNICODE).plain.strip().startswith(("LIVE", "OFFLINE"))


def test_failing_feed_names_stay_visible_at_100_columns_after_the_chip():
    sources = (line("usgs", Freshness.ERROR, 700.0), line("nws", Freshness.ERROR, None), line("jma", Freshness.ERROR))
    info = StatusInfo(
        sources, 50, 90, 30, "M4.5+ 72h", 0, alert_state="active", data_state=DataState.OFFLINE, data_age=700
    )
    text = build_status(info, UNICODE)
    assert text.plain[:100].count("failed") >= 1 and "usgs" in text.plain[:100] and "OFFLINE 11m" in text.plain[:100]


def test_a_warning_ground_keeps_the_chip_legible_and_the_word_printed():
    info = StatusInfo(
        (line("usgs", Freshness.ERROR),),
        1,
        1,
        None,
        "f",
        0,
        alert_state="alarm",
        alert_level="WARNING",
        data_state=DataState.OFFLINE,
        data_age=10.0,
    )
    text = build_status(info, UNICODE)
    assert " OFFLINE 10s " in text.plain
    chip = next(s for s in text.spans if text.plain[s.start : s.end] == " OFFLINE 10s ")
    assert "#ffffff on #000000" in str(chip.style)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_the_dashboard_shows_the_state_and_never_live_during_an_outage(size):
    app = week_app(transport=mock_transport([]), run_scheduler=True)  # every feed answers 404: a real outage
    async with app.run_test(size=size) as pilot:
        await wait_until(
            pilot, lambda: app.scheduler.data_state()[0] in (DataState.STALE, DataState.OFFLINE), what="an outage"
        )
        app.dashboard.refresh_status()
        await pilot.pause()
        bar = str(app.dashboard.query_one("#status").render())
        assert bar.startswith((" STALE", " OFFLINE")) and "LIVE" not in bar, bar


def test_poll_timeline_with_the_replay_clock_never_reads_live_after_a_failure(clock):
    sched = scheduler(clock, "usgs")
    good(sched, "usgs", clock)
    seen = []
    for _ in range(6):
        clock.advance(timedelta(seconds=30).total_seconds())
        seen.append(sched.data_state()[0])
    assert seen == [DataState.LIVE] * 4 + [DataState.STALE] * 2  # at 120 s still LIVE, then STALE
    fail(sched, "usgs")
    assert sched.data_state()[0] is DataState.OFFLINE
