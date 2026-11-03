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
    TEXT,
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
def test_dim_level_grounds_are_told_apart(kind):
    """Status bar and calm border: all five grounds, Warning included, are 20 dE apart. White text reads on every one
    (below), the Warning stays the loudest (further below)."""
    variables = css_variables()
    grounds = {n: _rgb(variables[f"level-{n}-dim"]) for n in ("warning", "advisory", "watch", "threat", "info")}
    delta, pair = _worst_pair(grounds, kind)
    assert delta >= 20, (pair, delta)


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


ANSI = {"black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"}
ANSI |= {f"bright_{n}" for n in ANSI}
STYLE_WORDS = {"bold", "dim", "italic", "underline", "reverse", "strike", "on", "not"}
STYLE_KEYWORDS = {"color", "bgcolor", "style", "fg", "bg", "background"}
STYLE_CALLS_ALL = {"Style", "parse", "RowStyle"}  # every positional argument is a style or colour
STYLE_CALLS_SECOND = {"Text", "append", "stylize", "field", "_row"}  # the arguments after the text are


def _ui_files():
    from pathlib import Path

    root = Path(__file__).parent.parent / "earthwave"
    return [p for p in [*(root / "ui").rglob("*.py"), root / "notify" / "inapp.py"] if p.name != "theme.py"]


def _style_text(node):
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _names_ansi(value: str, single_word_counts: bool) -> bool:
    tokens = value.split()
    # a style is only the words above, colours and placeholders: "turns on green" is prose, not a style
    is_style = bool(tokens) and all(t in STYLE_WORDS or t in ANSI or t.startswith(("grey", "#", "{")) for t in tokens)
    return is_style and any(t in ANSI for t in tokens) and (len(tokens) > 1 or single_word_counts)


def _bare_ansi_in_styles(tree) -> list[tuple[int, str]]:
    """String literals (f-string placeholders blanked) in a position a style goes to that name a bare ANSI colour. A
    one-word "red" counts only where a style or colour is expected, so the PAGER keys are not flagged."""
    import ast

    hits = []
    for node in ast.walk(tree):
        slots: list[ast.AST] = []
        if isinstance(node, ast.Call):
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            slots = [kw.value for kw in node.keywords if kw.arg in STYLE_KEYWORDS]
            if func in STYLE_CALLS_ALL:
                slots += node.args
            elif func in STYLE_CALLS_SECOND:
                slots += node.args[1:]
        elif isinstance(node, ast.FunctionDef):
            names = [a.arg for a in (*node.args.args, *node.args.kwonlyargs)]
            defaults = [*node.args.defaults, *node.args.kw_defaults]
            slots = [
                d
                for n, d in zip(names[len(names) - len(defaults) :], defaults, strict=False)
                if n in STYLE_KEYWORDS and d
            ]
        for slot in slots:
            if (s := _style_text(slot)) is not None and _names_ansi(s, True):
                hits.append((slot.lineno, s))
        if isinstance(node, ast.JoinedStr | ast.Constant) and (s := _style_text(node)) and _names_ansi(s, False):
            hits.append((node.lineno, s))
    return sorted(set(hits))


def test_no_ansi_colour_names_in_the_ui_code():
    """Textual maps ANSI names through the terminal's own theme, so a widget naming one shows a different colour from
    the theme constants for the same meaning (issue #293). Style strings, Style/Text/append arguments and style
    defaults in the UI code use theme constants only."""
    import ast

    files = _ui_files()
    assert len(files) > 10
    for path in files:
        hits = _bare_ansi_in_styles(ast.parse(path.read_text(encoding="utf-8")))
        assert not hits, (path.name, hits)


def test_the_lint_catches_what_it_must_and_spares_prose():
    import ast

    def hits(src):
        return _bare_ansi_in_styles(ast.parse(src))

    for bad in (
        'x.append("t", f"bold black on {CYAN}")',
        'Style(color="white")',
        'Style.parse("red")',
        'Color.parse("white")',
        'Text("x", "bright_white")',
        'def f(style: str = "white"): pass',
        'RowStyle("#000000", "white", True)',
        'S = "bold black on #ffd700"',
    ):
        assert hits(bad), bad
    for good in (
        'log("the map turns on green")',
        'PAGER = {"green": 1, "red": 2}',
        'if alert == "red": pass',
        'x = "bold"',
        'x.append("red", TEXT)',
    ):
        assert not hits(good), good


