"""Every screen at the three reference sizes (#326): one driver that opens a screen on a fixed-clock app and hands back
what is drawn, so the size tests and the SVG snapshots share their set-up."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import httpx
from conftest import footer_keys, wait_until
from test_observations import STATION, dart_rows, gauge_app
from test_tsunami import kam_app
from test_ui import DETAIL_BODY, week_app
from textual.command import CommandInput, CommandList, CommandPalette
from textual.geometry import Region

from earthwave.app import EarthwaveApp
from earthwave.clock import ReplayClock
from earthwave.models import Batch
from earthwave.state.gauges import GaugeSelection
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HelpScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.sethome import SetHomeScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.quake_list import QuakeList

SIZES = [(80, 24), (100, 30), (120, 40), (200, 60)]
MIN_SIZE = (100, 30)  # below it every screen shows the too-small notice instead


def utc(app: EarthwaveApp) -> EarthwaveApp:
    """Times print in the machine's zone otherwise, and a snapshot must not depend on the machine."""
    app.config = replace(app.config, view=replace(app.config.view, time_zone="UTC"))
    return app


def gauge_flagged() -> EarthwaveApp:
    """A tsunami-flagged M7.5 with 120 DART rows: `o` has stations to show."""
    clock = ReplayClock(datetime(2026, 10, 31, 6, 20, tzinfo=UTC), real=lambda: 0.0)
    app = gauge_app(clock)
    app.store.merge(Batch("dart", clock.now(), readings=tuple(dart_rows(120)), stations=(STATION,)), clock)
    return utc(app)


def detail_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, content=DETAIL_BODY))


def screen_text(app: EarthwaveApp) -> str:
    """What the current screen draws, one line per row (cells, not characters: wide glyphs keep their width)."""
    return "\n".join(strip.text.rstrip() for strip in app.screen._compositor.render_strips())


async def _dashboard_ready(pilot) -> None:
    app = pilot.app
    await wait_until(pilot, lambda: app.screen is app.dashboard and bool(app.dashboard.query(QuakeList)), what="list")


async def _open_key(pilot, key: str, screen_type: type) -> None:
    await _dashboard_ready(pilot)
    await pilot.press(key)
    await wait_until(pilot, lambda: isinstance(pilot.app.screen, screen_type), what=screen_type.__name__)
    await pilot.pause()


async def _dashboard(pilot) -> None:
    await _dashboard_ready(pilot)


async def _detail(pilot) -> None:
    app = pilot.app
    await _dashboard_ready(pilot)
    await pilot.press("enter")
    await wait_until(
        pilot, lambda: isinstance(app.screen, DetailScreen) and app.screen.detail is not None, 5.0, "detail"
    )
    await pilot.pause()


async def _tsunami(pilot) -> None:
    await _open_key(pilot, "t", TsunamiScreen)


async def _gauges(pilot) -> None:
    await _open_key(pilot, "o", ObservationsScreen)
    pilot.app.gauge_selection = GaugeSelection(dart=((1850.0, STATION),))  # the station the fixture rows belong to
    pilot.app.screen.refresh_gauges()
    await pilot.pause()


async def _help(pilot) -> None:
    await _open_key(pilot, "question_mark", HelpScreen)


async def _filters(pilot) -> None:
    await _open_key(pilot, "f", FiltersScreen)


async def _palette(pilot) -> None:
    await _dashboard_ready(pilot)
    await pilot.press("ctrl+p")
    await wait_until(pilot, lambda: isinstance(pilot.app.screen, CommandPalette), what="the palette")
    listing = pilot.app.screen.query_one(CommandList)
    await wait_until(pilot, lambda: listing.option_count > 0, what="the command list")
    await pilot.pause()


async def _sethome(pilot) -> None:
    app = pilot.app
    await _palette(pilot)
    app.screen.query_one(CommandInput).value = "Set home location"
    listing = app.screen.query_one(CommandList)
    await wait_until(pilot, lambda: listing.highlighted is not None, what="a highlighted hit")
    await pilot.press("enter")
    await wait_until(pilot, lambda: isinstance(app.screen, SetHomeScreen), what="the dialog")
    await pilot.pause()


async def _map(pilot) -> None:
    await _dashboard_ready(pilot)
    await pilot.press("M")
    await wait_until(pilot, lambda: pilot.app.dashboard.has_class("map-full"), what="the full map")
    await pilot.pause()


async def _offline(pilot) -> None:
    """Every feed failing under a Warning: the worst case for the status bar and the banner."""
    app = pilot.app
    await _dashboard_ready(pilot)
    app.scheduler_running = True  # judge the feeds without polling: the health is set here
    for status in app.scheduler.statuses.values():
        status.health.failures = 2
    app.dashboard.refresh_status()
    await wait_until(pilot, lambda: "OFFLINE" in str(app.dashboard.query_one("#status").render()), what="OFFLINE")
    await pilot.pause()


@dataclass(frozen=True)
class Scenario:
    name: str
    build: Callable[[], EarthwaveApp]
    open: Callable[..., Awaitable[None]]
    footer: bool = True  # the screen has a footer, which composes a few frames late


def _kam_dashboard() -> EarthwaveApp:
    return kam_app()


SCENARIOS = {
    s.name: s
    for s in (
        Scenario("dashboard", lambda: utc(week_app()), _dashboard),
        Scenario("dashboard-warning", _kam_dashboard, _dashboard),
        Scenario("dashboard-offline", _kam_dashboard, _offline),
        Scenario("detail", lambda: utc(week_app(transport=detail_transport())), _detail),
        Scenario("tsunami", kam_app, _tsunami),
        Scenario("gauges", gauge_flagged, _gauges),
        Scenario("help", lambda: utc(week_app()), _help, footer=False),
        Scenario("filters", lambda: utc(week_app()), _filters, footer=False),
        Scenario("sethome", lambda: utc(week_app()), _sethome, footer=False),
        Scenario("palette", lambda: utc(week_app()), _palette, footer=False),
        Scenario("map", lambda: utc(week_app()), _map),
    )
}


@dataclass(frozen=True)
class Shot:
    text: str
    regions: dict[str, Region]  # the widgets the size tests measure, by selector, where they were drawn
    backgrounds: dict[str, object]


MEASURED = ("#dialog", "#help_box", "#banner", "#status")


async def render(scenario: Scenario, size: tuple[int, int]) -> Shot:
    """The screen at `size`. Below the minimum size the screen is opened at 120x40 and then shrunk, the way a user
    meets it: the keys that open a screen are ignored on a too-small terminal."""
    app = scenario.build()
    open_size = size if size >= MIN_SIZE else (120, 40)
    async with app.run_test(size=open_size) as pilot:
        await scenario.open(pilot)
        if scenario.footer:
            await footer_keys(pilot)
        if open_size != size:
            await pilot.resize_terminal(*size)
            await pilot.pause()
        found = {sel: app.screen.query(sel).first() for sel in MEASURED if app.screen.query(sel)}
        return Shot(
            screen_text(app),
            {sel: widget.region for sel, widget in found.items()},
            {sel: widget.styles.background for sel, widget in found.items()},
        )
