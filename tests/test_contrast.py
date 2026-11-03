"""Every screen is legible whatever the terminal's own colours are (issues #244, #275).

The app paints its own ground under every cell and names a foreground for every word, so a light terminal profile
(xterm's black on white, Terminal.app's default) never shows through: AGE and DEPTH were Textual's pale grey on the
terminal's white because the quake list left its background unset. This audits the cells Textual would write:
no cell without a ground, no word without a foreground, body text at 4.5:1 and rules and borders at 3:1.
"""

import pytest
from conftest import make_quake
from test_tsunami import kam_app
from test_ui import week_app
from textual.color import Color

from earthwave.ui.theme import CURSOR_BG, LEVEL_COLORS, PAGER_COLORS, _carries_meaning, contrast, lift, mag_color
from earthwave.ui.widgets.quake_list import QuakeList

SCREENS = {
    "dashboard": (week_app, []),
    "detail": (week_app, ["enter"]),
    "filters": (week_app, ["f"]),
    "help": (week_app, ["question_mark"]),
    "command palette": (week_app, ["ctrl+p"]),
    "tsunami": (week_app, ["t"]),
    "warning dashboard": (kam_app, []),
    "warning tsunami screen": (kam_app, ["t"]),
}
BODY, RULE = 4.5, 3.0


def is_land_texture(text: str, fg: Color) -> bool:
    """The braille map's land hatch is a faint fill on purpose (map--hatch); coasts, markers and labels are not."""
    return fg.hex.upper() == "#4F4F4F" and all("⠀" <= c <= "⣿" for c in text)


@pytest.mark.parametrize("screen", SCREENS)
async def test_every_cell_names_both_colours_and_reads(screen):
    make, keys = SCREENS[screen]
    app = make()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        problems = []
        for y, strip in enumerate(app.screen._compositor.render_strips()):
            for seg in strip:
                style = seg.style
                if style is None or style.bgcolor is None:
                    problems.append(f"row {y}: no ground under {seg.text[:12]!r}")
                    continue
                if not seg.text.strip():
                    continue
                if style.color is None:
                    problems.append(f"row {y}: no foreground on {seg.text[:12]!r}")
                    continue
                fg, bg = Color.from_rich_color(style.color), Color.from_rich_color(style.bgcolor)
                if style.reverse:
                    fg, bg = bg, fg
                need = BODY if any(_carries_meaning(c) for c in seg.text) else RULE
                if contrast(fg, bg) < need and not is_land_texture(seg.text, fg):
                    problems.append(f"row {y}: {seg.text[:12]!r} {fg.hex} on {bg.hex} is {contrast(fg, bg):.2f}:1")
        assert not problems, "\n".join(sorted(set(problems))[:20])


async def test_selected_row_foregrounds_read_at_body_contrast():
    """The selected row's navy ground and every status colour printed on it (ANSI red was 2.75:1, issue #275)."""
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quakes = [
            make_quake(f"m{m}", mag=m, pager_alert=p, tsunami_flag=True)
            for m in (2.0, 4.0, 5.0, 6.0, 6.7, 7.5)
            for p in (*PAGER_COLORS, None)
        ]
        widget = app.screen.query_one(QuakeList)
        for quake in quakes:
            for seg in widget._row_strip(quake, True, 100):
                if not seg.text.strip():
                    continue
                style = seg.style
                fg, bg = Color.from_rich_color(style.color), Color.from_rich_color(style.bgcolor)
                assert bg.hex.upper() == CURSOR_BG.upper() or style.bgcolor is not None
                assert contrast(fg, bg) >= BODY, (quake.mag, quake.pager_alert, seg.text, fg.hex, bg.hex)


def test_lift_keeps_a_readable_colour_and_tints_an_unreadable_one():
    bg = Color.parse(CURSOR_BG)
    readable = Color.parse("#ffffff")
    assert lift(readable, bg) == readable
    red = Color.parse("#d03020")
    tinted = lift(red, bg)
    assert contrast(tinted, bg) >= BODY and tinted.r > tinted.g  # lighter, still red
    assert lift(Color.parse("#808080"), Color.parse("#ffffff")).r < 0x80  # darker on a light ground


def test_status_colours_read_on_the_screen_ground():
    ground = Color.parse("#1e1e1e")
    for mag in (2.0, 4.0, 5.0, 6.0, 6.7, 7.5):
        colour = mag_color(mag).removeprefix("bold ")
        if colour.startswith("#"):
            assert contrast(Color.parse(colour), ground) >= BODY, mag
    for name, colour in LEVEL_COLORS.items():
        colour = colour.removeprefix("bold ")
        if colour.startswith("#"):
            assert contrast(Color.parse(colour), ground) >= BODY, name


async def test_filter_dialog_error_follows_the_typing_and_focus_has_a_shape():
    """Hours 'abc' then '72': the error goes (#253). Focus on an input is a heavy white border, on a button reverse
    video, so neither rests on colour alone."""
    from conftest import wait_until
    from textual.widgets import Input, Label

    from earthwave.ui.screens.filters import FiltersScreen

    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_filters()
        await wait_until(pilot, lambda: isinstance(app.screen, FiltersScreen), what="the dialog")
        await wait_until(pilot, lambda: app.screen.query("#min_mag"), what="the fields")
        hours = app.screen.query_one("#hours", Input)
        hours.focus()
        hours.value = "abc"
        await pilot.press("enter")
        await pilot.pause()
        error = app.screen.query_one("#error", Label)
        assert "Hours must be a number" in str(error.render())
        hours.value = "7"
        await pilot.pause()
        assert str(error.render()).strip() == ""
        hours.value = "x"  # nothing is shown until Apply is pressed again
        await pilot.pause()
        assert str(error.render()).strip() == ""
        hours.value = "72"
        await pilot.pause()
        assert str(error.render()).strip() == ""
        assert hours.styles.border.top[0] == "heavy"  # the focused input
        assert app.screen.query_one("#min_mag", Input).styles.border.top[0] == "tall"
