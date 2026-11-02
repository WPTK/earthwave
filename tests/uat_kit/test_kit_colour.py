"""S6 self-test of the kit: cell colours and attributes, and the raw byte stream, with a program that has nothing to do
with any project. The colour check passes on a colour run and fails on a monochrome run."""

from __future__ import annotations

import sys

import pytest

from uat_kit import Terminal, unavailable_reason
from uat_kit.terminal import is_chromatic, sgr_chromatic, sgr_depth

pytestmark = pytest.mark.skipif(unavailable_reason() is not None, reason=str(unavailable_reason()))

ESC = chr(27)

PAINT = """
import sys, time
E = chr(27)
if sys.argv[1] == "colour":
    print(E + "[38;2;200;30;30mred" + E + "[0m " + E + "[1;44mbold-on-blue" + E + "[0m", flush=True)
else:
    print(E + "[1mbold" + E + "[0m plain", flush=True)
time.sleep(30)
"""


def paint(mode: str) -> Terminal:
    return Terminal([sys.executable, "-c", PAINT, mode], 40, 6)


def assert_cells_coloured(term: Terminal) -> None:
    """Mid-run: the emulator drew colours with a hue."""
    assert len(term.chromatic_on_screen()) >= 2, term.colours_on_screen()


def assert_stream_coloured(term: Terminal) -> None:
    """After the run: the stream asked for colour with a hue."""
    assert sgr_chromatic(term.raw_stream()) >= 2, term.colour_depth()


def test_cells_carry_colours_and_attributes_and_the_stream_is_readable() -> None:
    with paint("colour") as term:
        term.wait_for_text("bold-on-blue", 20)
        row = term.cells()[0]
        assert row[0].char == "r" and row[0].fg == "#c81e1e"  # truecolor 200,30,30
        blue = next(c for c in row if c.char == "b")
        assert blue.bold and blue.bg != "default"
        assert_cells_coloured(term)
        assert term.raw_stream() == ""  # not readable while the program runs
    assert f"{ESC}[38;2;200;30;30mred" in term.raw_stream()
    assert term.colour_depth() == "truecolor"
    assert_stream_coloured(term)


def test_the_colour_check_fails_on_a_monochrome_run() -> None:
    with paint("mono") as term:
        term.wait_for_text("plain", 20)
        assert term.cells()[0][0].bold
        with pytest.raises(AssertionError):
            assert_cells_coloured(term)  # passes on the colour run above, fails here
    assert term.colour_depth() == "mono"
    with pytest.raises(AssertionError):
        assert_stream_coloured(term)


def test_greys_are_not_colour() -> None:
    assert is_chromatic("#c81e1e") and not is_chromatic("#484848")
    assert sgr_chromatic(f"{ESC}[38;2;72;72;72m{ESC}[48;2;30;30;30m") == 0
    assert sgr_chromatic(f"{ESC}[38;2;200;30;30m{ESC}[44m") == 2


def test_sgr_depth_levels() -> None:
    assert sgr_depth(f"{ESC}[1mx{ESC}[0m") == "mono"
    assert sgr_depth(f"{ESC}[31mx") == "16"
    assert sgr_depth(f"{ESC}[1;38;5;200mx") == "256"
    assert sgr_depth(f"{ESC}[48;2;1;2;3mx") == "truecolor"