def _tcss_declarations(source: str):
    import re

    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    for m in re.finditer(r"(?m)\b(color|background|border(?:-[a-z-]+)?|outline|tint)\s*:\s*([^;{}]+);", source):
        yield m.group(1), m.group(2).strip()


NON_COLOUR_WORDS = {
    "none", "hidden", "auto", "transparent", "round", "solid", "heavy", "tall", "wide", "double", "dashed", "inner",
    "outer", "thick", "ascii", "blank", "panel", "tab", "hkey", "vkey", "block", "left", "right", "top", "bottom",
    "center", "middle", "initial",
}  # fmt: skip


def test_tcss_colours_are_all_theme_variables():
    """app.tcss and the widgets' DEFAULT_CSS name colours only as $variables, which are Textual's own or theme.py's:
    no hex, no colour word (issue #293: `color: black` on the banner)."""
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent / "earthwave" / "ui"
    sources = {"app.tcss": (root / "app.tcss").read_text(encoding="utf-8")}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_CSS" for t in node.targets):
                sources[f"{path.name}:{node.lineno}"] = ast.literal_eval(node.value)
    seen = 0
    for name, source in sources.items():
        for prop, value in _tcss_declarations(source):
            seen += 1
            for token in re.sub(r"\([^)]*\)", "", value).split():
                ok = token.startswith("$") or token in NON_COLOUR_WORDS or re.fullmatch(r"[\d.%]+", token)
                assert ok, (name, prop, value)
    assert seen > 30


def test_tcss_variables_are_defined_and_equal_the_constants():
    import re
    from pathlib import Path

    from textual.app import App

    from earthwave.ui import theme

    ground = theme.ground_variables()
    surfaces = theme.css_variables()
    defined = {**App().get_css_variables(), **ground, **surfaces}
    source = (Path(__file__).parent.parent / "earthwave/ui/app.tcss").read_text(encoding="utf-8")
    used = set(re.findall(r"\$([a-z][\w-]*)", source))
    assert not used - set(defined), used - set(defined)
    assert (ground["bright"], ground["title"], ground["ink"], ground["text"], ground["black"]) == (
        theme.WHITE,
        theme.TITLE,
        theme.INK,
        theme.TEXT,
        theme.BLACK,
    )
    assert ground["border"] == theme.BORDER and ground["cursor"] == theme.CURSOR_BG
    for name, (bright, dim) in theme.LEVEL_SURFACES.items():
        assert surfaces[f"level-{name}"] == bright and surfaces[f"level-{name}-dim"] == dim


def test_text_colours_are_truecolor_hex_and_pinned_to_monokai():
    """TEXT, INK and BRIGHT keep what the ANSI names rendered as, so swapping the names for them changed no pixel."""
    import re

    from rich.color import Color as RichColor

    from earthwave.ui import theme

    names = ("TEXT", "INK", "BRIGHT", "BLACK", "WHITE", "TITLE", "FLASH_NEW_BG", "DIM_NEW_BG", "FLASH_UPDATED_BG")
    for name in (*names, "PULSE_MAJOR_BG", "DIM_MAJOR_BG"):
        assert re.fullmatch(r"#[0-9a-f]{6}", getattr(theme, name)), name
    for const, ansi in (("TEXT", "white"), ("INK", "black"), ("BRIGHT", "bright_white")):
        assert RichColor.parse(ansi).get_truecolor(MONOKAI).hex == getattr(theme, const), const


