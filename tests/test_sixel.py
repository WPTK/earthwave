"""Optional sixel map: the encoder against an independent decoder, the terminal probe and the renderer decision, the
config setting, the renderer switch in the panel, and the fallback to braille."""

import random

import pytest
from sixel_decode import decode
from test_ui import wait_for, week_app

from earthwave.config import Config, ConfigError, parse_config
from earthwave.ui.graphics.probe import (
    FALLBACK_CELL,
    GraphicsChoice,
    decide,
    parse_reply,
)
from earthwave.ui.graphics.sixel import HOLE, encode, sixel_rgb
from earthwave.ui.widgets.map_panel import MapPanel

# What Windows Terminal 1.25 answered to the real probe (cell 10x20, text area 1200x600, DA1 with attribute 4).
WT_REPLY = "\x1b[6;20;10t\x1b[4;600;1200t\x1b[?61;4;6;7;14;21;22;23;24;28;32;42c"
SIXEL = GraphicsChoice("sixel", (10, 20), "test")


def never() -> str:
    raise AssertionError("the terminal must not be probed")


# --- encoder ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(1, 1), (7, 5), (40, 6), (33, 13), (64, 30)])
def test_encoder_round_trip(width, height):
    rng = random.Random(width * 100 + height)
    palette = [(0, 0, 0), (255, 255, 255), (230, 30, 30), (10, 120, 250), (33, 34, 35)]
    # runs (long stretches of one colour) and noise, so run-length coding and band edges are both exercised
    pixels = bytearray()
    for _ in range(height):
        row = bytearray()
        while len(row) < width:
            row += bytes([rng.randrange(len(palette))]) * rng.choice([1, 1, 3, 9, 30])
        pixels += row[:width]
    out = encode(bytes(pixels), width, height, palette)
    rows, w, h = decode(out)
    assert (w, h) == (width, height)
    for y in range(height):
        for x in range(width):
            assert rows[y][x] == sixel_rgb(palette[pixels[y * width + x]]), (x, y)


def test_encoder_envelope_and_opaque():
    out = encode(bytes(12), 4, 3, [(1, 2, 3)])
    assert out.startswith("\x1bP") and out.endswith("\x1b\\")
    rows, _, _ = decode(out)
    assert all(px is not None for row in rows for px in row)  # colour 0 is written too: no background shows through


def test_encoder_leaves_holes_unpainted():
    out = encode(bytes([HOLE]) * 6 + bytes(6), 4, 3, [(1, 2, 3)])  # row 0 and the first half of row 1 are holes
    rows, _, _ = decode(out)
    assert [px is None for px in rows[0]] == [True] * 4
    assert [px is None for px in rows[1]] == [True, True, False, False]
    assert all(px is not None for px in rows[2])


def test_punch_holes_marks_cell_rectangles():
    from dataclasses import replace

    from earthwave.ui.graphics.raster import Scene
    from earthwave.ui.widgets.map_panel import punch_holes

    scene = Scene(view=None, width=8, height=12, palette=((0, 0, 0),), cell=(2, 3), holes=((1, 1, 2, 2),))
    out = punch_holes(bytes(8 * 12), scene)
    assert [i for i, p in enumerate(out) if p == HOLE] == [y * 8 + x for y in range(3, 9) for x in range(2, 6)]
    assert replace(scene, holes=()) != scene  # the holes are part of the cache key


def test_encoder_rejects_wrong_length():
    with pytest.raises(ValueError):
        encode(bytes(5), 4, 3, [(0, 0, 0)])


# --- probe parsing ----------------------------------------------------------------------------------------------


def test_parse_real_windows_terminal_reply():
    reply = parse_reply(WT_REPLY, 120, 30)
    assert reply is not None and reply.sixel and reply.cell == (10, 20)


def test_parse_cell_from_text_area_when_no_cell_reply():
    reply = parse_reply("\x1b[4;600;1200t\x1b[?62;4c", 120, 30)
    assert reply is not None and reply.cell == (10, 20)


