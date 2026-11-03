"""Aftershock clustering (#323): the rule, its invariants as properties, and the grouped quake list."""

from dataclasses import replace
from datetime import timedelta

from conftest import WEEK_NOW, make_quake, wait_until, week_app
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from earthwave.state.clusters import cluster_aftershocks, group_by_mainshock, window_days, window_km
from earthwave.ui.widgets.quake_list import QuakeList

FAST = settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])

quakes = st.lists(
    st.builds(
        make_quake,
        quake_id=st.uuids().map(str),
        mag=st.floats(2.5, 9.0).map(lambda m: round(m, 1)),
        minutes_ago=st.integers(0, 60 * 24 * 90),
        lat=st.floats(-1.0, 1.0),  # close together, so clusters actually form
        lon=st.floats(-1.0, 1.0),
    ),
    max_size=25,
    unique_by=lambda q: q.id,
)


def snapshot(clusters):
    return {c.mainshock.id: tuple(q.id for q in c.aftershocks) for c in clusters}


def test_the_windows_grow_with_magnitude_and_match_gardner_knopoff():
    assert round(window_km(6.0)) == 53 and round(window_days(6.0)) == 499
    assert round(window_days(7.0)) == 918 and round(window_km(7.0)) == 71
    assert all(
        window_km(m + 0.1) > window_km(m) and window_days(m + 0.1) > window_days(m) for m in (3.0, 5.0, 6.4, 7.0)
    )


def test_a_nearby_smaller_later_quake_is_an_aftershock_and_a_far_or_early_one_is_not():
    main = make_quake("main", 6.5, minutes_ago=600)
    after = make_quake("after", 4.5, minutes_ago=300, lat=10.1)
    before = make_quake("before", 4.0, minutes_ago=900)  # a foreshock is not grouped
    far = make_quake("far", 4.0, minutes_ago=300, lat=40.0)
    late = make_quake("late", 4.0, minutes_ago=600 - int(window_days(6.5) * 1440) - 5)
    clusters = cluster_aftershocks([late, far, before, after, main])
    assert snapshot(clusters) == {"main": ("after",)}


def test_a_quake_of_equal_magnitude_is_not_an_aftershock():
    first, second = make_quake("a", 5.0, minutes_ago=100), make_quake("b", 5.0, minutes_ago=50)
    assert cluster_aftershocks([first, second]) == []


def test_an_aftershock_is_never_a_mainshock_so_a_chain_collapses_to_the_biggest():
    big = make_quake("big", 7.0, minutes_ago=3000)
    mid = make_quake("mid", 5.5, minutes_ago=2000)
    small = make_quake("small", 4.0, minutes_ago=1000)
    assert snapshot(cluster_aftershocks([small, mid, big])) == {"big": ("mid", "small")}


@FAST
@given(quakes, st.randoms(use_true_random=False))
def test_clusters_do_not_depend_on_the_order_of_the_input(rows, rnd):
    shuffled = list(rows)
    rnd.shuffle(shuffled)
    assert cluster_aftershocks(rows) == cluster_aftershocks(shuffled)


@FAST
@given(quakes)
def test_no_quake_is_in_two_clusters_and_no_aftershock_is_a_mainshock(rows):
    clusters = cluster_aftershocks(rows)
    members = [q.id for c in clusters for q in (c.mainshock, *c.aftershocks)]
    assert len(members) == len(set(members))
    assert all(q.id in {r.id for r in rows} for c in clusters for q in c.aftershocks)


@FAST
@given(quakes)
def test_a_mainshock_is_never_grouped_under_a_smaller_quake_and_members_follow_it(rows):
    for cluster in cluster_aftershocks(rows):
        assert cluster.aftershocks
        for q in cluster.aftershocks:
            assert q.mag < cluster.mainshock.mag and q.time > cluster.mainshock.time


def grouped_app():
    """The week's quakes plus a M7.4 mainshock off Bonin with three aftershocks, at the captured instant."""
    app = week_app()
    main = make_quake("main", 7.4, time=WEEK_NOW - timedelta(hours=5), lat=27.0, lon=140.0, place="Bonin Islands")
    after = [
        make_quake(f"after{i}", 4.6 + i / 10, time=WEEK_NOW - timedelta(hours=4 - i), lat=27.1, lon=140.1)
        for i in range(3)
    ]
    from earthwave.models import Batch

    app.store.merge(Batch("usgs", WEEK_NOW, quakes=(main, *after)), app.clock)
    return app


async def test_g_groups_aftershocks_under_the_mainshock_and_space_opens_them():
    app = grouped_app()
    async with app.run_test(size=(120, 40)) as pilot:
        quakes_ = app.dashboard.query_one(QuakeList)
        await wait_until(pilot, lambda: quakes_.rows)
        plain = [q.id for q in quakes_.rows]
        assert {"main", "after0", "after1", "after2"} <= set(plain)
        await pilot.press("g")
        await wait_until(pilot, lambda: len(quakes_.rows) < len(plain), what="grouping")
        ids = [q.id for q in quakes_.rows]
        groups = group_by_mainshock(quakes_._all_rows())
        assert {"after0", "after1", "after2"} <= {q.id for q in groups["main"]}
        folded = {q.id for members in groups.values() for q in members}
        assert ids == [i for i in plain if i not in folded]  # the order is the sort order, minus the aftershocks
        width = quakes_.scrollable_content_region.width
        row = quakes_.render_line(ids.index("main"))
        assert row.cell_length == width and f"[+{len(groups['main'])}] Bonin Islands" in row.text
        quakes_.cursor = ids.index("main")
        closed = len(quakes_.rows)
        await pilot.press("space")
        await wait_until(pilot, lambda: len(quakes_.rows) > closed, what="expanding")
        ids = [q.id for q in quakes_.rows]
        at = ids.index("main")
        count = len(groups["main"])
        assert [q.id for q in quakes_.rows[at + 1 : at + 1 + count]] == [q.id for q in groups["main"]]
        assert f"[-{count}] Bonin Islands" in quakes_.render_line(at).text
        assert all(quakes_.render_line(at + n).cell_length == width for n in range(count + 1))
        assert "└ " in quakes_.render_line(at + 1).text
        quakes_.cursor = at + 2  # from an aftershock, Space closes its group and lands on the mainshock
        await pilot.press("space")
        await wait_until(pilot, lambda: len(quakes_.rows) == closed, what="collapsing")
        assert quakes_.selected.id == "main"
        await pilot.press("g")
        await wait_until(pilot, lambda: [q.id for q in quakes_.rows] == plain, what="ungrouping")


async def test_the_group_mark_keeps_a_wide_place_inside_the_row():
    app = grouped_app()
    async with app.run_test(size=(120, 40)) as pilot:
        quakes_ = app.dashboard.query_one(QuakeList)
        await wait_until(pilot, lambda: quakes_.rows)
        await pilot.press("g")
        await wait_until(pilot, lambda: quakes_.group)
        width = quakes_.scrollable_content_region.width
        main = next(q for q in quakes_.rows if q.id == "main")
        wide = replace(main, place="東京湾 近海 の 地震 が 発生 しました 東京湾 近海 の 地震 が 発生 しました")
        assert quakes_._row_strip(wide, False, width).cell_length == width


def test_grouped_dashboard_snapshot(snap_compare):
    app = grouped_app()

    async def run_before(pilot):
        await pilot.press("g")
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 40), run_before=run_before)
