"""Tsunami layer of the map: source ring, geometry tint, level priority, blinking, dashboard wiring, snapshots."""

from dataclasses import replace
from datetime import UTC, datetime

from conftest import make_quake
from rich.style import Style
from test_tsunami import kam, kam_app, kam_quake, nws_alert
from textual.app import App

from earthwave.clock import ReplayClock
from earthwave.notify.inapp import AttentionTracker
from earthwave.sources.geo import haversine_km
from earthwave.state.events import TsunamiIssued
from earthwave.ui.glyphs import ASCII, UNICODE
from earthwave.ui.widgets.braille import WORLD, project_cell, unproject
from earthwave.ui.widgets.map_panel import (
    COAST_STYLE,
    MapModel,
    MapPanel,
    base_lines,
    geometry_cells,
    place_tsunami,
    row_segments,
    tsunami_marks,
    zoom_view,
)

BLANK = "⠀"
KAM_WARNING = replace(kam(1), geometry=("circle", 52.512, 160.324, 1500.0), quake_ref="us6000qw60")
KAM_WATCH = replace(kam(0), geometry=("circle", 52.512, 160.324, 4000.0))
ALEUTIAN_WARNING = nws_alert()  # polygon over the Aleutians and Bering Sea, crosses the antimeridian


def centre(col, row, view, cols, rows):
    return unproject(col * 2 + 0.5, row * 4 + 1.5, view, cols * 2, rows * 4)


def test_marks_pick_the_source_in_order():
    quake = kam_quake()
    assert tsunami_marks([KAM_WARNING], lambda a: quake)[0].source == (52.512, 160.324)  # linked quake wins
    assert tsunami_marks([KAM_WARNING], lambda a: None)[0].source == (52.5, 160.3)  # the product's origin
    no_origin = replace(KAM_WARNING, origin=None)
    assert tsunami_marks([no_origin], lambda a: None)[0].source == (52.512, 160.324)  # circle centre
    mark = tsunami_marks([replace(no_origin, geometry=None)], lambda a: None)[0]
    assert mark.source is None and mark.geometry is None and mark.level == "WARNING"


def test_circle_tints_exactly_the_coast_cells_inside_the_radius():
    cols, rows = 160, 40
    cells = geometry_cells(KAM_WARNING.geometry, WORLD, cols, rows, False)
    base = base_lines(cols, rows, WORLD, False)
    expected = {
        (c, r)
        for r, line in enumerate(base)
        for c, ch in enumerate(line)
        if ch != BLANK and haversine_km(*centre(c, r, WORLD, cols, rows)[::-1], 52.512, 160.324) <= 1500
    }
    assert len(cells) > 3 and set(cells) == expected
    assert geometry_cells(("circle", 52.5, 160.3, 0.0), WORLD, cols, rows, False) == frozenset()  # a point has no area


def test_polygon_tint_follows_the_nws_polygon_in_a_view_past_the_dateline():
    cols, rows = 160, 40
    view = zoom_view(180, 55, 40, cols, rows)
    assert view.lon0 < 180 < view.lon1
    cells = geometry_cells(ALEUTIAN_WARNING.geometry, view, cols, rows, False)
    lons = [centre(c, r, view, cols, rows)[0] for c, r in cells]
    assert cells and all(184 <= lon <= 205 for lon in lons)  # -176..-155 degrees seen from a view that runs past 180
    assert geometry_cells(ALEUTIAN_WARNING.geometry, zoom_view(0, 0, 40, cols, rows), cols, rows, False) == frozenset()
    ascii_cells = geometry_cells(ALEUTIAN_WARNING.geometry, view, cols, rows, True)
    assert ascii_cells and len(ascii_cells) >= len(cells) // 2  # the ascii raster is tinted the same way


