"""Executable walkthrough (UAT standard item 4). Each test is a checklist item with its assertion as the evidence.

W1  reach every screen from the dashboard by keyboard only
W2  reach every screen from the dashboard by mouse only (footer labels and rows)
W3  use every control once (filters apply, search, sort, zoom, ack, refresh, history day, detail copy and open)
W4  go offline (the feed fails) and come back, and the screen says so both ways
W5  quit cleanly from every screen

The real-input equivalents on the release exe are in test_realinput.py (same items, real bytes).
"""

from __future__ import annotations

import httpx
import pytest
from test_ui import HOUR_BODY, mock_transport
from textual.widgets._footer import FooterKey

from earthwave.app import EarthwaveApp
from earthwave.config import Config, with_overrides
from earthwave.ui.widgets.quake_list import QuakeList
from uat import states
from uat.harness import screen_text, week_app

SIZE = (130, 32)
TARGETS = {"t": "TsunamiScreen", "o": "ObservationsScreen", "h": "HistoryScreen", "f": "FiltersScreen", "question_mark": "HelpScreen"}


def app_with_history():
    app = week_app(ticks=True, transport=mock_transport([]))
    app.history = states.FakeHistory(states.week_quakes())
    return app


async def test_w1_every_screen_by_keyboard_only():
    app = app_with_history()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        for key, name in TARGETS.items():
            await pilot.press(key)
            await pilot.pause(0.5)
            assert type(app.screen).__name__ == name, f"{key} did not reach {name}"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.screen is app.dashboard, f"escape from {name} did not return"
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "DetailScreen"
        await pilot.press("escape")
        await pilot.press("M")
        await pilot.pause(0.3)
        assert app.dashboard.has_class("map-full")
        await pilot.press("M")


async def test_w2_every_screen_by_mouse_only():
    app = app_with_history()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)

        async def click_footer(key: str):
            label = next(k for k in app.screen.query(FooterKey) if k.key == key)
            await pilot.click(offset=(label.region.x + 1, label.region.y))
            await pilot.pause(0.5)

        for key, name in {"t": "TsunamiScreen", "o": "ObservationsScreen", "h": "HistoryScreen", "f": "FiltersScreen", "question_mark": "HelpScreen"}.items():
            if name in ("FiltersScreen", "HelpScreen"):
                await click_footer(key)
                assert type(app.screen).__name__ == name
                # the dialogs have no footer: Cancel button for filters; help closes with Esc or ? only
                if name == "FiltersScreen":
                    await pilot.click("#cancel")
                    await pilot.pause(0.3)
                else:
                    app.pop_screen()  # NOTE: no mouse way out of help; see test_help_has_no_mouse_exit
                continue
            await click_footer(key)
            assert type(app.screen).__name__ == name
            back = next(k for k in app.screen.query(FooterKey) if k.key == "escape")
            await pilot.click(offset=(back.region.x + 1, back.region.y))
            await pilot.pause(0.4)
            assert app.screen is app.dashboard
        await pilot.click("#quakes", offset=(10, 2))
        assert app.dashboard.query_one(QuakeList).cursor == 2


async def test_help_has_no_mouse_exit():
    app = app_with_history()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        await pilot.press("question_mark")
        await pilot.pause(0.3)
        await pilot.click(offset=(1, 1))  # outside the box
        await pilot.pause(0.3)
        assert type(app.screen).__name__ == "HelpScreen"  # recorded: a mouse-only user needs the keyboard here


async def test_w3_use_every_control_once():
    app = app_with_history()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        before = app.sort
        await pilot.press("s")
        assert app.sort != before
        await pilot.press("slash")
        for ch in "Jap":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        rows = app.dashboard.query_one(QuakeList).rows
        assert rows and all("jap" in q.place.lower() for q in rows)
        assert 'Jap' in screen_text(app)  # the status bar names the search
        await pilot.press("slash", "escape")
        await pilot.press("z")
        assert app.dashboard.query_one("#map").model.mode.value != "world"
        await pilot.press("r")
        await pilot.pause(0.2)
        assert any("Offline" in str(n.message) or "Refresh" in str(n.message) for n in app._notifications)
        await pilot.press("f")
        await pilot.pause(0.3)
        app.screen.query_one("#min_mag").value = "6"
        await pilot.click("#apply")
        await pilot.pause(0.4)
        assert app.view.min_magnitude == 6.0 and all(q.mag >= 6 for q in app.dashboard.query_one(QuakeList).rows)
        await pilot.press("enter")
        await pilot.pause(0.4)
        await pilot.press("y")
        await pilot.pause(0.2)
        assert any("URL copied" in str(n.message) for n in app._notifications)
        await pilot.press("escape")
        await pilot.press("h")
        await pilot.pause(0.5)
        day = app.screen.day
        await pilot.press("left")
        assert app.screen.day < day


class Net:
    """A transport whose network can be turned off and on: off raises like a dead connection."""

    def __init__(self):
        self.up = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("network is down", request=request)
        if "summary" in request.url.path or "fdsnws" in request.url.path:
            return httpx.Response(200, content=HOUR_BODY)
        return httpx.Response(404)


async def test_w4_offline_and_back_says_so_both_ways():
    net = Net()
    config = with_overrides(Config(), 0.0, 24, None, "unicode")
    app = EarthwaveApp(config, run_scheduler=True, ticks=True, transport=httpx.MockTransport(net))
    async with app.run_test(size=SIZE) as pilot:
        async def status() -> str:
            await pilot.pause(0.6)
            return next((ln for ln in screen_text(app).splitlines() if " shown of " in ln), "")

        for _ in range(40):
            if app.store.warm and app.scheduler.freshness("usgs").value == "fresh":
                break
            await pilot.pause(0.25)
        up = await status()
        assert "usgs" not in up.split("failed")[0] if "failed" in up else True, up
        assert app.scheduler.freshness("usgs").value == "fresh"
        net.up = False
        for _ in range(40):
            app.scheduler.statuses["usgs"].next_due = 0.0  # refresh_now respects the 60 s minimum interval
            await pilot.pause(0.5)
            if app.scheduler.freshness("usgs").value != "fresh":
                break
        down = await status()
        assert "usgs" in down and ("failed" in down or "stale" in down), f"feed is down but the bar says: {down!r}"
        assert "feeds ok" not in down
        net.up = True
        for _ in range(60):
            app.scheduler.statuses["usgs"].next_due = 0.0  # refresh_now respects the 60 s minimum interval
            await pilot.pause(0.5)
            if app.scheduler.freshness("usgs").value == "fresh":
                break
        back = await status()
        assert app.scheduler.freshness("usgs").value == "fresh", back
        assert "usgs" not in back, f"feed recovered but the bar still names it: {back!r}"


@pytest.mark.parametrize("opener", ["", "enter", "t", "o", "h", "f", "question_mark"])
async def test_w5_quit_cleanly_from_every_screen(opener):
    app = app_with_history()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        if opener:
            await pilot.press(opener)
            await pilot.pause(0.4)
        screen = type(app.screen).__name__
        await pilot.press("q")
        await pilot.pause(0.5)
        # q quits on the dashboard and detail; on tsunami, observations it is Back; in the dialogs it is typed or ignored.
        quits = opener in ("", "enter", "h")
        assert (app._exit or not app.is_running) is quits, f"q on {screen}: exited={app._exit}"