def test_parse_no_sixel_attribute():
    reply = parse_reply("\x1b[6;20;10t\x1b[?62;22c", 120, 30)
    assert reply is not None and not reply.sixel
    assert parse_reply("\x1b[?64;14c") is not None  # 14 is not 4


def test_parse_no_device_attributes_means_no_answer():
    assert parse_reply("") is None
    assert parse_reply("\x1b[6;20;10t") is None


def test_parse_rejects_absurd_cell_sizes():
    reply = parse_reply("\x1b[6;1;1t\x1b[?4c")
    assert reply is not None and reply.cell is None


# --- the auto decision table ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting,environ,tty,reply,renderer,why",
    [
        ("auto", {}, True, WT_REPLY, "sixel", "reports sixel"),
        ("auto", {}, False, WT_REPLY, "braille", "not a terminal"),
        ("auto", {"NO_COLOR": "1"}, True, WT_REPLY, "braille", "NO_COLOR"),
        ("auto", {"TERM": "dumb"}, True, WT_REPLY, "braille", "dumb"),
        ("auto", {"TMUX": "/tmp/x,1,0"}, True, WT_REPLY, "braille", "tmux"),
        ("auto", {"STY": "123.pts"}, True, WT_REPLY, "braille", "tmux or screen"),
        ("auto", {"TERM": "screen-256color"}, True, WT_REPLY, "braille", "tmux or screen"),
        ("auto", {"TERM": "tmux-256color"}, True, WT_REPLY, "braille", "tmux or screen"),
        ("auto", {}, True, "", "braille", "no reply"),  # the 200 ms timeout: the query returned nothing
        ("auto", {}, True, "\x1b[6;20;10t\x1b[?62;22c", "braille", "does not report sixel"),
        ("auto", {}, True, "\x1b[?62;4c", "braille", "cell size"),
        ("braille", {}, True, WT_REPLY, "braille", "braille"),
        ("sixel", {}, True, WT_REPLY, "sixel", "sixel"),
        ("sixel", {}, True, "", "sixel", "sixel"),  # forced: no answer still means sixel, with the fallback cell
        ("sixel", {"TMUX": "x"}, True, WT_REPLY, "sixel", "sixel"),  # forced ignores the multiplexer
        ("sixel", {}, False, WT_REPLY, "braille", "not a terminal"),  # but never writes an image to a pipe
        ("sixel", {"NO_COLOR": "1"}, True, WT_REPLY, "braille", "NO_COLOR"),
    ],
)
def test_decision_table(setting, environ, tty, reply, renderer, why):
    choice = decide(setting, environ, tty, (120, 30), lambda: reply)
    assert choice.renderer == renderer
    assert why in choice.reason
    assert (choice.cell is not None) == (renderer == "sixel")


def test_decision_cell_sizes():
    assert decide("auto", {}, True, (120, 30), query=lambda: WT_REPLY).cell == (10, 20)
    assert decide("sixel", {}, True, (120, 30), query=lambda: "").cell == FALLBACK_CELL


@pytest.mark.parametrize(
    "setting,environ,tty",
    [("braille", {}, True), ("auto", {}, False), ("auto", {"NO_COLOR": "1"}, True), ("auto", {"TERM": "dumb"}, True)],
)
def test_terminal_not_probed_when_braille_is_certain(setting, environ, tty):
    assert decide(setting, environ, tty, (120, 30), never).renderer == "braille"


# --- config -----------------------------------------------------------------------------------------------------


def test_config_default_and_values():
    assert Config().view.map_renderer == "auto"
    for value in ("auto", "braille", "sixel"):
        assert parse_config({"view": {"map_renderer": value}}).view.map_renderer == value


@pytest.mark.parametrize("bad", ["kitty", "SIXEL", "", 1, True])
def test_config_rejects_other_values(bad):
    with pytest.raises(ConfigError, match="map_renderer"):
        parse_config({"view": {"map_renderer": bad}})