def test_source_ring_flanks_the_quake_and_never_covers_a_marker():
    cols, rows = 160, 40
    mark = tsunami_marks([KAM_WARNING], lambda a: kam_quake())[0]
    _, rings = place_tsunami([mark], WORLD, cols, rows, False)
    col, row = project_cell(160.324, 52.512, WORLD, cols, rows)
    assert {k: m.glyph for k, m in rings.items()} == {(col - 1, row): "(", (col + 1, row): ")"}
    m = MapModel()
    m.resize(cols, rows)
    m.set_quakes([kam_quake()], None)
    m.set_tsunami([mark])
    glyphs = {k: v.glyph for k, v in m.frame().markers.items()}
    assert (glyphs[(col, row)], glyphs[(col - 1, row)], glyphs[(col + 1, row)]) == ("◉", "(", ")")
    left_lon, left_lat = centre(col - 1, row, WORLD, cols, rows)
    near = make_quake("near", 5.0, lat=left_lat, lon=left_lon)  # lands in the left ring cell
    m.set_quakes([kam_quake(), near], None)
    assert m.frame().markers[(col - 1, row)].glyph == "•"  # the quake keeps its cell


def test_higher_level_wins_where_alerts_overlap():
    cols, rows = 160, 40
    marks = tsunami_marks([KAM_WATCH, KAM_WARNING], lambda a: None)
    tint, _ = place_tsunami(marks, WORLD, cols, rows, False)
    warning = geometry_cells(KAM_WARNING.geometry, WORLD, cols, rows, False)
    watch_only = geometry_cells(KAM_WATCH.geometry, WORLD, cols, rows, False) - warning
    assert warning and all(tint[c].color.name == "red" for c in warning)
    assert watch_only and all(tint[c].color.name == "yellow" for c in watch_only)


def test_tsunami_attention_blinks_then_settles_when_acknowledged():
    clock = ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0)
    tracker = AttentionTracker(clock)
    tracker.observe([TsunamiIssued(KAM_WARNING)])
    m = MapModel(None, False, tracker)
    m.resize(160, 40)
    m.set_tsunami(tsunami_marks([KAM_WARNING], lambda a: kam_quake()))
    assert m.has_attention()

    def backgrounds():
        frame = m.frame()
        return {s.bgcolor.name if s.bgcolor else None for s in frame.tint.values()}

    seen = set()
    for _ in range(8):  # the pulse alternates over two seconds
        seen |= backgrounds()
        clock.advance(0.25)
    assert len(seen) == 2  # both pulse backgrounds are drawn
    tracker.ack_tsunami()
    tracker.prune()
    assert not m.has_attention() and backgrounds() == {None}
    assert {s.color.name for s in m.frame().tint.values()} == {"red"}


def test_row_segments_apply_tint_and_merge_runs():
    red = Style(color="red")
    segs = row_segments("abcdef", [], {1: red, 2: red})
    assert [(s.text, s.style) for s in segs] == [("a", COAST_STYLE), ("bc", red), ("def", COAST_STYLE)]


async def test_dashboard_feeds_active_alerts_to_the_map():
    app = kam_app()
    async with app.run_test(size=(160, 40)) as pilot:
        app.tracker.observe([TsunamiIssued(kam(1))])
        app.dashboard.refresh_view()
        await pilot.pause()
        panel = app.dashboard.query_one(MapPanel)
        assert [(m.level, m.source) for m in panel.model.tsunami] == [("WARNING", (52.512, 160.324))]
        text = "".join(panel.render_line(y).text for y in range(panel.size.height))
        assert "(" in text and ")" in text
        assert panel.model.has_attention()
        await pilot.press("a")  # acknowledging stops the blink but the alert stays drawn
        await pilot.pause()
        app.tracker.prune()  # the app tick does this
        assert len(panel.model.tsunami) == 1 and not panel.model.has_attention()


class TsunamiHarness(App):
    CSS = "MapPanel { width: 100%; height: 100%; }"

    def __init__(self, ascii_mode):
        super().__init__()
        self.tracker = AttentionTracker(ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0))
        self.tracker.observe([TsunamiIssued(KAM_WARNING)])
        self.panel = MapPanel(self.tracker, ASCII if ascii_mode else UNICODE, None, ticks=False)

    def compose(self):
        yield self.panel

    def on_mount(self):
        quake = kam_quake()
        self.panel.set_quakes([quake], quake)
        quake_for = {KAM_WARNING.id: quake}.get
        self.panel.set_tsunami(tsunami_marks([KAM_WARNING, ALEUTIAN_WARNING], lambda a: quake_for(a.id)))


def test_active_warning_snapshot_ascii_80x24(snap_compare):
    assert snap_compare(TsunamiHarness(True), terminal_size=(80, 24))


def test_active_warning_snapshot_unicode_160x40(snap_compare):
    assert snap_compare(TsunamiHarness(False), terminal_size=(160, 40))
