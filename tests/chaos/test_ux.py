"""Phase 1 chaos audit, UX / visual / focus probes. Read-only on product code.

A probe that exposes a defect is `xfail(strict=True)` with the finding id (see FINDINGS_UX.md), so the suite stays
green, `pytest --runxfail tests/chaos/test_ux.py` shows each finding failing, and a fix turns the probe into an XPASS
failure that says to delete the marker. SVGs for eyeballing go to tests/chaos/out/ (git-ignored), never to
tests/__snapshots__.
"""

import html
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import T0, make_quake
from textual.color import Color
from textual.widgets import Input

from earthwave.config import Config, with_overrides
from earthwave.models import Batch
from earthwave.poll import Freshness
from earthwave.state.store import SortKey, Store, ViewFilter
from earthwave.ui.glyphs import ASCII, UNICODE
from earthwave.ui.screens.detail import DetailScreen, render_detail
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.theme import LEVEL_COLORS, PAGER_COLORS, contrast, mag_color
from earthwave.ui.widgets.quake_list import CURSOR_BG, QuakeList, header_text
from earthwave.ui.widgets.status_bar import SourceLine, StatusInfo, build_status
from earthwave.ui.widgets.too_small import MIN_HEIGHT, MIN_WIDTH

OUT = Path(__file__).parent / "out"
SIZES = [(80, 24), (120, 40), (200, 60)]
_CELL_W, _CELL_H = 12.2, 24.4
_TEXT = re.compile(r'<text class="([^"]*)" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>')


def screen_rows(app) -> list[str]:
    """The screen as plain text rows (read back from the SVG export, as tests/uat/harness.py does)."""
    cells: dict[int, dict[int, str]] = {}
    for cls, x, y, body in _TEXT.findall(app.export_screenshot()):
        if cls.endswith("-title"):
            continue
        row, col = round((float(y) - 20) / _CELL_H), round(float(x) / _CELL_W)
        for offset, ch in enumerate(html.unescape(body).replace("\xa0", " ")):
            cells.setdefault(row, {})[col + offset] = ch
    return [
        "".join(cells.get(r, {}).get(c, " ") for c in range(max(cells.get(r, {}), default=-1) + 1)).rstrip()
        for r in range(max(cells, default=-1) + 1)
    ]


def save_svg(app, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"{name}.svg").write_text(app.export_screenshot(), encoding="utf-8")


def week_app(**kwargs):
    from test_ui import week_app as make

    return make(**kwargs)


def put(app, *quakes) -> None:
    """Merge quakes into the app's store as a live (non-backfill) poll and redraw the dashboard."""
    app.store.merge(Batch("usgs", app.clock.now(), quakes=tuple(quakes)), app.clock)
    app.dashboard.refresh_view()


def overflowing_widgets(screen) -> list[str]:
    """Visible widgets whose region leaves the screen."""
    bounds = screen.region
    bad = []
    for w in screen.query("*"):
        if not w.display or w.region.area == 0:
            continue
        if w.region not in bounds and w.region.width and w.region.height:
            bad.append(f"{w.__class__.__name__}#{w.id} {w.region} not in {bounds}")
    return bad


