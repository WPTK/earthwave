"""NO_COLOR makes Textual run every rendered line through its Monochrome filter, which reads `style.color` from each
segment: a segment without a style (Strip.blank(width)) crashed it (#36)."""

from test_ui import week_app

from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList


def test_major_quake_marker_stays_visible_and_map_dots_do_not_change():
    """The M7+ marker is the most important dot on the map; plain Monochrome makes it near-black on the dark ground."""
    from rich.segment import Segment
    from rich.style import Style
    from textual.color import Color

    from earthwave.ui.theme import LegibleMonochrome, contrast

    dark = Color(30, 30, 30)
    red = Style(color="rgb(120,0,0)")
    filtered = LegibleMonochrome().apply
    marker = filtered([Segment("●", red)], dark)[0]
    assert contrast(Color.from_rich_color(marker.style.color), dark) >= 4.5
    plain = super(LegibleMonochrome, LegibleMonochrome())
    for dots in ("⣿", "─", "▇"):
        assert filtered([Segment(dots, red)], dark)[0].style == plain.apply([Segment(dots, red)], dark)[0].style


async def test_app_renders_with_no_color_set(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    app = week_app()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        assert app.no_color
        assert app.export_screenshot()  # renders every widget through the Monochrome filter


async def test_blank_strips_carry_a_style():
    app = week_app()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        dash = app.dashboard
        for strip in (dash.query_one(QuakeList).render_line(10_000), dash.query_one(MapPanel).render_line(10_000)):
            assert strip.cell_length and all(seg.style is not None for seg in strip)


async def test_warning_and_major_text_stays_legible_under_no_color(monkeypatch):
    """Monochrome turns ANSI red (Warning, M7+) near-black, 1.1:1 on the dark ground; LegibleMonochrome lifts it."""
    from textual.color import Color
    from uat.harness import kam_alert_app

    from earthwave.ui.theme import LegibleMonochrome, contrast

    monkeypatch.setenv("NO_COLOR", "1")
    seen: dict[str, float] = {}
    apply = LegibleMonochrome.apply

    def spy(self, segments, background):
        out = apply(self, segments, background)
        for seg in out:
            style, word = seg.style, seg.text.strip()
            if style and style.color and word in ("WARNING", "Tsunami", "M8.8", "8.8"):
                bg = Color.from_rich_color(style.bgcolor) if style.bgcolor else background
                seen[word] = min(seen.get(word, 99), contrast(Color.from_rich_color(style.color), bg))
        return out

    monkeypatch.setattr(LegibleMonochrome, "apply", spy)
    app = kam_alert_app(1)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.no_color and any(isinstance(f, LegibleMonochrome) for f in app._filters)
        app.export_screenshot()
    assert {"WARNING", "Tsunami"} <= seen.keys() and seen.keys() & {"M8.8", "8.8"}
    assert min(seen.values()) >= 4.5, seen
