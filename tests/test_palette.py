"""The Ctrl+P command palette (#322): every screen action by name, and the in-app way to set the home (#251)."""

import pytest
from conftest import wait_until
from test_ui import week_app
from textual.command import CommandInput, CommandList, CommandPalette
from textual.widgets import Input, Label

from earthwave.config import HomeConfig, load_config
from earthwave.state.history import History
from earthwave.state.store import SortKey
from earthwave.ui.commands import EarthwaveCommands
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HELP_TEXT, HelpScreen
from earthwave.ui.screens.history import HistoryScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.sethome import SetHomeScreen, parse_home_fields
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.quake_list import QuakeList

SIZE = (120, 40)


async def ready(pilot) -> None:
    app = pilot.app
    await wait_until(pilot, lambda: app.screen is app.dashboard and bool(app.dashboard.query(QuakeList)), what="list")


async def run_command(pilot, text: str) -> None:
    """Open the palette, type `text`, pick the first hit and wait for the palette to go."""
    app = pilot.app
    await pilot.press("ctrl+p")
    await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette), what="the palette")
    app.screen.query_one(CommandInput).value = text
    await wait_until(pilot, lambda: app.screen.query_one(CommandList).option_count > 0, what="a hit")
    for _ in range(100):
        if not isinstance(app.screen, CommandPalette):
            break
        await pilot.press("enter")
        await pilot.pause(0.02)
    await wait_until(pilot, lambda: not isinstance(app.screen, CommandPalette), what="the palette to close")


def palette_app(tmp_path, home=True, config_text="# my notes\n[view]\nmin_magnitude = 4.5\n"):
    path = tmp_path / "config.toml"
    if config_text is not None:
        path.write_text(config_text, encoding="utf-8")
    return week_app(home=home, config_path=path), path


def notices(app) -> str:
    return " | ".join(f"{n.title} {n.message}" for n in app._notifications)


async def test_ctrl_p_opens_the_palette_and_lists_the_commands():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+p")
        await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette), what="the palette")
        await wait_until(pilot, lambda: app.screen.query_one(CommandList).option_count > 0, what="the list")
        names = {e.name for e in EarthwaveCommands(app.screen).entries()}
        assert {"Go to tsunami alerts", "Sort by magnitude", "Set home location", "Quit", "Refresh now"} <= names
        await pilot.press("escape")
        await wait_until(pilot, lambda: app.screen is app.dashboard, what="Esc to close the palette")


async def test_there_is_one_palette_and_help_lists_it():
    assert "Ctrl+P" in HELP_TEXT
    assert EarthwaveCommands in week_app().COMMANDS
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        assert sum(1 for b in app.active_bindings.values() if b.binding.action == "command_palette") == 1


@pytest.mark.parametrize(
    ("text", "screen"),
    [
        ("Go to tsunami alerts", TsunamiScreen),
        ("Go to help", HelpScreen),
        ("Filters", FiltersScreen),
        ("Open quake detail", DetailScreen),
        ("Go to tide gauges", ObservationsScreen),
        ("Set home location", SetHomeScreen),
    ],
)
async def test_screen_commands_open_their_screen(text, screen):
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, text)
        await wait_until(pilot, lambda: isinstance(app.screen, screen), what=text)


async def test_history_command_says_when_there_is_none_and_opens_it_when_there_is(tmp_path, clock):
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Go to history")
        await wait_until(pilot, lambda: "history" in notices(app).lower(), what="the no-history notice")
        assert app.screen is app.dashboard
    history = History(tmp_path / "h.sqlite", 90, clock)
    history.open()
    app = week_app(history=history)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Go to history")
        await wait_until(pilot, lambda: isinstance(app.screen, HistoryScreen), what="history")


async def test_a_command_goes_back_to_the_quake_list_first():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("question_mark")
        await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
        await run_command(pilot, "Go to tsunami alerts")
        await wait_until(pilot, lambda: isinstance(app.screen, TsunamiScreen), what="tsunami")
        await pilot.press("escape")
        await wait_until(pilot, lambda: app.screen is app.dashboard, what="Esc back to the list, not to Help")


async def test_sort_commands():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        for text, key in (
            ("Sort by magnitude", SortKey.MAG),
            ("Sort by distance from home", SortKey.DIST),
            ("Sort by time", SortKey.TIME),
        ):
            await run_command(pilot, text)
            await wait_until(pilot, lambda key=key: app.sort is key, what=text)
    app = week_app(home=False)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        assert "Sort by distance from home" not in {e.name for e in EarthwaveCommands(app.screen).entries()}


async def test_filter_commands():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Show M6 and up")
        await wait_until(pilot, lambda: app.view.min_magnitude == 6.0, what="M6")
        assert app.view.hours == 168
        app.set_query("alaska")
        await run_command(pilot, "Clear search")
        await wait_until(pilot, lambda: app.view.query == "", what="the search to clear")