# --- (1) layout at 80x24, 120x40, 200x60 --------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_dashboard_layout(size):
    w, h = size
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        save_svg(app, f"dashboard_{w}x{h}")
        rows = screen_rows(app)
        text = "\n".join(rows)
        assert len(rows) <= h and all(len(r) <= w for r in rows), "text outside the screen"
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            assert "AGE" not in text, "the dashboard must be replaced, not overdrawn, below 100x30"
            assert "enlarge" in text.lower() or "too small" in text.lower() or "resize" in text.lower(), text
            return
        assert "AGE" in text and "PLACE" in text and "shown of" in text
        assert not overflowing_widgets(app.screen), overflowing_widgets(app.screen)
        quakes = app.dashboard.query_one(QuakeList)
        # every row strip is exactly the content width: no wrapped/overflowing columns
        for y in range(min(quakes.size.height, len(quakes.rows))):
            strip = quakes.render_line(y)
            assert strip.cell_length == quakes.scrollable_content_region.width, (y, strip.text)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_status_bar_fits_one_line(size):
    w, h = size
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        bar = app.dashboard.query_one("#status")
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            return
        assert bar.region.height == 1 and bar.region.width == w
        assert "shown of" in screen_rows(app)[bar.region.y]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_detail_screen_layout(size):
    w, h = size
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        quake = app.dashboard.query_one(QuakeList).selected
        quake = replace(
            quake,
            tsunami_flag=True,
            pager_alert="orange",
            felt=12,
            cdi=3.4,
            mmi=5.1,
            updated=quake.updated + timedelta(1),
        )
        put(app, quake)
        app.open_detail(quake)
        await pilot.pause(0.1)
        save_svg(app, f"detail_{w}x{h}")
        rows = screen_rows(app)
        assert all(len(r) <= w for r in rows)
        text = "\n".join(rows)
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            assert "Magnitude" not in text, "detail must show the too-small overlay below 100x30"
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DetailScreen), "Escape must leave detail even under the overlay"
            return
        assert "Magnitude" in text and "PAGER alert" in text and "Tsunami" in text
        assert not overflowing_widgets(app.screen)


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
async def test_tsunami_screen_layout(size):
    from uat.harness import kam_alert_app

    w, h = size
    app = kam_alert_app(0, 1)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        await pilot.press("t")
        await pilot.pause(0.2)
        assert isinstance(app.screen, TsunamiScreen)
        save_svg(app, f"tsunami_{w}x{h}")
        rows = screen_rows(app)
        assert all(len(r) <= w for r in rows)
        text = "\n".join(rows)
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            assert "LEVEL" not in text
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, TsunamiScreen)
            return
        assert "LEVEL" in text and "WARNING" in text
        assert not overflowing_widgets(app.screen)


async def test_dashboard_has_a_tsunami_banner_at_every_size_when_an_alert_is_in_force():
    """Even the 80x24 overlay must say an alert is in force (tests/uat covers 80x24 for the overlay text only)."""
    from uat.harness import kam_alert_app

    for size in SIZES:
        app = kam_alert_app(1)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            assert "TSUNAMI" in "\n".join(screen_rows(app)), size


# --- (1b) hostile row content: the list column math counts characters, the terminal draws cells -------------------


def row_strip(app, quake, width=None, selected=False):
    quakes = app.dashboard.query_one(QuakeList)
    return quakes._row_strip(quake, selected, width or quakes.scrollable_content_region.width)


@pytest.mark.parametrize(
    "place",
    [
        pytest.param("東京湾 近海 の 地震 が 発生 しました 東京湾 近海 の 地震 が 発生 しました", id="cjk-long"),
        pytest.param("🌋 Mount Etna 🌋 Sicily, Italy and a lot more place text to overflow the column xx", id="emoji"),
        pytest.param("x\ty", id="tab"),
        pytest.param("a\nb", id="newline"),
    ],
)
@pytest.mark.xfail(strict=True, reason="UX-1: place column is sized in characters, not terminal cells")
async def test_row_width_is_cells_not_characters(place):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        quake = replace(base, place=place, pager_alert="red", tsunami_flag=True)
        strip = row_strip(app, quake)
        text = strip.text
        assert "\n" not in text and "\t" not in text, repr(text)
        assert strip.cell_length == width, (strip.cell_length, width)  # else the FL/PAGER flags are pushed off
        assert text.rstrip().endswith("TR"), text  # the tsunami and PAGER flags stay at the right edge


async def test_wide_place_effect_is_a_cropped_row_that_loses_the_flags():
    """Evidence for UX-1: render_line crops the over-wide row, so the tsunami and PAGER flags are what disappears."""
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        wide = replace(quakes.rows[0], place="東京湾" * 30, pager_alert="red", tsunami_flag=True)
        quakes.set_rows([wide, *quakes.rows[1:]])
        line = quakes.render_line(0)
        assert line.cell_length == quakes.scrollable_content_region.width
        assert not line.text.rstrip().endswith("TR"), "if this fails UX-1 is fixed: drop its xfail marker"


def test_header_matches_row_columns_when_home_off_and_narrow():
    for width in (0, 1, 10, 30, 31, 32, 33, 60):
        for home in (True, False):
            assert header_text(width, home).cell_len == width, (width, home)


