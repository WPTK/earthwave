"""Clicks on the map: a click that changes nothing leaves the picture alone, and a click on a dot selects its quake."""

import pytest
from conftest import wait_until, week_app
from test_sixel import SIXEL
from textual.geometry import Offset

from earthwave.ui.widgets.map_panel import MapModel, MapPanel
from earthwave.ui.widgets.quake_list import QuakeList

SIZE = (160, 40)


def empty_cells(model: MapModel, cell: tuple[int, int] | None, count: int) -> list[tuple[int, int]]:
    """Map cells (content-relative) with no dot under them, spread over the map."""
    found = [
        (col, row)
        for row in range(0, model.map_rows, 3)
        for col in range(0, model.cols, 7)
        if model.quake_at(col, row, cell) is None
    ]
    assert len(found) >= count
    return found[:: len(found) // count][:count]


def dot_cell(panel: MapPanel, cell: tuple[int, int] | None, skip: str | None = None):
    """A (quake, col, row) whose dot is the only one that cell reaches."""
    model = panel.model
    for quake in sorted(model.quakes, key=lambda q: -q.mag):
        for row in range(model.map_rows):
            for col in range(model.cols):
                if quake.id != skip and model.quake_at(col, row, cell) is quake:
                    return quake, col, row
    raise AssertionError("no dot on the map")


async def settled_panel(app, pilot) -> MapPanel:
    panel = app.screen.query_one(MapPanel)
    await wait_until(pilot, lambda: panel._scene is not None and panel._scene in panel._images, 5.0, "picture built")
    await pilot.pause()
    return panel


# --- the flash ------------------------------------------------------------------------------------------------


async def test_clicks_and_mouse_moves_do_not_rewrite_the_picture():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=SIZE) as pilot:
        panel = await settled_panel(app, pilot)
        origin = panel.content_region.offset
        sea = [Offset(origin.x + c, origin.y + r) for c, r in empty_cells(panel.model, SIXEL.cell, 4)]
        await pilot.click(offset=sea[0])  # the first click moves focus to the map, which repaints the screen
        await pilot.pause()
        writes: list[str] = []
        panel.emit_image(writes.append)  # what the app's overlay does after each screen update
        assert writes
        drawn = len(writes)
        for point in sea:
            await pilot.hover(offset=point)
            await pilot.click(offset=point)
            await pilot.click(offset=point)
            await pilot.pause()
            panel.emit_image(writes.append)
        assert len(writes) == drawn, "the picture was written again although nothing about it changed"
        assert panel.is_attached and app.focused is panel


async def test_click_on_the_list_does_not_repaint_the_picture_when_the_selection_stays():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=SIZE) as pilot:
        panel = await settled_panel(app, pilot)
        writes: list[str] = []
        panel.emit_image(writes.append)
        writes.clear()
        quakes = app.screen.query_one(QuakeList)
        row = Offset(3, quakes.cursor)
        await pilot.click(quakes, offset=row)  # the first click may clear the quake's attention ring: a new picture
        await pilot.pause()
        await wait_until(pilot, lambda: panel._scene in panel._images, what="picture built")
        panel.emit_image(writes.append)
        writes.clear()
        await pilot.click(quakes, offset=row)
        await pilot.click(quakes, offset=row)
        await pilot.pause()
        panel.emit_image(writes.append)
        assert writes == []


# --- the hit test ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cell", [None, SIXEL.cell], ids=["braille", "sixel"])
async def test_hit_test_finds_the_dot_and_nothing_on_empty_sea(cell):
    app = week_app(**({"graphics": SIXEL} if cell else {}))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        model = app.screen.query_one(MapPanel).model
        quake, col, row = dot_cell(app.screen.query_one(MapPanel), cell)
        assert model.quake_at(col, row, cell) is quake
        for c, r in empty_cells(model, cell, 5):
            assert model.quake_at(c, r, cell) is None
        assert model.quake_at(-1, 0, cell) is None
        assert model.quake_at(0, model.map_rows, cell) is None


# --- selecting from the map -----------------------------------------------------------------------------------


@pytest.mark.parametrize("graphics", [None, SIXEL], ids=["braille", "sixel"])
async def test_click_on_a_dot_selects_that_quake_in_the_list(graphics):
    app = week_app(**({"graphics": graphics} if graphics else {}))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        panel, quakes = app.screen.query_one(MapPanel), app.screen.query_one(QuakeList)
        cell = graphics.cell if graphics else None
        start = quakes.selected
        quake, col, row = dot_cell(panel, cell, skip=start.id if start else None)
        origin = panel.content_region.offset
        await pilot.click(offset=Offset(origin.x + col, origin.y + row))
        await wait_until(pilot, lambda: quakes.selected is not None and quakes.selected.id == quake.id, what="selected")
        assert panel.model.selected is quake
        assert quakes.rows[quakes.cursor] is quake


async def test_click_on_empty_sea_selects_nothing():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        panel, quakes = app.screen.query_one(MapPanel), app.screen.query_one(QuakeList)
        before = quakes.selected
        origin = panel.content_region.offset
        for c, r in empty_cells(panel.model, None, 3):
            await pilot.click(offset=Offset(origin.x + c, origin.y + r))
        await pilot.pause()
        assert quakes.selected is before


async def test_click_on_a_folded_aftershock_opens_its_group():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("g")
        panel, quakes = app.screen.query_one(MapPanel), app.screen.query_one(QuakeList)
        await pilot.pause()
        hidden = [q for q in panel.model.quakes if q.id in quakes._children and q.id not in {r.id for r in quakes.rows}]
        assert hidden, "the captured week has no aftershock group"
        quake = hidden[0]
        panel.model.quakes = [quake]  # its mainshock shares its cell, so show the aftershock alone
        spot = next(
            (c, r)
            for r in range(panel.model.map_rows)
            for c in range(panel.model.cols)
            if panel.model.quake_at(c, r, None) is quake
        )
        origin = panel.content_region.offset
        await pilot.click(offset=Offset(origin.x + spot[0], origin.y + spot[1]))
        await wait_until(pilot, lambda: quakes.selected is not None and quakes.selected.id == quake.id, what="revealed")
        assert quakes._children[quake.id] in quakes._expanded


async def test_only_writes_over_the_picture_erase_it():
    from textual._compositor import ChopsUpdate

    app = week_app(graphics=SIXEL)
    async with app.run_test(size=SIZE) as pilot:
        panel = await settled_panel(app, pilot)
        panel.emit_image([].append)
        region = panel.content_region
        panel.note_update(ChopsUpdate([], [(region.y - 1, region.x, region.right)], []))  # the border row above
        panel.note_update(ChopsUpdate([], [(region.y, 0, region.x)], []))  # beside the picture
        panel.note_update(ChopsUpdate([], [(region.y + panel.model.map_rows, region.x, region.right)], []))  # below
        assert not panel._stale
        panel.note_update(ChopsUpdate([], [(region.y + 2, region.x + 5, region.x + 9)], []))
        assert panel._stale
