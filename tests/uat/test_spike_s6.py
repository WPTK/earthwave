"""S6 on earthwave: the kit reads cell colours and the raw stream from the real app, and a colour assertion fails on a
monochrome run (NO_COLOR) and passes on a colour run. Runs the source (python -m earthwave) under tui-test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from uat_kit import Terminal, unavailable_reason
from uat_kit.terminal import sgr_chromatic

USGS = Path(__file__).parents[1] / "fixtures" / "replay" / "usgs_short.jsonl"
ALT_SCREEN = chr(27) + "[?1049h"
pytestmark = pytest.mark.skipif(unavailable_reason() is not None, reason=str(unavailable_reason()))


def launch(no_color: bool) -> Terminal:
    argv = [sys.executable, "-m", "earthwave", "--no-config", "--replay", str(USGS), "--hours", "720", "--min-mag", "2.5"]
    if no_color:
        return Terminal(argv, 130, 32, env={"NO_COLOR": "1"})
    return Terminal(argv, 130, 32, env_remove=("NO_COLOR",))


def assert_cells_coloured(term: Terminal) -> None:
    assert len(term.chromatic_on_screen()) >= 6, f"only {sorted(term.chromatic_on_screen())} with a hue on screen"


def assert_stream_coloured(term: Terminal) -> None:
    assert sgr_chromatic(term.raw_stream()) >= 6, f"stream asks for {term.colour_depth()} but little hue"


def test_colour_run_is_coloured() -> None:
    with launch(False) as term:
        term.wait_for_text("shown of", 40)
        term.settle(0.5)
        assert_cells_coloured(term)
    assert ALT_SCREEN in term.raw_stream()  # the app took the alternate screen
    assert term.colour_depth() == "truecolor"
    assert_stream_coloured(term)


def test_no_color_run_has_no_hue_and_the_colour_check_fails_on_it() -> None:
    """Finding: under NO_COLOR the app still paints (Textual sends grey shades), so 'monochrome' means no hue, not no SGR."""
    with launch(True) as term:
        term.wait_for_text("shown of", 40)
        term.settle(0.5)
        assert not term.chromatic_on_screen(), sorted(term.chromatic_on_screen())
        with pytest.raises(AssertionError):
            assert_cells_coloured(term)
    with pytest.raises(AssertionError):
        assert_stream_coloured(term)