async def test_map_commands():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        shown = app.dashboard.show_map
        await run_command(pilot, "Show or hide the map")
        await wait_until(pilot, lambda: app.dashboard.show_map is not shown, what="the map to toggle")
        await run_command(pilot, "Show or hide the map")
        await wait_until(pilot, lambda: app.dashboard.show_map is shown)
        await run_command(pilot, "Full-screen map")
        await wait_until(pilot, lambda: app.dashboard.has_class("map-full"), what="full screen")
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.dashboard.has_class("map-full"), what="Esc to leave full screen")


async def test_refresh_and_acknowledge_commands():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Refresh now")
        await wait_until(pilot, lambda: "Offline" in notices(app), what="the refresh notice")
        called = []
        app.tracker.ack_all = lambda: called.append(1)  # type: ignore[method-assign]
        await run_command(pilot, "Acknowledge all")
        await wait_until(pilot, lambda: called, what="ack")


async def test_quit_command_quits():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Quit")
        await wait_until(pilot, lambda: not app.is_running, what="the app to quit")


async def test_q_and_esc_keep_their_meaning_with_the_palette():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+p")
        await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette))
        await pilot.press("q")  # a letter in the palette's box, not a quit
        assert app.is_running and app.screen.query_one(CommandInput).value == "q"
        await pilot.press("escape")
        await wait_until(pilot, lambda: app.screen is app.dashboard)
        await pilot.press("q")
        await wait_until(pilot, lambda: not app.is_running, what="q to quit")


# --- set home -------------------------------------------------------------------------------------------------


async def fill_home(pilot, location: str, name: str = "") -> None:
    app = pilot.app
    await run_command(pilot, "Set home location")
    await wait_until(pilot, lambda: isinstance(app.screen, SetHomeScreen), what="the dialog")
    app.screen.query_one("#location", Input).value = location
    app.screen.query_one("#home_name", Input).value = name
    await pilot.click("#apply")


async def test_set_home_writes_the_config_and_keeps_the_rest(tmp_path):
    app, path = palette_app(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await fill_home(pilot, "-33.87, 151.21", "Sydney")
        await wait_until(pilot, lambda: "Home saved" in notices(app), what="the saved notice")
        assert app.screen is app.dashboard
    assert path.read_text(encoding="utf-8").startswith("# my notes\n[view]\nmin_magnitude = 4.5\n")
    home = load_config(path).home
    assert (home.name, home.lat, home.lon) == ("Sydney", -33.87, 151.21)
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]  # no temp file left behind


async def test_set_home_goes_through_save_home(tmp_path, monkeypatch):
    app, path = palette_app(tmp_path)
    calls = []
    monkeypatch.setattr("earthwave.app.save_home", lambda *args: calls.append(args))
    monkeypatch.setattr("earthwave.app.load_config", lambda _path: None)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await fill_home(pilot, "51.5,-0.12", "")
        await wait_until(pilot, lambda: calls, what="save_home")
    assert calls == [(path, "home", 51.5, -0.12)]


@pytest.mark.parametrize(
    ("location", "name", "field"),
    [
        ("", "", "Location"),
        ("40.7", "", "Location"),
        ("abc,def", "", "Location"),
        ("95,10", "", "Location"),
        ("1,200", "", "Location"),
        ("40,-74", "x" * 61, "Name"),
        ("40,-74", "bad\x00name", "Name"),
    ],
)
async def test_bad_input_is_refused_naming_the_field_and_creates_nothing(tmp_path, location, name, field):
    app, _ = palette_app(tmp_path, config_text=None)  # no config file: nothing may create the first-run template
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await fill_home(pilot, location, name)

        def shown() -> str:
            return str(app.screen.query_one("#error", Label).render())

        await wait_until(pilot, lambda: shown().startswith(field), what=f"an error naming {field}")
        assert isinstance(app.screen, SetHomeScreen)
    assert list(tmp_path.iterdir()) == []


def test_name_defaults_to_the_configured_one():
    assert parse_home_fields("1,2", "", HomeConfig(name="Cabin", lat=5, lon=5)) == (1.0, 2.0, "Cabin")
    assert parse_home_fields("1,2", " ", HomeConfig()) == (1.0, 2.0, "home")


async def test_a_missing_config_file_is_reported_not_created(tmp_path):
    app, path = palette_app(tmp_path, config_text=None)
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await fill_home(pilot, "40,-74", "NYC")
        await wait_until(pilot, lambda: "Home not saved" in notices(app), what="the failure notice")
    assert not path.exists()


async def test_no_config_in_use_is_reported():
    app = week_app()
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await fill_home(pilot, "40,-74", "NYC")
        await wait_until(pilot, lambda: "not saved" in notices(app), what="the notice")


async def test_cancel_leaves_the_config_alone(tmp_path):
    app, path = palette_app(tmp_path)
    before = path.read_text(encoding="utf-8")
    async with app.run_test(size=SIZE) as pilot:
        await ready(pilot)
        await run_command(pilot, "Set home location")
        await wait_until(pilot, lambda: isinstance(app.screen, SetHomeScreen))
        await pilot.press("escape")
        await wait_until(pilot, lambda: app.screen is app.dashboard)
    assert path.read_text(encoding="utf-8") == before
