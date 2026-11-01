"""Map panel: marker placement, zoom and pan math, attention blinking, goldens at three sizes, performance, keys."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import load_json, make_quake
from test_ui import week_app
from textual.app import App

from earthwave.clock import ReplayClock
from earthwave.config import HomeConfig
from earthwave.notify.inapp import FLASH_NEW, AttentionTracker
from earthwave.sources.usgs import parse_collection
from earthwave.state.events import QuakeNew
from earthwave.ui.glyphs import ASCII, UNICODE
from earthwave.ui.widgets.braille import WORLD, Viewport, coastline_canvas, project_cell, warm_coastline
from earthwave.ui.widgets.map_panel import (
    ASCII_MARKERS,
    TICK_SECONDS,
    UNICODE_MARKERS,
    MapModel,
    MapPanel,
    ZoomMode,
    base_lines,
    land_rows,
    marker_band,
    place_markers,
    row_segments,
    warm_world,
    world_view,
    zoom_view,
)
from earthwave.ui.widgets.quake_list import QuakeList

GOLDEN = Path(__file__).parent / "golden" / "map"
WEEK = load_json("usgs/4.5_week.geojson")
QUAKES = parse_collection(WEEK)
BONIN = next(q for q in QUAKES if q.id == "us6000u4r9")  # M6.5, 27.80N 139.62E (the Oct 25 Bonin Islands event)
NEW_YORK = HomeConfig("New York", 40.7, -74.0)
SIZES = [(80, 24), (160, 40), (240, 60)]


def expected_world_cell(lon: float, lat: float, cols: int, rows: int) -> tuple[int, int]:
    """Independent restatement of the world projection: edges map to the first and last dot."""
    x = (lon + 180) / 360 * (cols * 2 - 1)
    y = (90 - lat) / 180 * (rows * 4 - 1)
    return round(x) // 2, round(y) // 4


def model(cols, rows, home=None, ascii_mode=False, tracker=None, selected=BONIN) -> MapModel:
    m = MapModel(home, ascii_mode, tracker)
    m.resize(cols, rows)
    m.set_quakes(QUAKES, selected)
    return m


def to_region(m: MapModel) -> None:
    """World -> wide -> region."""
    m.cycle_zoom()
    assert m.cycle_zoom() is ZoomMode.REGION


# --- marker placement -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cols", "rows"), SIZES)
def test_bonin_islands_marker_lands_on_the_right_cell(cols, rows):
    literal = {(80, 24): (70, 8), (160, 40): (141, 13), (240, 60): (212, 20)}
    assert list(place_markers([BONIN], None, None, WORLD, cols, rows, False)) == [literal[(cols, rows)]]
    markers = place_markers([BONIN], None, None, WORLD, cols, rows, False)
    assert list(markers) == [expected_world_cell(139.6184, 27.7953, cols, rows)]
    marker = markers[next(iter(markers))]
    assert marker.glyph == "◉" and marker.style.color.name == "dark_orange"  # M6.5 is the top band, orange


def test_ptwc_epicenter_is_within_a_cell_of_the_usgs_one():
    usgs = project_cell(BONIN.lon, BONIN.lat, WORLD, 160, 40)
    ptwc = project_cell(139.9, 28.0, WORLD, 160, 40)
    assert abs(usgs[0] - ptwc[0]) <= 1 and abs(usgs[1] - ptwc[1]) <= 1


def test_markers_share_the_projection_of_the_raster():
    # Tokyo Bay is on the 10m coastline: its marker cell, from the same projection function, holds raster dots.
    cols, rows = 160, 40
    cell = project_cell(139.77, 35.65, WORLD, cols, rows)
    assert cell == (141, 12)
    assert coastline_canvas(cols, rows).cell_bits(*cell) != 0


def test_marker_bands_and_glyph_sets():
    assert [marker_band(m) for m in (2.5, 4.4, 4.5, 5.4, 5.5, 6.4, 6.5, 9.0)] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert UNICODE_MARKERS == ("·", "•", "●", "◉") and len(set(ASCII_MARKERS)) == 4
    quakes = [make_quake(f"b{i}", mag, lat=10.0, lon=-170.0 + 40 * i) for i, mag in enumerate((3.0, 5.0, 6.0, 7.0))]
    unicode_glyphs = [
        m.glyph for m in sorted(place_markers(quakes, None, None, WORLD, 160, 40, False).values(), key=lambda m: m.col)
    ]
    ascii_glyphs = [
        m.glyph for m in sorted(place_markers(quakes, None, None, WORLD, 160, 40, True).values(), key=lambda m: m.col)
    ]
    assert unicode_glyphs == list(UNICODE_MARKERS) and ascii_glyphs == list(ASCII_MARKERS)


def test_shared_cell_priority_and_outside_viewport():
    big, small = make_quake("big", 6.0, lat=10.0, lon=20.0), make_quake("small", 3.0, lat=10.0, lon=20.0)
    cell = place_markers([small, big], None, None, WORLD, 80, 24, False)
    assert [m.glyph for m in cell.values()] == ["●"]  # magnitude wins
    cell = place_markers([small, big], "small", None, WORLD, 80, 24, False)
    assert [m.glyph for m in cell.values()] == ["·"]  # selection beats magnitude
    far = make_quake("far", 5.0, lat=-60.0, lon=100.0)
    assert place_markers([far], None, None, Viewport(0, 0, 90, 45), 80, 24, False) == {}


def test_home_marker_and_selected_highlight():
    markers = place_markers(QUAKES, BONIN.id, NEW_YORK, WORLD, 160, 40, False)
    home = markers[expected_world_cell(-74.0, 40.7, 160, 40)]
    assert home.glyph == "⌂"
    assert place_markers([], None, NEW_YORK, WORLD, 80, 24, True)[expected_world_cell(-74.0, 40.7, 80, 24)].glyph == "H"
    assert place_markers([], None, HomeConfig(), WORLD, 80, 24, False) == {}  # unset home draws nothing
    selected = markers[expected_world_cell(BONIN.lon, BONIN.lat, 160, 40)]
    assert selected.style.reverse and selected.glyph == "◉"


def test_attention_markers_blink_with_the_injected_clock():
    clock = ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0)
    tracker = AttentionTracker(clock)
    quake = make_quake("new1", 5.0, lat=0.0, lon=0.0)
    other = make_quake("old1", 5.0, lat=0.0, lon=100.0)
    tracker.observe([QuakeNew(quake)])

    def style_of(q):
        return next(
            m
            for m in place_markers([quake, other], None, None, WORLD, 80, 24, False, tracker).values()
            if m.glyph and (m.col, m.row) == project_cell(q.lon, q.lat, WORLD, 80, 24)
        ).style

    assert style_of(quake).bgcolor.name == FLASH_NEW.bg  # flash on
    clock.advance(0.5)
    assert style_of(quake).bgcolor is None and style_of(quake).bold  # between flashes: plain but emphasised
    clock.advance(0.5)
    assert style_of(quake).bgcolor.name == FLASH_NEW.bg
    assert style_of(other).bgcolor is None  # a quake without attention never blinks


def test_row_segments_split_around_markers():
    markers = place_markers([make_quake("a", 5.0, lat=0, lon=0)], None, None, WORLD, 10, 4, False)
    marker = next(iter(markers.values()))
    segs = row_segments("x" * 10, [marker])
    assert "".join(s.text for s in segs) == "x" * marker.col + marker.glyph + "x" * (9 - marker.col)
    assert row_segments("abc", [])[0].text == "abc"

    from rich.style import Style

    land, coast = Style(color="bright_black"), Style(color="cyan")
    segs = row_segments("abcde", [], None, bytes([0, 1, 1, 0, 0]), coast, land)
    assert [(s.text, s.style) for s in segs] == [("a", coast), ("bc", land), ("de", coast)]


def test_marker_stays_on_top_of_land_dots_and_tint_skips_land_cells():
    from rich.style import Style

    from earthwave.ui.widgets.map_panel import Marker

    land, coast, tint = Style(color="bright_black"), Style(color="cyan"), Style(color="red")
    marker = Marker(1, 0, "●", Style(color="green"), (False, False, 0, 5.0))
    segs = row_segments("abc", [marker], {1: tint, 2: tint}, bytes([1, 1, 1]), coast, land)
    assert [(s.text, s.style) for s in segs] == [("a", land), ("●", marker.style), ("c", land)]
    assert row_segments("ab", [], {0: tint}, bytes([0, 0]), coast, land)[0].style == tint


def test_land_rows_flag_land_dot_cells_and_are_cached():
    view = zoom_view(138, 37, 40, 198, 45)
    rows_ = land_rows(198, 45, view)
    assert len(rows_) == 45 and all(len(r) == 198 for r in rows_)
    assert land_rows(198, 45, view) is rows_
    assert base_lines(198, 45, view, False) is base_lines(198, 45, view, False)
    assert 0 < sum(map(sum, rows_)) < 198 * 45
    glyphs = base_lines(198, 45, view, False)
    assert all(glyphs[r][c] != "⠀" for r, row in enumerate(rows_) for c, f in enumerate(row) if f)


def test_idle_redraw_reuses_the_cached_raster():
    model = MapModel()
    model.resize(120, 30)
    first = model.frame()
    second = model.frame()
    assert second.base is first.base and second.land is first.land


def test_ascii_mode_draws_the_coast_only():
    model = MapModel(ascii_mode=True)
    model.resize(120, 30)
    assert model.frame().land == ()


# --- zoom and pan -----------------------------------------------------------------------------------------------


def test_zoom_view_keeps_equal_degrees_per_dot_and_whole_degrees():
    view = zoom_view(139.6, 27.8, 40, 160, 40)
    assert view == Viewport(100.0, 8.0, 180.0, 48.0)  # aspect 320/160 = 2: 80 degrees of longitude for 40 of latitude
    wide = zoom_view(0, 0, 40, 80, 24)
    assert (wide.lon1 - wide.lon0) / (wide.lat1 - wide.lat0) == pytest.approx(160 / 96, abs=0.03)
    assert all(v == int(v) for v in view)


def test_zoom_view_clamps_poles_and_spans_the_dateline():
    south = zoom_view(0, -89, 40, 160, 40)
    assert south.lat0 == -90.0 and south.lat1 == -50.0
    north = zoom_view(0, 89, 40, 160, 40)
    assert north.lat1 == 90.0
    east = zoom_view(179, 0, 40, 160, 40)
    assert east.lon0 < 180 < east.lon1  # lon1 runs past 180
    cell = project_cell(-179.0, 0.0, east, 160, 40)  # a quake just across the dateline is still on screen
    assert cell is not None and cell[0] > 80


def test_region_zoom_centres_the_selected_quake():
    m = model(160, 40)
    assert m.mode is ZoomMode.WORLD and m.view() == world_view(0, 160, 40)
    assert m.cycle_zoom() is ZoomMode.WIDE
    assert m.cycle_zoom() is ZoomMode.REGION
    col, row = project_cell(BONIN.lon, BONIN.lat, m.view(), 160, 40)
    assert abs(col - 80) <= 1 and abs(row - 20) <= 1
    assert m.frame().markers[(col, row)].glyph == "◉"


def test_zoom_cycle_steps_and_spans():
    Z = ZoomMode
    m = model(160, 40, home=NEW_YORK)
    assert [m.cycle_zoom() for _ in range(5)] == [Z.WIDE, Z.REGION, Z.CLOSE, Z.WORLD, Z.WIDE]
    spans = {}
    for _ in range(3):
        view = m.view()
        spans[m.mode] = view.lat1 - view.lat0
        assert m.shown.startswith(m.mode.value) and "°" in m.shown
        m.cycle_zoom()
    assert spans == {Z.WIDE: 90, Z.REGION: 40, Z.CLOSE: 16}
    assert m.shown == "world"
    ascii_model = model(160, 40, ascii_mode=True)
    ascii_model.cycle_zoom()
    assert ascii_model.shown == "wide 90 deg"


def test_zoom_without_a_selection_centres_on_pan_point_then_home_then_latest_major():
    home = model(160, 40, home=NEW_YORK, selected=None)
    assert home.cycle_zoom() is ZoomMode.WIDE
    assert home.view().lon0 < -74 < home.view().lon1 and home.view().lat0 < 40.7 < home.view().lat1
    home.cycle_zoom()
    home.cycle_zoom()
    assert home.view().lat1 - home.view().lat0 == 16 and home.view().lon0 < -74 < home.view().lon1

    major = max((q for q in QUAKES if q.mag >= 6.0), key=lambda q: q.time)
    no_home = model(160, 40, selected=None)
    assert no_home.cycle_zoom() is ZoomMode.WIDE
    assert project_cell(major.lon, major.lat, no_home.view(), 160, 40) is not None
    view = no_home.view()
    assert abs(((view.lon0 + view.lon1) / 2 - major.lon + 180) % 360 - 180) < 3

    panned = model(160, 40, home=NEW_YORK, selected=None)
    panned.pan(1, 0)  # the world scrolled 30 degrees east: that is the point to zoom on
    panned.cycle_zoom()
    assert abs((panned.view().lon0 + panned.view().lon1) / 2 - 30) <= 5
    panned.pan(1, 0)  # panning in a zoomed view moves the point the next step centres on
    centre = (panned.view().lon0 + panned.view().lon1) / 2
    panned.cycle_zoom()
    assert abs((panned.view().lon0 + panned.view().lon1) / 2 - centre) <= 5

    nothing = MapModel()
    nothing.resize(160, 40)
    assert nothing.cycle_zoom() is ZoomMode.WIDE and nothing.shown == "world"  # no anchor anywhere: draws the world


def test_selecting_a_quake_recentres_a_zoom_made_without_one():
    m = model(160, 40, home=NEW_YORK, selected=None)
    m.cycle_zoom()
    m.select(BONIN)
    col, row = project_cell(BONIN.lon, BONIN.lat, m.view(), 160, 40)
    assert abs(col - 80) <= 1 and abs(row - 20) <= 1


def test_world_view_has_equal_degrees_per_dot():
    for cols, rows in ((200, 50), (280, 70), (88, 41), (160, 40), (60, 10)):
        view = world_view(0, cols, rows)
        per_x = (view.lon1 - view.lon0) / (cols * 2 - 1)
        per_y = (view.lat1 - view.lat0) / (rows * 4 - 1)
        assert abs(per_x - per_y) < 1e-9, (cols, rows)
        assert view.lon1 - view.lon0 == 360
        assert (view.lat1 - view.lat0 >= 180 and view.lat0 < -90) or view.lat1 <= 90
    assert world_view(0, 88, 41).lat1 > 90  # a tall panel is letterboxed
    assert world_view(0, 160, 20).lat0 > -90  # a short one drops the south polar cap


def test_pan_math():
    m = model(160, 40)
    m.pan(1, 1)
    assert m.view().lon0 == -180 + 30 and m.view().lon1 - m.view().lon0 == 360  # world scrolls sideways only
    m.pan(-1, 0)
    assert m.view() == world_view(0, 160, 40)
    to_region(m)
    m.pan(1, 0)  # the first pan snaps the centre to the 5 degree grid
    before = m.view()
    m.pan(1, 0)
    after = m.view()
    assert after.lon0 - before.lon0 == 20 and after.lat0 == before.lat0  # a quarter of the 80 degree span
    m.pan(0, -1)
    assert m.view().lat0 == before.lat0 - 10
    m.select(make_quake("other", 5.0))
    assert m.pan_lon == m.pan_lat == 0  # a new selection recentres
    m.cycle_zoom()
    assert (m.pan_lon, m.pan_lat) == (0, 0)


def test_pan_stops_at_the_pole_and_reverses_at_once():
    m = model(160, 40)
    m.cycle_zoom()
    for _ in range(30):  # hold Up
        m.pan(0, 1)
    top = m.view()
    assert top.lat1 == 90.0
    pinned = m.pan_lat
    m.pan(0, 1)
    assert m.pan_lat == pinned  # the offset does not keep growing
    m.pan(0, -1)
    assert m.view().lat1 < top.lat1  # one Down press moves immediately
    for _ in range(30):
        m.pan(0, -1)
    assert m.view().lat0 == -90.0
    m.pan(0, 1)
    assert m.view().lat0 > -90.0


def test_pan_longitude_wraps_around_the_globe():
    m = model(160, 40)
    for _ in range(40):  # far more than one trip around the world
        m.pan(1, 0)
    assert -180 <= m.pan_lon < 180
    before = m.view()
    m.pan(-1, 0)
    assert m.view().lon0 != before.lon0  # panning back is never dead
    m.cycle_zoom()
    for _ in range(100):
        m.pan(-1, 0)
    assert -180 <= m.pan_lon < 180


def test_data_refresh_keeps_the_pan_but_user_selection_recentres():
    m = model(160, 40)
    to_region(m)
    m.pan(1, 1)
    panned = (m.pan_lon, m.pan_lat)
    other = QUAKES[0] if QUAKES[0].id != BONIN.id else QUAKES[1]
    m.set_quakes(QUAKES, other)  # a refresh moved a different quake under the cursor
    assert (m.pan_lon, m.pan_lat) == panned and m.mode is ZoomMode.REGION and m.selected is other
    m.select(BONIN)  # the user moved the cursor
    assert (m.pan_lon, m.pan_lat) == (0, 0)


async def test_dashboard_refresh_does_not_reset_the_map_pan():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        panel = app.dashboard.query_one(MapPanel)
        await pilot.press("z")
        panel.focus()
        await pilot.press("right")
        panned = (panel.model.pan_lon, panel.model.pan_lat)
        app.dashboard.refresh_view()
        assert (panel.model.pan_lon, panel.model.pan_lat) == panned
        app.dashboard.query_one(QuakeList).focus()
        await pilot.press("j")
        assert (panel.model.pan_lon, panel.model.pan_lat) == (0, 0)


def test_world_view_wraps_and_pan_views_are_cache_friendly():
    assert (
        world_view(360) == WORLD
        and world_view(-30).lon0 == WORLD.lon0 - 30 + 360
        and world_view(30).lon0 == WORLD.lon0 + 30
    )
    m = model(160, 40)
    to_region(m)
    base_lines.cache_clear()
    for _ in range(3):
        m.pan(1, 0)
        m.frame()
        m.pan(-1, 0)
        m.frame()
    assert base_lines.cache_info().misses == 2  # revisiting a panned view never re-rasterises


# --- goldens ----------------------------------------------------------------------------------------------------


def check_golden(name: str, lines: list[str]) -> None:
    path = GOLDEN / name
    text = "\n".join(lines) + "\n"
    if os.environ.get("EARTHWAVE_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize(("cols", "rows"), SIZES)
@pytest.mark.parametrize("ascii_mode", [False, True], ids=["unicode", "ascii"])
def test_world_golden(cols, rows, ascii_mode):
    m = model(cols, rows, home=NEW_YORK, ascii_mode=ascii_mode)
    check_golden(f"world_{cols}x{rows}_{'ascii' if ascii_mode else 'unicode'}.txt", m.plain_rows())


@pytest.mark.parametrize("ascii_mode", [False, True], ids=["unicode", "ascii"])
def test_region_golden(ascii_mode):
    m = model(160, 40, home=NEW_YORK, ascii_mode=ascii_mode)
    to_region(m)
    check_golden(f"region_160x40_{'ascii' if ascii_mode else 'unicode'}.txt", m.plain_rows())


# --- performance ------------------------------------------------------------------------------------------------


def test_performance_budget_at_240x60():
    m = model(240, 60, home=NEW_YORK)
    warm_coastline()  # loading the data is done once, off the UI thread, by the panel's pre-warm
    base_lines.cache_clear()
    start = time.perf_counter()
    m.frame()
    cold = time.perf_counter() - start
    assert cold < 1.0  # rasterising the full-resolution coastline for a new viewport blocks the UI for this long
    start = time.perf_counter()
    for _ in range(20):
        m.frame()
    tick = (time.perf_counter() - start) / 20
    assert tick < 0.05  # per-tick cost with the raster cached: markers only


# --- widget -----------------------------------------------------------------------------------------------------


class Harness(App):
    CSS = "MapPanel { width: 100%; height: 100%; }"

    def __init__(self, ascii_mode=False, home=NEW_YORK):
        super().__init__()
        self.tracker = AttentionTracker(ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0))
        self.panel = MapPanel(self.tracker, ASCII if ascii_mode else UNICODE, home, ticks=False)

    def compose(self):
        yield self.panel

    def on_mount(self):
        self.panel.set_quakes(QUAKES, BONIN)


def text_of(panel: MapPanel) -> list[str]:
    return [panel.render_line(y).text for y in range(panel.size.height)]


async def test_widget_renders_the_model_and_cycles_zoom_with_keys():
    app = Harness()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        panel = app.panel
        assert text_of(panel) == panel.model.plain_rows() and "◉" in "".join(text_of(panel))
        assert panel.border_title == "Map · world"
        panel.action_cycle_zoom()
        await pilot.pause()
        assert panel.border_title == "Map · wide 90°" and panel.model.mode is ZoomMode.WIDE
        panel.action_cycle_zoom()
        await pilot.pause()
        assert panel.border_title == "Map · region 40°" and panel.model.mode is ZoomMode.REGION
        before = panel.model.view()
        panel.focus()
        await pilot.press("right", "up")
        assert panel.model.view().lon0 > before.lon0 and panel.model.view().lat0 > before.lat0
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert (panel.model.cols, panel.model.rows) == (80, 24) and len(text_of(panel)) == 24


async def test_widget_ascii_glyphs_use_plain_characters():
    app = Harness(ascii_mode=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        text = "".join(text_of(app.panel))
        assert all(ord(ch) < 128 for ch in text) and "@" in text and "H" in text
        assert app.panel.border_title == "Map - world"


def test_map_snapshot_ascii_80x24(snap_compare):
    assert snap_compare(Harness(ascii_mode=True), terminal_size=(80, 24))


def test_map_snapshot_unicode_160x40(snap_compare):
    assert snap_compare(Harness(), terminal_size=(160, 40))


def test_map_snapshot_unicode_240x60(snap_compare):
    assert snap_compare(Harness(), terminal_size=(240, 60))


async def test_dashboard_keys_zoom_fullscreen_and_selection_follow():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        panel = app.dashboard.query_one(MapPanel)
        quake_list = app.dashboard.query_one(QuakeList)
        assert panel.model.selected.id == quake_list.selected.id
        await pilot.press("j")
        assert panel.model.selected.id == quake_list.selected.id  # moving the cursor moves the highlight
        await pilot.press("z")
        assert panel.model.mode is ZoomMode.WIDE and "wide 90" in panel.border_title
        await pilot.press("z")
        assert panel.model.mode is ZoomMode.REGION and "region 40" in panel.border_title
        await pilot.press("z")
        assert panel.model.mode is ZoomMode.CLOSE and "close 16" in panel.border_title
        await pilot.press("z")
        assert panel.model.mode is ZoomMode.WORLD
        await pilot.press("M")
        await pilot.pause()
        assert app.dashboard.has_class("map-full") and app.focused is panel
        assert panel.size.width > 150
        await pilot.press("right")
        assert panel.model.pan_lon == 30 and panel.model.view().lon0 == -180 + 30  # one step, not a jump
        await pilot.press("M")
        await pilot.pause()
        assert not app.dashboard.has_class("map-full") and app.focused is quake_list
        await pilot.press("M", "m")
        await pilot.pause()
        assert not app.dashboard.has_class("map-full") and app.dashboard.has_class("no-map")


# --- blink and pre-warm -------------------------------------------------------------------------------------------


def test_blink_tick_is_finer_than_the_phase_flips():
    assert TICK_SECONDS <= 0.25


def test_both_blink_phases_render_with_uneven_clock_steps():
    clock = ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0)
    tracker = AttentionTracker(clock)
    quake = make_quake("new1", 5.0, lat=0.0, lon=0.0)
    m = MapModel(None, False, tracker)
    m.resize(80, 24)
    m.set_quakes([quake], None)
    assert not m.has_attention()
    tracker.observe([QuakeNew(quake)])
    assert m.has_attention()
    seen = set()
    elapsed = 0.0
    # Ticks arrive at 0.25 s plus jitter; within any 1 s window both the flash and the plain phase must be sampled.
    for jitter in (0.0, 0.05, -0.04, 0.1, -0.08, 0.02, 0.0, 0.07, -0.06, 0.03, 0.0, 0.09):
        clock.advance(TICK_SECONDS + jitter)
        elapsed += TICK_SECONDS + jitter
        if elapsed < 3.0:
            marker = next(iter(m.frame().markers.values()))
            seen.add(marker.style.bgcolor is not None)
    assert seen == {True, False}


def test_unattended_markers_do_not_force_refreshes():
    tracker = AttentionTracker(ReplayClock(datetime(2026, 10, 31, 12, tzinfo=UTC), real=lambda: 0.0))
    m = MapModel(None, False, tracker)
    m.set_quakes([make_quake("q", 5.0)], None)
    assert not m.has_attention()


async def test_panel_prewarms_the_world_raster_on_resize(monkeypatch):
    calls = []
    monkeypatch.setattr(MapPanel, "run_worker", lambda self, work, **kwargs: calls.append((work, kwargs)))
    app = Harness()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(100, 28)
        await pilot.pause()
    work, kwargs = calls[-1]
    assert work.func is warm_world and work.args == (100, 28, False) and kwargs["thread"] is True


def test_warm_world_fills_the_cache():
    base_lines.cache_clear()
    warm_world(80, 24, True)
    assert base_lines.cache_info().currsize == 1
    warm_world(0, 0, False)  # a not-yet-sized panel is ignored
    assert base_lines.cache_info().currsize == 1
