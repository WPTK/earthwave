"""The Ctrl+P command palette: Textual's own palette, with earthwave's actions added as a provider."""

from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from typing import TYPE_CHECKING, NamedTuple, cast

from textual.command import DiscoveryHit, Hit, Hits, Provider

from earthwave.state.store import SortKey

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp


class Entry(NamedTuple):
    name: str
    help: str
    run: Callable[[], Awaitable[None] | None]


class EarthwaveCommands(Provider):
    """Every screen action by name. Listed as soon as the palette opens; typing narrows it."""

    def entries(self) -> Iterator[Entry]:
        app = cast("EarthwaveApp", self.app)
        go = app.go_screen
        yield Entry("Go to quake list", "The main screen", lambda: go("dashboard"))
        yield Entry("Go to tsunami alerts", "Alerts in force (t)", lambda: go("tsunami"))
        yield Entry("Go to tide gauges", "Stations near the selected quake (o)", lambda: go("gauges"))
        yield Entry("Go to history", "Saved quakes by day (h)", lambda: go("history"))
        yield Entry("Go to help", "Keys, colours and feeds (?)", lambda: go("help"))
        yield Entry("Open quake detail", "The selected quake (Enter)", lambda: go("detail"))
        yield Entry("Refresh now", "Poll every feed again (r)", lambda: go("refresh"))
        yield Entry("Acknowledge all", "Clear the alerts and new-row marks (a)", lambda: go("ack"))
        for key in SortKey:
            if key is SortKey.DIST and not app.config.home.enabled:
                continue
            label = {SortKey.TIME: "time", SortKey.MAG: "magnitude", SortKey.DIST: "distance from home"}[key]
            yield Entry(f"Sort by {label}", "The order of the quake list (s cycles)", partial(app.set_sort, key))
        yield Entry("Filters", "Minimum magnitude and hours back (f)", lambda: go("filters"))
        for mag in (2.5, 4.5, 6.0):
            yield Entry(
                f"Show M{mag:g} and up", "Keeps the hours back", partial(app.apply_filters, mag, app.view.hours)
            )
        yield Entry("Search places", "Filter the list by place name (/)", lambda: go("search"))
        yield Entry("Clear search", "Show every place again", lambda: app.set_query(""))
        yield Entry("Show or hide the map", "The map panel (m)", lambda: go("map"))
        yield Entry("Full-screen map", "Esc comes back (M)", lambda: go("fullmap"))
        yield Entry("Zoom the map", "Cycle the zoom level (z)", lambda: go("zoom"))
        yield Entry("Set home location", "LAT,LON and an optional name; saved to the config file", app.open_set_home)
        yield Entry("Quit", "Close earthwave (q)", app.action_quit)

    async def discover(self) -> Hits:
        for entry in self.entries():
            yield DiscoveryHit(entry.name, entry.run, help=entry.help)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for entry in self.entries():
            score = matcher.match(entry.name)
            if score > 0:
                yield Hit(score, matcher.highlight(entry.name), entry.run, help=entry.help)
