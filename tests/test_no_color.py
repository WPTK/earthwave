"""NO_COLOR makes Textual run every rendered line through its Monochrome filter, which reads `style.color` from each
segment: a segment without a style (Strip.blank(width)) crashed it (#36)."""

from test_ui import week_app

from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList


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
