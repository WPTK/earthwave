"""Aftershock clustering (#323): the rule, its invariants as properties, and the grouped quake list."""

import random
import time
from dataclasses import replace
from datetime import timedelta

from conftest import WEEK_NOW, make_quake, wait_until, week_app
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from earthwave.sources.geo import haversine_km
from earthwave.state.clusters import Cluster, cluster_aftershocks, group_by_mainshock, window_days, window_km
from earthwave.state.store import SortKey
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


def test_the_window_has_no_gap_near_m6_5_and_stays_finite_for_the_biggest_quakes():
    for mag in (6.4, 6.5, 6.6):  # G-K's two fits meet at 6.5 with a small step down, not a hole
        assert 0.9 < window_days(mag + 0.1) / window_days(mag) < 1.3 and window_km(mag) > 0
    assert 1000 < window_days(9.1) < 1200 and 100 < window_km(9.1) < 150


def test_a_magnitude_that_is_not_a_number_is_left_out():
    main = make_quake("main", 6.0, minutes_ago=600)
    after = make_quake("after", 4.0, minutes_ago=300)
    bad = [make_quake("nan", float("nan"), minutes_ago=200), make_quake("inf", float("inf"), minutes_ago=100)]
    assert snapshot(cluster_aftershocks([*bad, after, main])) == {"main": ("after",)}


def test_the_antimeridian_and_the_poles_group_neighbours_that_are_far_apart_in_longitude():
    west = make_quake("west", 6.0, minutes_ago=600, lat=10.0, lon=179.9)
    east = make_quake("east", 4.0, minutes_ago=300, lat=10.0, lon=-179.9)  # 22 km away across the date line
    assert snapshot(cluster_aftershocks([east, west])) == {"west": ("east",)}
    north = make_quake("north", 6.0, minutes_ago=600, lat=89.9, lon=0.0)
    across = make_quake("across", 4.0, minutes_ago=300, lat=89.9, lon=180.0)  # 22 km away over the pole
    assert snapshot(cluster_aftershocks([across, north])) == {"north": ("across",)}
    south = make_quake("south", 6.0, minutes_ago=600, lat=-89.95, lon=10.0)
    opposite = make_quake("opposite", 4.0, minutes_ago=300, lat=-89.95, lon=-170.0)
    assert snapshot(cluster_aftershocks([opposite, south])) == {"south": ("opposite",)}


def scatter(count, seed, lat, lon, minutes):
    rng = random.Random(seed)
    return [
        make_quake(
            f"q{i}",
            round(rng.uniform(2.5, 7.5), 1),
            minutes_ago=rng.randint(0, minutes),
            lat=rng.uniform(*lat),
            lon=rng.uniform(*lon),
        )
        for i in range(count)
    ]


def test_five_thousand_quakes_cluster_well_inside_a_second():
    rows = scatter(5000, 7, (-80, 80), (-180, 180), 43200)
    start = time.perf_counter()
    clusters = cluster_aftershocks(rows)
    elapsed = time.perf_counter() - start
    assert clusters and elapsed < 2.0  # about 0.2 s here; the bound is generous for slow runners


def reference_clusters(rows):
    """The same rule as one all-pairs scan, to compare the banded search against."""
    claimed, out = set(), []
    for main in sorted(rows, key=lambda q: (-q.mag, q.time, q.id)):
        if main.id in claimed:
            continue
        horizon = window_days(main.mag) * 86400
        members = [
            q
            for q in rows
            if q.id not in claimed
            and q.mag < main.mag
            and 0 < (q.time - main.time).total_seconds() <= horizon
            and haversine_km(main.lat, main.lon, q.lat, q.lon) <= window_km(main.mag)
        ]
        if members:
            claimed.update(q.id for q in members)
            out.append(Cluster(main, tuple(sorted(members, key=lambda q: (q.time, q.id)))))
    return out


