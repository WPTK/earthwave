"""A terminal's late answer to Textual's own startup queries must not reach the key bindings (#270).

Textual asks `CSI ? 2026 $ p` and `CSI ? 2048 $ p` at startup; the answer is `CSI ? 2026 ; 2 $ y`. Textual's parser waits
100 ms for the rest of a sequence after an Escape: on a loaded machine (kitty under Xvfb in CI) a late tail comes out
as the keys `[ ? 2 0 2 6 ; 2 $ y`, and `?` opens Help with nobody at the keyboard. The failure itself was seen once in 21
runs and not reproduced; this pins the mechanism that can produce it.
"""

import pytest
from test_ui import week_app
from textual import events
from textual.keys import _character_to_key

from earthwave.ui.graphics.probe import ReplyFilter
from earthwave.ui.screens.help import HelpScreen

REPORT = "[?2026;2$y"


def run(filter_: ReplyFilter, text: str):
    released = []
    for ch in text:
        out, _ = filter_.push(("escape", "\x1b") if ch == "\x1b" else (ch, ch))
        released += out
    return released


@pytest.mark.parametrize("text", [REPORT, "\x1b" + REPORT, "^" + REPORT, "[?2048;0$y"])
def test_mode_reports_are_swallowed(text):
    assert run(ReplyFilter(), text) == []
    typed = run(ReplyFilter(hold_escape=False), text)
    assert [key for key, _ in typed] == (["escape"] if text.startswith("") else [])  # Escape is not held


def test_without_a_query_of_ours_outstanding_escape_is_typed_at_once():
    released = run(ReplyFilter(hold_escape=False), "\x1b")
    assert [key for key, _ in released] == ["escape"]


def test_a_dollar_that_is_not_a_report_is_given_back():
    flt = ReplyFilter()
    released = run(flt, "[5$x") + flt.flush()
    assert [c for _, c in released] == list("[5$x")


async def test_a_late_mode_report_does_not_open_help():
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for key, char in [
            ("escape", None),
            *((_character_to_key(c), c) for c in REPORT),
        ]:  # as the driver delivers a split reply
            app.post_message(events.Key(key, char))
        await pilot.pause(0.3)
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("question_mark")  # a real ? still does
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


@pytest.mark.parametrize("key", ["enter", "up", "f1", "tab"])
def test_keys_without_a_character_are_never_held(key):
    """They have no character, so they read as an empty string, which matches 'could still become a reply'."""
    assert ReplyFilter().push((key, "")) == ([(key, "")], None)
