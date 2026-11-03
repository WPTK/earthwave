"""Status colours stay tellable apart for red-green colour-blind readers (issue #293).

Magnitude bands, PAGER levels and tsunami levels share hue families (green/yellow/orange/red), which collapse under
protanopia and deuteranopia unless lightness differs too. Distance is CIE76 dE after a Machado et al. 2009 simulation
(severity 1.0, linear RGB), read off the colours the app renders (Textual maps the ANSI names through its terminal theme).
"""

from itertools import combinations

import pytest
from rich.style import Style
from textual._ansi_theme import MONOKAI
from textual.color import Color

from earthwave.ui.theme import (
    FRESHNESS_COLORS,
    INK,
    LEVEL_COLORS,
    PAGER_COLORS,
    TSUNAMI_BADGE_COLOR,
    contrast,
    css_variables,
    mag_color,
)

_MACHADO = {
    "protanopia": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
    "deuteranopia": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
}
MAG_SAMPLES = {"<3": 2.0, "3-4.4": 4.0, "4.5-5.4": 5.0, "5.5-6.4": 6.0, "6.5-6.9": 6.7, "7+": 7.5}


def _truecolor(style: str) -> tuple[int, int, int]:
    c = Style.parse(style).color.get_truecolor(MONOKAI)
    return c.red, c.green, c.blue


def _lin(rgb):
    return [(v / 255) / 12.92 if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in rgb]


def _unlin(v):
    v = min(max(v, 0.0), 1.0)
    return 255 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)


