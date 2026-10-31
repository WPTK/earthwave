"""The Textual application: owns the store, scheduler and attention tracker and wires them to the screens."""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta

import httpx
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.message import Message

from earthwave.clock import Clock, SystemClock
from earthwave.config import Config
from earthwave.models import Earthquake
from earthwave.net import build_client
from earthwave.notify.inapp import AttentionTracker
from earthwave.poll import PollResult, Scheduler
from earthwave.sources.usgs import UsgsSource, feed_coverage
from earthwave.state.events import QuakeMajor, SourceDegraded, SourceRecovered
from earthwave.state.store import SortKey, Store, ViewFilter
from earthwave.ui.glyphs import resolve_glyphs
from earthwave.ui.screens.dashboard import DashboardScreen
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HelpScreen
from earthwave.ui.widgets.quake_list import QuakeList

log = logging.getLogger(__name__)

TICK_SECONDS = 0.5


class PollDone(Message):
    """A source finished a poll (or a backfill); the view is recomputed from the store, never from the message."""

    def __init__(self, result: PollResult):
        super().__init__()
        self.result = result


class EarthwaveApp(App):
    CSS_PATH = "ui/app.tcss"
    TITLE = "earthwave"
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        clock: Clock | None = None,
        run_scheduler: bool = True,
        ticks: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__()
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.store = store or Store(config.alerts, config.home)
        self.tracker = AttentionTracker(self.clock)
        self.glyphs = resolve_glyphs(config.view.glyphs)
        self.view = ViewFilter(config.view.min_magnitude, config.view.hours)
        self.sort = SortKey.TIME
        self.usgs = UsgsSource(self.clock, config.poll.usgs_seconds, config.view.min_magnitude)
        self.client = build_client(config.notify.contact, transport)
        self.scheduler = Scheduler([self.usgs] if config.sources.usgs else [], self.store, self.clock, self._on_poll)
        self.scheduler_running = run_scheduler
        self.ticking = ticks
        self.dashboard = DashboardScreen()
        self._previous_visible: set[str] = set()
        self._coverage_checked = False

    # --- lifecycle ----------------------------------------------------------------------------------------------

    def on_mount(self) -> None:
        self.push_screen(self.dashboard)
        if self.ticking:
            self.set_interval(TICK_SECONDS, self._tick)
        if self.scheduler_running:
            self._run_scheduler()

    @work(exclusive=True, exit_on_error=False)
    async def _run_scheduler(self) -> None:
        try:
            await self.scheduler.run(self.client)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler stopped unexpectedly")
            raise
        finally:
            await self.client.aclose()

    async def on_unmount(self) -> None:
        await self.client.aclose()

    def _tick(self) -> None:
        """Half-second heartbeat: attention decay, seen tracking, blink phases and the status bar countdown."""
        self.tracker.prune()
        if self.screen is self.dashboard:
            quake_list = self.dashboard.query_one(QuakeList)
            visible = quake_list.visible_ids()
            self.tracker.mark_seen(visible & self._previous_visible)
            self._previous_visible = visible
            quake_list.refresh()
            self.dashboard.refresh_status()
        else:
            self._previous_visible = set()

    # --- polling ------------------------------------------------------------------------------------------------

    def _on_poll(self, result: PollResult) -> None:
        self.post_message(PollDone(result))

    def on_poll_done(self, message: PollDone) -> None:
        result = message.result
        self.tracker.observe(list(result.events))
        for event in result.events:
            if isinstance(event, QuakeMajor):
                q = event.quake
                self.notify(f"M{q.mag:.1f} {q.place}", title="Major earthquake", severity="warning", timeout=10)
            elif isinstance(event, SourceDegraded | SourceRecovered):
                log.info("%s", event)
        if not self.usgs.starting and not self._coverage_checked:
            self._coverage_checked = True
            self._backfill_if_needed(force=False)
        self.dashboard.refresh_view()

    # --- view changes -------------------------------------------------------------------------------------------

    def _backfill_if_needed(self, force: bool) -> None:
        if not self.config.sources.usgs:
            return
        if force or not self.store.is_covered(self.view.hours, self.view.min_magnitude):
            end = self.clock.now()
            start = end - timedelta(hours=self.view.hours)
            min_mag = self.view.min_magnitude
            self.scheduler.submit_backfill("usgs", self.client, lambda c: self.usgs.fetch_range(c, start, end, min_mag))

    def apply_filters(self, min_magnitude: float, hours: float) -> None:
        """New filters: bump the generation, switch the steady feed if needed and backfill what the feeds miss."""
        old_feed = self.usgs.steady
        self.view = replace(self.view, min_magnitude=min_magnitude, hours=hours)
        self.usgs.set_min_magnitude(min_magnitude)
        self.scheduler.switch()
        if self.usgs.steady != old_feed:
            floor = feed_coverage(self.usgs.steady)
            if floor:
                self.store.drop_coverage_below(floor[1])
        self._backfill_if_needed(force=self.usgs.steady != old_feed)
        self.dashboard.refresh_view()

    def set_query(self, query: str) -> None:
        self.view = replace(self.view, query=query.strip())
        self.dashboard.refresh_view()

    # --- screens ------------------------------------------------------------------------------------------------

    def open_filters(self) -> None:
        def done(result: tuple[float, float] | None) -> None:
            if result is not None:
                self.apply_filters(*result)

        self.push_screen(FiltersScreen(self.view), done)

    def open_help(self) -> None:
        self.push_screen(HelpScreen())

    def open_detail(self, quake: Earthquake) -> None:
        self.push_screen(DetailScreen(quake))
