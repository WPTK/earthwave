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
        await pilot.pause(0.2)  # a lone Escape is held a moment in case it begins a terminal reply
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
        writes.clear()
        panel._stale = True  # the repaint that took the toast away blanked the cells under the picture too
        panel.emit_image(writes.append)
        # the picture without the hole was encoded before the toast came: it goes out in this very update, so the
        # cells are never left blank waiting for a later repaint
        assert panel._holes == () and panel._scene.holes == () and writes
        assert panel._on_terminal == panel._scene


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


def tokens(text):
    """The key presses Textual makes of terminal output it cannot name: Escape, then one key per character."""
    names = {"?": "question_mark", ";": "semicolon", "[": "left_square_bracket", "^": "circumflex_accent"}
    return [("escape", "") if c == "" else (names.get(c, c), c) for c in text]


def run(filter_, text):
    released, cell = [], None
    for token in tokens(text):
        out, got = filter_.push(token)
        released += out
        cell = got or cell
    return released, cell


@pytest.mark.parametrize("prefix", ["", "^", ""])
def test_reply_filter_reads_the_cell_answer_with_or_without_the_escape(prefix):
    from earthwave.ui.graphics.probe import ReplyFilter

    reply = ReplyFilter()
    released, cell = run(reply, f"{prefix}[6;24;12t")
    assert released == [] and cell == (12, 24) and reply.held == []


@pytest.mark.parametrize("text", ["[?61;4;21c", "[?64;1;2;6;9;15;18;21;22c", "[4;800;1600t", "[6;1;1t"])
def test_reply_filter_swallows_every_answer_we_ask_for(text):
    from earthwave.ui.graphics.probe import ReplyFilter

    released, cell = run(ReplyFilter(), text)
    assert released == [] and cell is None


@pytest.mark.parametrize("typed", ["[", "^", "[5", "[5;", "^[", "[5x", "[?", "5", "t", "?", "^5t", "[99999999999t"])
def test_reply_filter_gives_plain_keys_back_in_order(typed):
    from earthwave.ui.graphics.probe import ReplyFilter

    reply = ReplyFilter()
    released, cell = run(reply, typed)
    released += reply.flush()  # the user stopped typing: whatever is still held is theirs
    assert [char for _, char in released] == list(typed) and cell is None


def test_reply_filter_hands_a_lone_escape_back():
    from earthwave.ui.graphics.probe import ReplyFilter

    reply = ReplyFilter()
    assert reply.push(("escape", "")) == ([], None)
    assert reply.flush() == [("escape", "")]


async def late_reply(pilot, app, text, wait=0.2):
    from textual import events

    for key, char in tokens(text):
        app.post_message(events.Key(key, char))
    await pilot.pause(wait)


async def test_late_probe_reply_fires_no_binding():
    """The startup probe timed out (a slow link) and chose braille; its answer then comes in as key presses. It must
    not open Tsunami (t) or Help (?), whatever its terminator and however late."""
    app = week_app(graphics=GraphicsChoice("braille", None, "no reply to the terminal probe", probed=True))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        depth = len(app.screen_stack)
        await late_reply(pilot, app, "[?61;4;21c")
        await late_reply(pilot, app, "[6;24;12t")
        await late_reply(pilot, app, "[4;800;1600t")
        assert len(app.screen_stack) == depth
        assert app.graphics.renderer == "braille"  # a late startup reply is dropped, the choice stands


async def test_late_cell_reply_updates_the_cell_size_of_a_sixel_map():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await pilot.pause()
        depth = len(app.screen_stack)
        await late_reply(pilot, app, "[6;24;12t", wait=0.3)  # nobody asked just now: a reply that took long
        assert len(app.screen_stack) == depth
        assert app.graphics.cell == (12, 24) and panel._cell == (12, 24)


async def test_reply_split_across_the_old_window_still_never_types():
    from textual import events

    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        depth = len(app.screen_stack)
        for key, char in tokens("[?6"):
            app.post_message(events.Key(key, char))
        await pilot.pause(0.02)  # a gap shorter than the hold: the rest follows
        for key, char in tokens("1;4c"):
            app.post_message(events.Key(key, char))
        await pilot.pause(0.2)
        assert len(app.screen_stack) == depth


async def test_typed_brackets_and_digits_get_through_the_filter():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        depth = len(app.screen_stack)
        await pilot.press(
            "[", "5", "x", "^", "question_mark"
        )  # not a reply: the keys are typed, and `?` still opens Help
        await pilot.pause(0.2)
        assert len(app.screen_stack) > depth


async def test_resize_probe_updates_the_cell_size_and_eats_the_reply():
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await pilot.pause()
        if panel._reprobe is not None:  # the start-up resize arms its own re-probe; on a slow runner it fires mid-test
            panel._reprobe.stop()
        app.request_cell_probe()
        await late_reply(pilot, app, "[6;24;12t")
        assert app.graphics.cell == (12, 24)
        assert panel._cell == (12, 24)


async def test_no_probe_for_braille():
    app = week_app()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.request_cell_probe()  # nothing is written, nothing waits