def test_template_documents_the_setting():
    from earthwave.config import TEMPLATE

    assert 'map_renderer = "auto"' in TEMPLATE
    assert parse_config(__import__("tomllib").loads(TEMPLATE)).view.map_renderer == "auto"


# --- the panel --------------------------------------------------------------------------------------------------


async def test_default_panel_is_braille_and_writes_nothing():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one(MapPanel)
        assert panel.renderer == "braille"
        writes: list[str] = []
        panel.emit_image(writes.append)
        assert writes == []


async def test_sixel_panel_draws_image_once_then_stays_quiet():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        assert panel.renderer == "sixel"
        await wait_for(lambda: panel._images)
        await pilot.pause()
        writes: list[str] = []
        panel._stale = True
        panel.emit_image(writes.append)
        assert writes and "\x1bP" in "".join(writes)
        region = panel.content_region
        assert writes[0] == f"\x1b7\x1b[{region.y + 1};{region.x + 1}H"  # cursor placed at the panel, then restored
        assert writes[-1] == "\x1b8"
        # idle: nothing repainted, nothing to write
        again: list[str] = []
        panel.emit_image(again.append)
        assert again == []
        # Textual repainting the cells erases the picture, so it is sent again
        panel.render_lines(region)
        panel.emit_image(again.append)
        assert again


async def test_sixel_image_matches_the_panel_size():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._images)
        await pilot.pause()
        data = next(iter(panel._images.values()))
        _, w, h = decode(data)
        full = panel.model.map_rows * 20  # a world map in a tall panel leaves the rows under it for text
        assert w == panel.content_region.width * 10
        assert h % 6 == 0 and full - 6 < h <= full  # whole sixel bands, so no half band sticks out


async def test_image_not_written_under_another_screen():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._images)
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen is not panel.screen
        writes: list[str] = []
        panel._stale = True
        panel.emit_image(writes.append)
        assert writes == []
        assert panel._on_terminal is None
        await pilot.press("escape")
        await pilot.pause()
        await wait_for(lambda: panel._scene is not None and panel._images.get(panel._scene) is not None)
        panel.emit_image(writes.append)  # back on top: on the terminal again (the real overlay may have drawn it first)
        assert panel._on_terminal == panel._scene


async def test_toast_over_the_image_gets_a_hole_in_the_picture():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        await pilot.press("M")  # the full-screen map: a toast sits on the image
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._scene is not None and panel._scene in panel._images)
        assert panel._holes == () and panel._scene.holes == ()
        app.notify("hello", title="toast", timeout=30)
        await wait_for(lambda: app.screen.query("Toast"))
        writes: list[str] = []
        panel._stale = True
        panel.emit_image(
            writes.append
        )  # the overlay hook of the real app: it sees the toast and holds the picture back
        assert writes == [] and panel._holes
        await wait_for(lambda: panel._scene.holes == panel._holes)
        await wait_for(lambda: panel._scene in panel._images)
        data = panel._images[panel._scene]
        rows, _, _ = decode(data)
        col, row = panel._holes[0][:2]
        cw, ch = panel._cell
        assert rows[row * ch + 1][col * cw + 1] is None  # nothing painted under the toast
        assert rows[1][1] is not None  # and the rest of the picture is there
        panel._stale = True
        panel.emit_image(writes.append)  # now the picture with the hole is written
        assert writes and panel._on_terminal == panel._scene
        for toast in list(app.screen.query("Toast")):
            await toast.remove()
        panel.emit_image(writes.append)
        await wait_for(lambda: panel._holes == () and panel._scene.holes == ())


async def test_write_failure_falls_back_to_braille():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._images)
        await pilot.pause()

        def broken(_data: str) -> None:
            raise OSError("pipe closed")

        panel._stale = True
        panel.emit_image(broken)
        assert panel.renderer == "braille"
        await pilot.pause()
        assert panel.model is not None


async def test_sixel_overlay_registered_with_the_app():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one(MapPanel)
        assert panel.emit_image in app._overlays
    assert panel.emit_image not in app._overlays


# --- cell size re-probe after a resize ------------------------------------------------------------------------------