@pytest.mark.parametrize("width", [0, 1, 5, 20, 40])
async def test_narrow_row_widths_never_raise_or_overflow(width):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        strip = quakes._row_strip(quakes.rows[0], True, width)
        assert strip.cell_length >= 0
        if width >= 40:
            assert strip.cell_length == width, (width, strip.text)


@pytest.mark.xfail(strict=True, reason="UX-2: a two-digit-or-negative magnitude overflows the 4-wide MAG column")
async def test_extreme_magnitude_keeps_columns_aligned():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        for mag in (-10.1, 10.0, 100.0):
            assert row_strip(app, replace(base, mag=mag)).cell_length == width, mag


@pytest.mark.parametrize("depth", [-700.0, 12345.6])
async def test_extreme_depth_keeps_columns_aligned(depth):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        assert row_strip(app, replace(base, depth_km=depth)).cell_length == width, depth


async def test_future_dated_quake_age_stays_in_its_column():
    """Clock skew / a revised time in the future: fmt_age clamps to 0s, so the row must not show a negative age."""
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        future = replace(base, time=app.clock.now() + timedelta(days=400))
        strip = row_strip(app, future)
        assert strip.cell_length == app.dashboard.query_one(QuakeList).scrollable_content_region.width
        assert strip.text[1:5].strip() == "0s"


async def test_empty_place_reads_unknown_not_blank():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        assert "unknown" in row_strip(app, replace(base, place="")).text


# --- (1c) status bar at the 100-column floor -------------------------------------------------------------------


def worst_status() -> StatusInfo:
    names = ["USGS", "tsunami.gov", "NWS", "JMA", "NDBC", "CO-OPS"]
    return StatusInfo(
        sources=tuple(
            SourceLine(n, Freshness.STALE if i % 2 else Freshness.ERROR, 3600.0 * 5) for i, n in enumerate(names)
        ),
        shown=9999,
        total=99999,
        next_poll=59,
        filter_text="M>=2.5, last 168h, search 'vanuatu'",
        unseen=123,
        sort="dist",
        alert_state="alarm",
        alert_level="WARNING",
    )


@pytest.mark.xfail(
    strict=True, reason="UX-3: with an alert and a search active the status bar is cut before feed health"
)
def test_status_bar_keeps_feed_health_visible_at_100_columns():
    """The bar is one no_wrap line with an ellipsis, so whatever is past column 100 is invisible."""
    visible = build_status(worst_status(), UNICODE).plain[:99]
    assert "stale" in visible or "failed" in visible, visible


# --- (2) visual encoding ---------------------------------------------------------------------------------------

_MACHADO = {  # Machado et al. 2009, severity 1.0, applied in linear RGB
    "protanopia": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
    "deuteranopia": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
    "tritanopia": [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]],
}


def _truecolor(app, style: str) -> tuple[int, int, int]:
    """What the style renders as: Textual maps the sixteen ANSI colours through its own terminal theme."""
    from rich.style import Style

    c = Style.parse(style).color.get_truecolor(app.ansi_theme)
    return c.red, c.green, c.blue


def _lin(rgb):
    return [((v / 255) / 12.92 if v / 255 <= 0.04045 else (((v / 255) + 0.055) / 1.055) ** 2.4) for v in rgb]


def _unlin(v):
    v = min(max(v, 0.0), 1.0)
    return 255 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)