def test_margin_beyond_the_poles():
    from earthwave.ui.graphics.raster import MARGIN, render_base
    from earthwave.ui.widgets.braille import Viewport

    pixels = render_base(Viewport(-180.0, -135.0, 180.0, 135.0), 100, 75)
    assert pixels[:100] == bytes([MARGIN]) * 100 and pixels[-100:] == bytes([MARGIN]) * 100
    assert pixels[37 * 100 + 50] != MARGIN  # the middle is the globe
    assert MARGIN not in render_base(Viewport(-180.0, -90.0, 180.0, 90.0), 100, 50)


async def settle(app, panel):
    """Wait for the wanted picture to be encoded and, as the app's overlay does after every screen update (not driven
    in a headless run), write it if it is not on the terminal yet."""
    await wait_for(lambda: panel._scene in panel._images)
    panel.emit_image(app._driver.write)
    await wait_for(lambda: panel._on_terminal == panel._scene)


async def test_zoom_cursor_move_and_back_leaves_no_zoom_image_rows_under_the_legend(monkeypatch):
    """A taller zoom picture, then the shorter world picture over it: the rows below the world picture must have been
    repainted (as legend and list), so no row of the zoom picture survives there."""
    import re

    from textual.widget import Widget

    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(200, 60)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._scene is not None and panel._scene in panel._images)
        await pilot.pause()
        cell_h = panel._cell[1]
        image_rows: set[int] = set()  # the terminal's rows that still hold some of a picture
        real_refresh = Widget.refresh

        def spy(self, *regions, **kwargs):
            if self is panel:
                if regions:
                    for r in regions:
                        image_rows.difference_update(range(r.y, r.bottom))
                else:
                    image_rows.clear()
            return real_refresh(self, *regions, **kwargs)

        monkeypatch.setattr(Widget, "refresh", spy)
        real_write = app._driver.write

        def write(text):
            if m := re.search(r'"1;1;\d+;(\d+)', text):
                image_rows.update(range(-(-int(m.group(1)) // cell_h)))
            return real_write(text)

        app._driver.write = write

        async def settled():
            await settle(app, panel)

        for _ in range(2):  # world -> wide -> region
            panel.action_cycle_zoom()
            await settled()
        quake = next(q for q in panel.model.quakes if q != panel.model.selected)
        panel.select(quake)  # the cursor moves while zoomed
        await settled()
        panel.action_cycle_zoom()  # close
        await settled()
        panel.action_cycle_zoom()  # back to world, a picture nobody has built for this cursor
        await settled()
        await pilot.pause()
        assert panel.model.map_rows < panel.model.rows
        assert max(image_rows) < panel.model.map_rows, sorted(image_rows)


async def test_held_cursor_key_builds_one_picture_at_a_time_and_ends_on_the_latest(monkeypatch):
    """Every cursor move wants a new picture; the encoder runs on one thread at a time, the pictures nobody waits for
    any more are never built, and the last one wanted is the one that ends up on the terminal."""
    import threading
    import time

    from earthwave.ui.widgets import map_panel

    running = peak = 0
    built: list[int] = []
    lock = threading.Lock()
    real = map_panel.render_smooth

    def slow(scene):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        try:
            return real(scene)
        finally:
            with lock:
                running -= 1
                built.append(1)

    monkeypatch.setattr(map_panel, "render_smooth", slow)
    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._scene is not None and panel._scene in panel._images)
        await pilot.pause()
        built.clear()
        quakes = list(panel.model.quakes)
        for quake in quakes[:12]:
            panel.select(quake)
            await pilot.pause(0.01)
        await settle(app, panel)
        assert panel.model.selected == quakes[11]
        assert peak == 1
        assert len(built) < 12  # the pictures in between were dropped


async def test_switching_between_cached_pictures_does_not_repaint_the_cells_under_them(monkeypatch):
    """A marker pulse or a cursor move that lands on a picture already encoded goes over the old one in place; a
    repaint of the cells under the picture would blank the map for a moment (the idle flicker seen in Windows
    Terminal)."""
    from textual.widget import Widget

    app = week_app(graphics=SIXEL)
    async with app.run_test(size=(160, 40)) as pilot:
        panel = app.screen.query_one(MapPanel)
        await wait_for(lambda: panel._scene is not None and panel._scene in panel._images)
        await pilot.pause()
        first, second = list(panel.model.quakes)[:2]
        for quake in (first, second, first):  # both pictures encoded and cached
            panel.select(quake)
            await settle(app, panel)
        await pilot.pause()
        panel.emit_image(app._driver.write)  # the overlay's write after that last repaint
        assert not panel._stale
        wanted = panel._scene
        calls, writes = [], []
        real = Widget.refresh
        monkeypatch.setattr(
            Widget,
            "refresh",
            lambda self, *a, **k: (calls.append(a) if self is panel else None, real(self, *a, **k))[1],
        )
        real_write = app._driver.write
        app._driver.write = lambda text: (writes.append(text), real_write(text))[1]
        panel.select(second)  # cached
        await pilot.pause()
        assert panel._scene != wanted and panel._on_terminal == panel._scene
        assert any("\x1bP" in w for w in writes)  # the new picture went out
        assert not any(a == () for a in calls)  # and no repaint of the whole panel came with it