def test_the_banded_search_gives_the_same_clusters_as_a_plain_scan():
    # a thin strip across the date line, so the latitude bands and the longitude wrap are both exercised
    rows = [replace(q, lon=(q.lon + 180) % 360 - 180) for q in scatter(400, 3, (-4, 4), (175, 185), 20000)]
    assert cluster_aftershocks(rows) == reference_clusters(rows)
    assert cluster_aftershocks(rows)


def grouped_app():
    """The week's quakes plus a M7.4 mainshock off Bonin with three aftershocks, at the captured instant."""
    app = week_app()
    main = make_quake("main", 7.4, time=WEEK_NOW - timedelta(hours=5), lat=27.0, lon=140.0, place="Bonin Islands")
    after = [
        make_quake(
            f"after{i}",
            4.6 + i / 10,
            time=WEEK_NOW - timedelta(hours=4 - i),
            lat=27.1,
            lon=140.1,
            place=f"Aftershock {i}",
        )
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
        groups = group_by_mainshock(quakes_._source)
        assert {"after0", "after1", "after2"} <= {q.id for q in groups["main"]}
        folded = {q.id for members in groups.values() for q in members}
        assert ids == [i for i in plain if i not in folded]  # the order is the sort order, minus the aftershocks
        screen = " ".join(quakes_.render_line(y).text for y in range(quakes_.size.height))
        assert "Aftershock" not in screen and "after0" not in ids  # folded rows are really gone from the list
        assert quakes_.visible_ids().isdisjoint(folded)  # and a new one is not marked seen while it is folded
        assert quakes_.total == len(plain)  # the status bar still counts them
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
        members = {q.id for q in groups["main"]}
        assert [q.id for q in quakes_.rows[at + 1 : at + 1 + count]] == [
            q.id for q in quakes_._source if q.id in members
        ]
        assert f"[-{count}] Bonin Islands" in quakes_.render_line(at).text
        assert all(quakes_.render_line(at + n).cell_length == width for n in range(count + 1))
        assert "└ Aftershock" in quakes_.render_line(at + 1).text
        quakes_.cursor = at + 2  # from an aftershock, Space closes its group and lands on the mainshock
        await pilot.press("space")
        await wait_until(pilot, lambda: len(quakes_.rows) == closed, what="collapsing")
        assert quakes_.selected.id == "main"
        await pilot.press("g")
        await wait_until(pilot, lambda: [q.id for q in quakes_.rows] == plain, what="ungrouping")


async def test_opening_a_group_or_polling_does_not_cluster_again_and_keeps_the_sort_order(monkeypatch):
    calls = []

    def counted(rows):
        calls.append(len(rows))
        return group_by_mainshock(rows)

    monkeypatch.setattr("earthwave.ui.widgets.quake_list.group_by_mainshock", counted)
    app = grouped_app()
    async with app.run_test(size=(120, 40)) as pilot:
        quakes_ = app.dashboard.query_one(QuakeList)
        await wait_until(pilot, lambda: quakes_.rows)
        app.sort = SortKey.MAG
        await pilot.press("g")
        await wait_until(pilot, lambda: quakes_.group)
        assert len(calls) == 1
        quakes_.cursor = [q.id for q in quakes_.rows].index("main")
        await pilot.press("space")
        await wait_until(pilot, lambda: "main" in quakes_._expanded)
        app.dashboard.refresh_view()  # a poll with nothing new
        app.dashboard.refresh_view()
        assert len(calls) == 1
        ids = [q.id for q in quakes_.rows]
        at = ids.index("main")
        children = [q.id for q in app.store.visible(app.view, app.clock, SortKey.MAG) if q.id.startswith("after")]
        assert ids[at + 1 : at + 4] == children == ["after2", "after1", "after0"]  # by magnitude, across polls
        later = make_quake("after9", 4.7, time=WEEK_NOW - timedelta(hours=1), lat=27.1, lon=140.1)
        from earthwave.models import Batch

        app.store.merge(Batch("usgs", WEEK_NOW, quakes=(later,)), app.clock)
        app.dashboard.refresh_view()
        assert len(calls) == 2  # a new quake does cluster again


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