def test_every_text_colour_reads_on_the_grounds_it_is_drawn_on():
    from earthwave.notify import inapp
    from earthwave.ui import theme

    ground = "#1e1e1e"  # the dark ground the app paints under every cell
    lit = [r.bg for r in (inapp.FLASH_NEW, inapp.FLASH_UPDATED, inapp.PULSE_MAJOR)]
    calm = [r.bg for r in (inapp.DIM_NEW, inapp.DIM_MAJOR)]
    controls = [theme.ground_variables()[k] for k in ("button", "button-primary")]
    dims = [dim for name, (_, dim) in theme.LEVEL_SURFACES.items() if name != "warning"]
    cases = [
        ("TEXT", theme.TEXT, [ground, theme.CURSOR_BG, *calm]),
        ("BRIGHT", theme.BRIGHT, [ground]),
        ("TITLE", theme.TITLE, [ground]),
        ("WHITE", theme.WHITE, [theme.CURSOR_BG, *controls, *dims, *lit]),
        ("BLACK", theme.BLACK, [bright for bright, _ in theme.LEVEL_SURFACES.values()]),
        ("INK", theme.INK, [theme.YELLOW, theme.CYAN, theme.GREEN, theme.RED, "#9e9e9e"]),
    ]
    for name, fg, grounds in cases:
        for bg in grounds:
            assert contrast(Color.parse(fg), Color.parse(bg)) >= 4.5, (name, bg)
    # The Warning's calm ground is white-on-red at 3.5:1 by design (see test_dim_surfaces_carry_white_text...)
    assert contrast(Color.parse(theme.BLACK), Color.parse(theme.LEVEL_SURFACES["warning"][1])) >= 4.5


def _chip_grounds():
    return {state.name: _truecolor(colour) for state, colour in FRESHNESS_COLORS.items()}


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_data_state_chips_are_told_apart_by_colour_blind_readers(kind):
    """LOADING, LIVE, STALE and OFFLINE chips: the word is printed, the colour only adds to it."""
    delta, pair = _worst_pair(_chip_grounds(), kind)
    assert delta >= 20, (pair, delta)


def test_chip_ink_reads_on_every_chip_ground():
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


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
@pytest.mark.parametrize("pair", [("threat", "info"), ("watch", "advisory")])
def test_the_pairs_that_were_confused_differ_on_every_surface(kind, pair):
    """THREAT vs INFORMATION and WATCH vs ADVISORY (issue #293): the word, the banner and pulsing border, the status
    bar and calm border are each 20 dE apart."""
    variables = css_variables()
    a, b = pair
    word = {"threat": "THREAT", "info": "INFORMATION", "watch": "WATCH", "advisory": "ADVISORY"}
    surfaces = {
        "word": (_truecolor(LEVEL_COLORS[word[a]]), _truecolor(LEVEL_COLORS[word[b]])),
        "bright": (_rgb(variables[f"level-{a}"]), _rgb(variables[f"level-{b}"])),
        "calm": (_rgb(variables[f"level-{a}-dim"]), _rgb(variables[f"level-{b}-dim"])),
    }
    for surface, (x, y) in surfaces.items():
        delta, _ = _worst_pair({a: x, b: y}, kind)
        assert delta >= 20, (surface, delta)


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_quake_list_badges_and_group_mark_are_told_apart(kind):
    """The FL column (tsunami flag T, PAGER G/Y/O/R) and the place column that carries the aftershock group mark
    ([+3], an indent) in its own colour. Each badge is a letter as well as a colour; the colours differ too."""
    from earthwave.ui.labels import pager_badge, tsunami_badge

    badges = {"T": _truecolor(tsunami_badge(True)[1])}
    badges |= {letter: _truecolor(colour) for letter, colour in (pager_badge(a) for a in PAGER_COLORS)}
    delta, pair = _worst_pair(badges, kind)
    assert delta >= 20, (pair, delta)
    delta, pair = _worst_pair({"place and group mark": _truecolor(TEXT), **badges}, kind)
    assert delta >= 10, (pair, delta)


@pytest.mark.parametrize("kind", ["protanopia", "deuteranopia"])
def test_the_tsunami_badge_is_told_apart_from_the_magnitude_colours(kind):
    """The flag's blue against the MAG column's bands. The 3-4.4 cyan band is the one close neighbour (6 dE: both are
    light blues), but it is a number in another column and the flag is the letter T, so only the others must differ."""
    flag = _truecolor(TSUNAMI_BADGE_COLOR)
    mags = {k: _truecolor(mag_color(v)) for k, v in MAG_SAMPLES.items() if k != "3-4.4"}
    delta, pair = _worst_pair({"flag": flag, **mags}, kind)
    assert delta >= 10, (pair, delta)
    assert flag != _truecolor(mag_color(4.0))