def _lab(rgb):
    r, g, b = _lin(rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _simulate(rgb, kind):
    lin = _lin(rgb)
    return tuple(_unlin(sum(m * c for m, c in zip(row, lin, strict=True))) for row in _MACHADO[kind])


def _worst_pair(colours, kind):
    return min(
        (
            sum(
                (p - q) ** 2
                for p, q in zip(_lab(_simulate(colours[a], kind)), _lab(_simulate(colours[b], kind)), strict=True)
            )
            ** 0.5,
            f"{a} vs {b}",
        )
        for a, b in combinations(colours, 2)
    )


def _palettes():
    mags = {k: _truecolor(mag_color(v)) for k, v in MAG_SAMPLES.items()}
    pagers = {k: _truecolor(v) for k, v in PAGER_COLORS.items()}
    return mags, pagers


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_green_and_yellow_survive_red_green_colour_blindness(kind):
    for name, palette in zip(("magnitude", "PAGER"), _palettes(), strict=True):
        delta, pair = _worst_pair(palette, kind)
        assert delta >= 20, (name, pair, delta)


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_every_tsunami_level_is_told_apart_by_colour_blind_readers(kind):
    levels = {name: _truecolor(style) for name, style in LEVEL_COLORS.items()}
    delta, pair = _worst_pair(levels, kind)
    assert delta >= 20, (pair, delta)


def test_status_colours_read_on_the_dark_ground():
    dark = Color(30, 30, 30)
    for style in [*PAGER_COLORS.values(), *LEVEL_COLORS.values(), *(mag_color(m) for m in MAG_SAMPLES.values())]:
        assert contrast(Color(*_truecolor(style)), dark) >= 4.5, style


def _rgb(colour: str) -> tuple[int, int, int]:
    c = Color.parse(colour)
    return c.r, c.g, c.b


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_bright_level_colours_are_told_apart_by_colour_blind_readers(kind):
    """Banner and pulsing border: Warning, Advisory, Watch, Threat and Information differ from one another."""
    variables = css_variables()
    surfaces = {n: _rgb(variables[f"level-{n}"]) for n in ("warning", "advisory", "watch", "threat", "info")}
    delta, pair = _worst_pair(surfaces, kind)
    assert delta >= 20, (pair, delta)


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_dim_level_grounds_keep_warning_apart_and_the_rest_apart(kind):
    """Status bar and calm border. The level word is printed on every surface, so colour is a secondary cue here and a
    Warning that must be the loudest ground (issues #152, #192) cannot also sit 20 dE from all four others: Warning
    differs from each other ground by >= 20 dE, and the other four from one another by >= 10 (Threat from Information
    included)."""
    variables = css_variables()
    grounds = {n: _rgb(variables[f"level-{n}-dim"]) for n in ("warning", "advisory", "watch", "threat", "info")}
    others = {n: v for n, v in grounds.items() if n != "warning"}
    for name, rgb in others.items():
        delta, pair = _worst_pair({"warning": grounds["warning"], name: rgb}, kind)
        assert delta >= 20, (pair, delta)
    delta, pair = _worst_pair(others, kind)
    assert delta >= 10, (pair, delta)


def test_level_surfaces_agree_with_the_level_words():
    """One source of truth: a surface is the level word's colour (or its calm step), never a second palette."""
    variables = css_variables()
    for name, style in LEVEL_COLORS.items():
        if name == "CANCELLATION":
            continue
        key = "info" if name == "INFORMATION" else name.lower()
        assert variables[f"level-{key}"] == Style.parse(style).color.name


def test_dim_surfaces_carry_white_text_and_bright_ones_black():
    for name, colour in css_variables().items():
        text = Color(255, 255, 255) if name.endswith("-dim") else Color(0, 0, 0)
        floor = 3.5 if name == "level-warning-dim" else 4.5  # the Warning bar was white on solid red, 4.0:1, before
        assert contrast(text, Color.parse(colour)) >= floor, name


def test_a_warning_ground_is_louder_than_every_other_level_ground():
    """The Warning status bar and calm border are the brightest and most saturated of the dim grounds (a Warning once
    looked like a Watch: issues #152, #192), so darkening the set for white text and colour-blind separation cannot
    quietly invert the order."""

    def loudness(name):
        rgb = _rgb(css_variables()[f"level-{name}-dim"])
        _, a, b = _lab(rgb)
        return max(rgb), (a * a + b * b) ** 0.5  # brightness, CIE chroma

    warning = loudness("warning")
    for other in ("advisory", "watch", "threat", "info"):
        assert warning[0] > loudness(other)[0] and warning[1] > loudness(other)[1], other


def test_no_ansi_colour_names_left_in_the_ui_code():
    """Textual maps ANSI names through the terminal's own theme, so a widget naming one shows a different colour from the
    theme constants for the same meaning, and a light profile changes it again (issue #293). Every colour in the UI
    code is a theme.py constant, a truecolor or a fixed 256-colour grey; Textual's own CSS colour names in app.tcss
    are truecolor and not affected."""
    import re
    from pathlib import Path

    names = "black|red|green|yellow|blue|magenta|cyan|white"
    word = rf"(?:bright_)?(?:{names})"
    # a name inside a string literal that is a Rich style: "red", "bold white", "bold black on cyan", "dim red"
    style = re.compile(rf"""["'](?:(?:bold|dim|italic|underline|reverse) )*{word}(?: on [#\w]+)?["']|on {word}""")
    keyword = re.compile(rf"""(?:color|bgcolor|style)\s*=\s*["']{word}["']""")
    root = Path(__file__).parent.parent / "earthwave"
    files = [*(root / "ui").rglob("*.py"), root / "notify" / "inapp.py"]
    assert len(files) > 10
    for path in files:
        if path.name == "theme.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            code = line.split("#", 1)[0] if "#" in line and not re.search(r"""["'][^"']*#""", line) else line
            assert not (style.search(code) or keyword.search(code)), (path.name, line.strip())


def _chip_grounds():
    return {state.name: _truecolor(colour) for state, colour in FRESHNESS_COLORS.items()}


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_data_state_chips_are_told_apart_by_colour_blind_readers(kind):
    """LOADING, LIVE, STALE and OFFLINE chips: the word is printed, the colour only adds to it."""
    delta, pair = _worst_pair(_chip_grounds(), kind)
    assert delta >= 20, (pair, delta)


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_chip_ink_reads_on_every_chip_ground(kind):
    for name, rgb in _chip_grounds().items():
        assert contrast(Color.parse(INK), Color(*rgb)) >= 4.5, name


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_pulse_step_of_a_level_border_is_visible(kind):
    """The pulsing border alternates between the calm and the bright step. A Warning is the exception: its calm step is
    already the loudest red (issues #152, #192) and the banner and word carry the alert, so it may not pulse visibly."""
    variables = css_variables()
    for name in ("advisory", "watch", "threat", "info"):
        pair = {"calm": _rgb(variables[f"level-{name}-dim"]), "bright": _rgb(variables[f"level-{name}"])}
        delta, _ = _worst_pair(pair, kind)
        assert delta >= 10, (name, delta)


def test_status_colours_do_not_move_with_the_terminals_ansi_theme():
    """A light terminal profile maps the sixteen ANSI names to other colours (Textual's light ANSI theme here). Every
    status colour is truecolor, so the colour-blind separation pinned above holds on it unchanged."""
    from textual._ansi_theme import ALABASTER

    styles = [*LEVEL_COLORS.values(), *PAGER_COLORS.values(), *FRESHNESS_COLORS.values()]
    styles += [mag_color(m) for m in MAG_SAMPLES.values()] + [TSUNAMI_BADGE_COLOR]
    for style in styles:
        colour = Style.parse(style).color
        assert colour.get_truecolor(ALABASTER) == colour.get_truecolor(MONOKAI), style


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_threat_and_information_differ_on_every_surface(kind):
    """The pair #293 was opened for: word, banner/pulsing border and status-bar/calm border."""
    variables = css_variables()
    pairs = {
        "word": (_truecolor(LEVEL_COLORS["THREAT"]), _truecolor(LEVEL_COLORS["INFORMATION"])),
        "bright": (_rgb(variables["level-threat"]), _rgb(variables["level-info"])),
        "calm": (_rgb(variables["level-threat-dim"]), _rgb(variables["level-info-dim"])),
    }
    for surface, (a, b) in pairs.items():
        delta, _ = _worst_pair({"threat": a, "info": b}, kind)
        assert delta >= 10, (surface, delta)