def feed_all(reply, text):
    return [reply.feed(c) for c in text]


def test_world_map_keeps_equal_scale_and_leaves_the_rest_for_text():
    from earthwave.ui.widgets.map_panel import MapModel, world_rows

    model = MapModel()
    model.resize(70, 31)
    assert model.map_rows == world_rows(70) < 31
    view = model.view()
    assert view.lat1 - view.lat0 >= 180  # the whole globe, top-aligned at equal scale
    model.resize(70, world_rows(70) + 1)  # one spare row is too few: the map takes everything, as before
    assert model.map_rows == model.rows


@pytest.mark.parametrize("sixel", [False, True])
async def test_spare_rows_show_legend_then_strongest_quakes(sixel):
    app = week_app(graphics=SIXEL) if sixel else week_app()
    async with app.run_test(size=(200, 60)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await pilot.pause()
        strips = panel._spare_strips()
        assert strips and "7+" in strips[0].text
        for label in ("<3", "3-4.4", "4.5-5.4", "5.5-6.4", "6.5-6.9", "7+"):  # the real bands of mag_color, no gaps
            assert label in strips[0].text
        background = {seg.style.bgcolor for strip in strips for seg in strip if seg.style}
        assert len(background) == 1 and None not in background  # text and padding share the panel's colour
        assert all(strip.cell_length == panel.size.width for strip in strips)
        mags = [float(s.text.split()[0][1:]) for s in strips[1:]]
        assert mags and mags == sorted(mags, reverse=True)
        assert panel.render_line(panel.model.map_rows).text == strips[0].text


def test_cell_reply_reads_the_answer_with_or_without_the_escape():
    from earthwave.ui.graphics.probe import CellReply

    for prefix in ("\x1b", "^", ""):
        reply = CellReply()
        results = feed_all(reply, f"{prefix}[6;24;12t")
        assert all(consumed for consumed, _ in results)
        assert results[-1][1] == (12, 24)
        assert reply.replies == 1


def test_cell_reply_lets_other_keys_through():
    from earthwave.ui.graphics.probe import CellReply

    reply = CellReply()
    assert reply.feed("z") == (False, None)
    assert reply.feed("t") == (False, None)
    reply.feed("[")
    reply.feed("5")
    assert reply.feed("x") == (False, None)  # not a reply: the state is dropped and the key is typed
    assert reply.feed("z") == (False, None)


def test_cell_reply_rejects_absurd_sizes_but_counts_the_reply():
    from earthwave.ui.graphics.probe import CellReply

    reply = CellReply()
    results = feed_all(reply, "[6;1;1t")
    assert results[-1] == (True, None)
    assert reply.replies == 1


async def test_resize_probe_updates_the_cell_size_and_eats_the_reply():
    from textual import events

    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await pilot.pause()
        if panel._reprobe is not None:  # the start-up resize arms its own re-probe; on a slow runner it fires mid-test
            panel._reprobe.stop()
        app._cell_reply, app._cell_pending = None, 0
        app.request_cell_probe()
        assert app._cell_pending == 1
        for char in "[6;24;12t":
            await app.on_event(events.Key("x", char))
        await pilot.pause()
        assert app.graphics.cell == (12, 24)
        assert panel._cell == (12, 24)
        assert app._cell_reply is None and app._cell_pending == 0


async def test_no_probe_for_braille():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.request_cell_probe()
        assert app._cell_reply is None


def test_margin_beyond_the_poles():
    from earthwave.ui.graphics.raster import MARGIN, render_base
    from earthwave.ui.widgets.braille import Viewport

    pixels = render_base(Viewport(-180.0, -135.0, 180.0, 135.0), 100, 75)
    assert pixels[:100] == bytes([MARGIN]) * 100 and pixels[-100:] == bytes([MARGIN]) * 100
    assert pixels[37 * 100 + 50] != MARGIN  # the middle is the globe
    assert MARGIN not in render_base(Viewport(-180.0, -90.0, 180.0, 90.0), 100, 50)