def _lab(rgb):
    r, g, b = _lin(rgb)
    x, y, z = (
        (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
        0.2126 * r + 0.7152 * g + 0.0722 * b,
        (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883,
    )

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _simulate(rgb, kind):
    if kind is None:
        return rgb
    lin = _lin(rgb)
    return tuple(_unlin(sum(m * c for m, c in zip(row, lin, strict=True))) for row in _MACHADO[kind])


def min_delta_e(colours: dict[str, tuple[int, int, int]], kind) -> tuple[float, str]:
    names = list(colours)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            la, lb = _lab(_simulate(colours[a], kind)), _lab(_simulate(colours[b], kind))
            pairs.append((sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5, f"{a} vs {b}"))
    return min(pairs)


MAG_SAMPLES = {"<3": 2.0, "3-4.4": 4.0, "4.5-5.4": 5.0, "5.5-6.4": 6.0, "6.5-6.9": 6.7, "7+": 7.5}


async def _palettes():
    app = week_app()
    async with app.run_test(size=(120, 40)):
        mags = {k: _truecolor(app, mag_color(v)) for k, v in MAG_SAMPLES.items()}
        pagers = {k: _truecolor(app, v) for k, v in PAGER_COLORS.items()}
        levels = {k: _truecolor(app, v) for k, v in LEVEL_COLORS.items() if k != "CANCELLATION"}
    return mags, pagers, levels


async def test_palettes_are_distinguishable_with_normal_vision():
    mags, pagers, _ = await _palettes()
    for name, pal in (("mag", mags), ("pager", pagers)):
        delta, pair = min_delta_e(pal, None)
        assert delta >= 25, (name, pair, delta)


@pytest.mark.parametrize("kind", [None, "protanopia", "deuteranopia", "tritanopia"], ids=str)
async def test_cvd_report(kind):
    """Not an assertion: writes the CVD table to tests/chaos/out/cvd.txt for the findings file."""
    mags, pagers, levels = await _palettes()
    OUT.mkdir(exist_ok=True)
    with (OUT / "cvd.txt").open("w" if kind is None else "a", encoding="utf-8") as f:
        for name, pal in (("magnitude", mags), ("PAGER", pagers), ("tsunami level", levels)):
            delta, pair = min_delta_e(pal, kind)
            f.write(f"{kind or 'normal':13} {name:14} worst pair {pair:28} dE={delta:5.1f}\n")


@pytest.mark.xfail(strict=True, reason="UX-4: green and yellow are one colour to a protanope; letters/digits rescue it")
@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
async def test_green_and_yellow_survive_red_green_colour_blindness(kind):
    mags, pagers, _ = await _palettes()
    assert min_delta_e(pagers, kind)[0] >= 20, min_delta_e(pagers, kind)
    assert min_delta_e(mags, kind)[0] >= 20, min_delta_e(mags, kind)


@pytest.mark.xfail(strict=True, reason="UX-4: THREAT and INFORMATION share one colour (the level word is the only cue)")
async def test_threat_and_information_have_distinct_colours():
    assert LEVEL_COLORS["THREAT"] != LEVEL_COLORS["INFORMATION"]


async def test_every_colour_coded_field_has_a_non_colour_cue_in_the_row():
    """PAGER is a letter (G/Y/O/R), magnitude is the number, tsunami is a T: strip every colour and all remain."""
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        letters = set()
        for alert in ("green", "yellow", "orange", "red"):
            text = row_strip(app, replace(base, pager_alert=alert, tsunami_flag=True)).text
            letters.add(text.rstrip()[-1])
            assert text.rstrip()[-2] == "T"
        assert letters == {"G", "Y", "O", "R"}


@pytest.mark.parametrize("alert", [None, "", "purple", "Green", "RED", "unknown", " "], ids=repr)
@pytest.mark.parametrize("flag", [False, True])
async def test_pager_alert_unknown_or_null_renders_blank_not_wrong(alert, flag):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        width = app.dashboard.query_one(QuakeList).scrollable_content_region.width
        strip = row_strip(app, replace(base, pager_alert=alert, tsunami_flag=flag))
        assert strip.cell_length == width
        assert strip.text[-2:] == ("T " if flag else "  "), repr(strip.text[-4:])
        detail = render_detail(replace(base, pager_alert=alert), None, "", None, "UTC", T0)
        assert ("PAGER alert" in detail.plain) == bool(alert), detail.plain


@pytest.mark.xfail(strict=True, reason="UX-5: a PAGER level spelled with capitals is dropped from the list column")
async def test_pager_alert_matching_is_case_insensitive():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.dashboard.query_one(QuakeList).rows[0]
        assert row_strip(app, replace(base, pager_alert="Red")).text.rstrip().endswith("R")


def test_tsunami_flag_and_pager_in_detail_are_words_not_colour():
    base = make_quake(tsunami_flag=True, pager_alert="red")
    text = render_detail(base, None, "", None, "UTC", T0).plain
    assert "USGS links a tsunami product" in text and "PAGER alert" in text and "red" in text


@pytest.mark.parametrize("glyphs", [UNICODE, ASCII], ids=["unicode", "ascii"])
def test_tsunami_flag_is_a_letter_in_both_glyph_sets(glyphs):
    assert glyphs.tsunami.isalpha()


@pytest.mark.xfail(
    strict=True, reason="UX-6: red (M7+, PAGER red) is 2.75:1 on the cursor row, the least readable in the list"
)
async def test_selected_row_foregrounds_stay_readable_on_the_cursor_background():
    """CURSOR_BG is a fixed #1e3a5f; every foreground used in a row must keep 3:1 on it (large-text floor)."""
    app = week_app()
    async with app.run_test(size=(120, 40)):
        bg = Color.parse(CURSOR_BG)
        samples = {f"mag {m}": mag_color(m) for m in MAG_SAMPLES.values()}
        samples |= {f"pager {k}": v for k, v in PAGER_COLORS.items()}
        samples |= {"grey62": "grey62", "tsunami flag": "bold blue", "marker": "cyan"}
        worst = {name: round(contrast(Color(*_truecolor(app, style)), bg), 2) for name, style in samples.items()}
        low = {k: v for k, v in worst.items() if v < 3.0}
        assert not low, low


async def test_no_color_rows_keep_every_cue(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert app.no_color
        base = app.dashboard.query_one(QuakeList).rows[0]
        put(app, replace(base, id="nc1", pager_alert="orange", tsunami_flag=True, mag=7.4, time=app.clock.now()))
        await pilot.pause(0.1)
        line = next(r for r in screen_rows(app) if re.match(r"\s*\S?\s*0s\s+7\.4", r))
        assert " TO " in line, line
        assert app.export_screenshot()


async def test_no_color_selected_row_is_still_marked(monkeypatch):
    """With colour stripped the cursor background may vanish; the marker glyph must remain."""
    monkeypatch.setenv("NO_COLOR", "1")
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert app.no_color
        assert any(r.lstrip().startswith(("▌", ">")) for r in screen_rows(app)), "no cursor marker"


# --- (3) live-sort stability ------------------------------------------------------------------------------------

HOME = (40.7, -74.0)


def tie_store(order: list[int], count: int = 6) -> tuple[Store, Config]:
    """`count` quakes sharing time, magnitude and epicentre (a swarm), merged in the given arrival order."""
    config = with_overrides(Config(), 2.5, 24, HOME, "unicode")
    store = Store(config.alerts, config.home)
    quakes = [make_quake(f"sw{i}", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0) for i in range(count)]
    for i in order:
        store.merge(Batch("usgs", T0, quakes=(quakes[i],)), _clock())
    return store, config


def _clock():
    from earthwave.clock import ReplayClock

    return ReplayClock(T0, real=lambda: 0.0)


SORTS = [SortKey.TIME, SortKey.MAG, SortKey.DIST]


@pytest.mark.xfail(
    strict=True, reason="UX-7: Store._put pops then re-inserts, so a re-poll moves ties to the batch's order"
)
@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_equal_key_rows_keep_their_order_across_repeated_unchanged_polls(sort):
    store, _ = tie_store(list(range(6)))
    clock = _clock()
    view = ViewFilter(2.5, 24)
    first = [q.id for q in store.visible(view, clock, sort)]
    reversed_batch = tuple(
        reversed([make_quake(f"sw{i}", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0) for i in range(6)])
    )
    for _ in range(10):
        store.merge(Batch("usgs", T0, quakes=reversed_batch), clock)
        assert [q.id for q in store.visible(view, clock, sort)] == first


@pytest.mark.xfail(strict=True, reason="UX-7: a revised quake moves to the end of its ties (Store._put)")
@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_a_revised_quake_does_not_jump_among_its_equals(sort):
    store, _ = tie_store(list(range(6)))
    clock = _clock()
    view = ViewFilter(2.5, 24)
    first = [q.id for q in store.visible(view, clock, sort)]
    revised = make_quake("sw2", mag=4.0, minutes_ago=30, lat=35.0, lon=140.0, updated=T0, place="revised")
    store.merge(Batch("usgs", T0 + timedelta(seconds=1), quakes=(revised,)), clock)
    assert [q.id for q in store.visible(view, clock, sort)] == first


@pytest.mark.xfail(
    strict=True, reason="UX-7: ties have no final tiebreak, so order follows arrival (restart vs live differ)"
)
@pytest.mark.parametrize("sort", SORTS, ids=lambda s: s.value)
def test_equal_key_order_does_not_depend_on_arrival_order(sort):
    clock = _clock()
    view = ViewFilter(2.5, 24)
    a, _ = tie_store([0, 1, 2, 3, 4, 5])
    b, _ = tie_store([5, 3, 1, 0, 4, 2])
    assert [q.id for q in a.visible(view, clock, sort)] == [q.id for q in b.visible(view, clock, sort)]


def fresh(app, n: int, *, above: bool, tag: str):
    """n quakes newer than everything (above, time sort) or older but still inside the window (below)."""
    now = app.clock.now()
    out = []
    for i in range(n):
        when = now - timedelta(seconds=10 + i) if above else now - timedelta(hours=160, minutes=i)
        out.append(make_quake(f"{tag}{i}", mag=5.0, time=when, lat=-10.0 - i, lon=100.0 + i, updated=when))
    return out


async def _walk(pilot, app, steps: int):
    for _ in range(steps):
        await pilot.press("pagedown")
    await pilot.pause(0.05)
    return app.dashboard.query_one(QuakeList)


@pytest.mark.parametrize("sort_presses", [0, 1, 2], ids=["time", "mag", "dist"])
async def test_cursor_and_scroll_survive_a_poll_that_inserts_above_and_below(sort_presses):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        for _ in range(sort_presses):
            await pilot.press("s")
        await pilot.pause(0.05)
        quakes = await _walk(pilot, app, 2)
        assert quakes.scroll_offset.y > 0, "setup: list must be scrolled"
        for _ in range(3):
            await pilot.press("j")
        sel, line = quakes.selected, quakes.cursor - quakes.scroll_offset.y
        put(app, *fresh(app, 3, above=True, tag="up"), *fresh(app, 3, above=False, tag="dn"))
        await pilot.pause(0.05)
        assert quakes.selected.id == sel.id, "selection moved to another quake"
        assert quakes.cursor - quakes.scroll_offset.y == line, "selected row moved on screen"
        assert 0 <= quakes.cursor - quakes.scroll_offset.y < quakes.size.height


async def test_cursor_at_top_stays_on_its_quake_when_rows_arrive_above():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        first = quakes.selected
        put(app, *fresh(app, 2, above=True, tag="up"))
        await pilot.pause(0.05)
        assert quakes.selected.id == first.id
        assert quakes.scroll_offset.y == 0


async def test_scrolled_away_view_stays_anchored_on_its_top_row():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        quakes.scroll_to(y=40, animate=False, immediate=True)
        await pilot.pause(0.05)
        top = quakes.rows[quakes.scroll_offset.y].id
        sel = quakes.selected.id  # still row 0, off screen
        put(app, *fresh(app, 4, above=True, tag="up"))
        await pilot.pause(0.05)
        assert quakes.rows[quakes.scroll_offset.y].id == top
        assert quakes.selected.id == sel


async def test_idempotent_poll_leaves_cursor_scroll_and_order_untouched():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = await _walk(pilot, app, 3)
        before = ([q.id for q in quakes.rows], quakes.cursor, quakes.scroll_offset.y)
        for _ in range(10):
            put(app, *quakes.rows[:20])
        after = ([q.id for q in quakes.rows], quakes.cursor, quakes.scroll_offset.y)
        assert after == before


async def test_selected_quake_dropping_out_does_not_leave_the_map_on_a_ghost():
    from earthwave.ui.widgets.map_panel import MapPanel

    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        quakes = app.dashboard.query_one(QuakeList)
        for _ in range(5):
            await pilot.press("j")
        gone = quakes.selected
        app.set_query("zzzz-no-such-place")
        await pilot.pause(0.05)
        assert quakes.selected is None
        app.set_query("")
        await pilot.pause(0.05)
        assert quakes.selected is not None
        panel = app.dashboard.query_one(MapPanel)
        picked = getattr(panel, "_selected", None) or getattr(panel, "selected", None)
        assert gone.id != "" and (picked is None or picked.id == quakes.selected.id), (picked, quakes.selected.id)


# --- (4) focus graph ---------------------------------------------------------------------------------------------


def focusables(screen) -> list:
    return [w for w in screen.query("*") if w.focusable and w.display and w.region.area > 0]


async def tab_cycle(pilot, key: str = "tab", limit: int = 12) -> list:
    seen = []
    for _ in range(limit):
        await pilot.press(key)
        await pilot.pause(0.02)
        focused = pilot.app.screen.focused
        if focused in seen:
            break
        seen.append(focused)
    return seen


@pytest.mark.parametrize("size", [(120, 40), (100, 30), (200, 60)], ids=lambda s: f"{s[0]}x{s[1]}")
async def test_dashboard_tab_reaches_every_focusable_and_shift_tab_reverses(size):
    app = week_app()
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        screen = app.screen
        assert screen.focused is not None, "dashboard opens with nothing focused"
        want = set(focusables(screen))
        got = set(await tab_cycle(pilot))
        assert want <= got, f"Tab never reaches {want - got}"
        back = await tab_cycle(pilot, "shift+tab")
        assert set(back) >= want


async def test_open_search_box_takes_focus_and_escape_gives_it_back():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        await pilot.pause(0.05)
        assert isinstance(app.screen.focused, Input)
        for key in ("tab", "shift+tab"):
            await pilot.press(key)
            await pilot.pause(0.02)
        await pilot.press("escape")
        await pilot.pause(0.05)
        box = app.screen.query_one("#search", Input)
        assert not box.display
        assert isinstance(app.screen.focused, QuakeList), app.screen.focused


async def test_tab_from_the_search_box_does_not_strand_focus_on_a_hidden_widget():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        await pilot.pause(0.05)
        for _ in range(6):
            await pilot.press("tab")
            await pilot.pause(0.02)
            focused = app.screen.focused
            assert focused is not None and focused.display and focused.region.area > 0, focused


@pytest.mark.parametrize("opener", ["question_mark", "f", "t", "enter"], ids=["help", "filters", "tsunami", "detail"])
async def test_escape_leaves_every_pushed_screen(opener):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        base = app.screen
        await pilot.press(opener)
        await pilot.pause(0.15)
        assert app.screen is not base, f"{opener} did not open anything"
        pushed = app.screen
        # focus must live somewhere on the pushed screen, and Tab must cycle without escaping or trapping on nothing
        assert pushed.focused is not None or opener in ("question_mark", "enter"), "pushed screen has no focus"
        await tab_cycle(pilot)
        await pilot.press("escape")
        await pilot.pause(0.15)
        assert app.screen is base, f"Escape trapped on {type(pushed).__name__}"


@pytest.mark.parametrize("opener", ["f", "t"], ids=["filters", "tsunami"])
async def test_tab_reaches_every_focusable_on_pushed_screens(opener):
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press(opener)
        await pilot.pause(0.15)
        want = set(focusables(app.screen))
        got = set(await tab_cycle(pilot))
        assert want <= got, f"Tab never reaches {want - got}"


async def test_escape_inside_a_filter_input_cancels_without_applying():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        before = app.view
        await pilot.press("f")
        await pilot.pause(0.15)
        await pilot.press("9", "9")
        await pilot.press("escape")
        await pilot.pause(0.15)
        assert app.screen is app.dashboard and app.view == before


async def test_focus_returns_to_the_list_after_a_too_small_resize_round_trip():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.2)
        await pilot.resize_terminal(120, 40)
        await pilot.pause(0.3)
        assert isinstance(app.screen.focused, QuakeList), app.screen.focused
        await pilot.press("j")
        assert app.dashboard.query_one(QuakeList).cursor == 1


@pytest.mark.xfail(strict=True, reason="UX-8: Escape does nothing in the full-screen map; only M leaves it")
async def test_escape_leaves_the_fullscreen_map():
    from earthwave.ui.widgets.map_panel import MapPanel

    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("M")
        await pilot.pause(0.2)
        assert app.dashboard.has_class("map-full")
        assert isinstance(app.screen.focused, MapPanel)
        await tab_cycle(pilot, limit=4)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not app.dashboard.has_class("map-full"), "Escape does not leave the full-screen map (only M does)"


async def test_hidden_map_is_never_focusable_by_tab():
    from earthwave.ui.widgets.map_panel import MapPanel

    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("m")
        await pilot.pause(0.2)
        seen = await tab_cycle(pilot, limit=6)
        assert not any(isinstance(w, MapPanel) for w in seen), "Tab focused the hidden map"


async def test_detail_scroll_area_is_focused_and_scrolls_with_keys():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, DetailScreen)
        assert app.screen.focused is not None and app.screen.focused.id == "detail_scroll", app.screen.focused
