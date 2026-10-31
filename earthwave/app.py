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
from earthwave.models import Batch, Earthquake
from earthwave.net import build_client
from earthwave.notify.desktop import Notifier
from earthwave.notify.inapp import AttentionTracker
from earthwave.poll import Freshness, PollResult, Scheduler
from earthwave.sources.anomaly import threshold_m
from earthwave.sources.base import Source
from earthwave.sources.coops import CoopsSource
from earthwave.sources.jma import JmaSource
from earthwave.sources.ndbc import DartSource
from earthwave.sources.nws import NwsSource
from earthwave.sources.tsunamigov import TsunamiGovSource
from earthwave.sources.usgs import UsgsSource, feed_coverage
from earthwave.state.events import GaugeAnomaly, QuakeMajor, QuakeNew, SourceDegraded, SourceRecovered
from earthwave.state.gauges import GaugeSelection, choose_stations
from earthwave.state.history import History
from earthwave.state.rules import AlertRules, Notification
from earthwave.state.store import RETAIN, SortKey, Store, ViewFilter
from earthwave.ui.glyphs import resolve_glyphs
from earthwave.ui.screens.dashboard import DashboardScreen
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HelpScreen
from earthwave.ui.screens.history import HistoryScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.gauge_strip import GaugeRowData, GaugeStrip
from earthwave.ui.widgets.quake_list import QuakeList

log = logging.getLogger(__name__)

TICK_SECONDS = 0.5
TSUNAMI_FEEDS = (TsunamiGovSource.name, NwsSource.name, JmaSource.name)
SEND_TIMEOUT = 15.0  # seconds a desktop notifier gets before the next toast goes out
GAUGE_REFRESH_TICKS = 20  # the open gauge strip redraws every 10 s (ages and 'last batch' text move with the clock)


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
        notifier: Notifier | None = None,
        bell: bool = False,
        history: History | None = None,
        offline: bool = False,
    ):
        super().__init__()
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.store = store if store is not None else Store(config.alerts, config.home)
        self.tracker = AttentionTracker(self.clock)
        self.glyphs = resolve_glyphs(config.view.glyphs)
        self.view = ViewFilter(config.view.min_magnitude, config.view.hours)
        self.sort = SortKey.TIME
        self.usgs = UsgsSource(self.clock, config.poll.usgs_seconds, config.view.min_magnitude)
        self.client = build_client(config.notify.contact, transport)
        self.rules = AlertRules(config.alerts)
        self.notifier = notifier
        self.bell_enabled = bell
        self._outbox: asyncio.Queue[Notification] = asyncio.Queue()
        self._sender: asyncio.Task | None = None
        self.dart: DartSource | None = None
        self.coops: CoopsSource | None = None
        self.gauge_selection = GaugeSelection()
        self._gauge_key: tuple | None = None  # what the selection was computed from; recomputed only when it changes
        self._gauge_alert = False
        self._gauge_quake: Earthquake | None = None  # the flagged quake `o` was last pressed on
        self._last_anomaly: GaugeAnomaly | None = None
        self._ticks = 0
        self.scheduler = Scheduler(self._enabled_sources(config), self.store, self.clock, self._on_poll)
        self.offline = offline
        self.history = history
        self.scheduler_running = run_scheduler and not offline
        self.ticking = ticks
        self.dashboard = DashboardScreen()
        self._previous_visible: set[str] = set()
        self._coverage_checked = False

    def _enabled_sources(self, config: Config) -> list[Source]:
        poll, enabled = config.poll, config.sources
        return [
            *([self.usgs] if enabled.usgs else []),
            *([TsunamiGovSource(self.clock, poll.tsunami_seconds)] if enabled.tsunamigov else []),
            *([NwsSource(self.clock, poll.tsunami_seconds)] if enabled.nws else []),
            *([JmaSource(self.clock, poll.tsunami_seconds)] if enabled.jma else []),
            *self._gauge_sources(config),
        ]

    def _gauge_sources(self, config: Config) -> list[Source]:
        """DART and CO-OPS follow the tsunami switches: on when tsunami alerting is on and a feed can name a quake."""
        enabled = config.sources
        if not (config.alerts.tsunami and (enabled.usgs or enabled.tsunamigov or enabled.nws or enabled.jma)):
            return []
        self.dart = DartSource(self.clock, config.poll)
        self.coops = CoopsSource(self.clock, config.poll)
        self.store.predictions_for = self.coops.predictions
        return [self.dart, self.coops]

    # --- lifecycle ----------------------------------------------------------------------------------------------

    def on_mount(self) -> None:
        self._restore_history()
        self.push_screen(self.dashboard)
        if self.notifier is not None:
            self._sender = asyncio.get_running_loop().create_task(self._send_loop(self.notifier))
        if self.ticking:
            self.set_interval(TICK_SECONDS, self._tick)
        if self.scheduler_running:
            self._run_scheduler()
        elif self.offline:
            self.notify("Offline: showing saved history, no data is fetched", timeout=8)

    def _restore_history(self) -> None:
        """Refill the store from saved quakes, silently (no events, so no flash storm and no stale alert: only quakes
        are loaded), and bring back the highlight of quakes that were announced but never seen."""
        if self.history is None:
            return
        try:
            quakes, unseen = self.history.load_since(self.clock.now() - RETAIN)
        except Exception:
            log.exception("history could not be read; continuing without it")
            self.history = None
            return
        self.history.start()
        self.store.merge(Batch("history", self.clock.now(), quakes=tuple(quakes), backfill=True), self.clock)
        self.tracker.restore(unseen)

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

    async def _send_loop(self, notifier: Notifier) -> None:
        """Desktop notifications go out one at a time, in order, so a burst cannot pile up threads."""
        while True:
            note = await self._outbox.get()
            try:
                await asyncio.wait_for(notifier.send(note), SEND_TIMEOUT)
            except Exception as exc:  # a raising or hung notifier must not stall the toasts behind it
                log.warning("desktop notification %s dropped: %r", note.key, exc)
            finally:
                self._outbox.task_done()

    async def on_unmount(self) -> None:
        if self._sender is not None:
            self._sender.cancel()
        if self.history is not None:
            self.history.sync_seen(self.tracker.is_unseen)
            await self.history.close()
        await self.client.aclose()

    def _tick(self) -> None:
        """Half-second heartbeat: attention decay, seen tracking, blink phases and the status bar countdown."""
        self.tracker.prune()
        if self.history is not None:
            self.history.sync_seen(self.tracker.is_unseen)
        self.sync_tsunami()
        self.sync_gauges()
        self._ticks += 1
        if self._ticks % GAUGE_REFRESH_TICKS == 0:
            self.refresh_gauges()
        if not self.screen_stack or not self.dashboard.query(QuakeList):  # shutting down, or not yet composed
            return
        if self.screen is self.dashboard:
            quake_list = self.dashboard.query_one(QuakeList)
            visible = quake_list.visible_ids()
            self.tracker.mark_seen(visible & self._previous_visible)
            self._previous_visible = visible
            quake_list.refresh()
            self.dashboard.refresh_tsunami()
            self.dashboard.refresh_status()
        else:
            self._previous_visible = set()

    # --- polling ------------------------------------------------------------------------------------------------

    def _on_poll(self, result: PollResult) -> None:
        self.post_message(PollDone(result))

    def on_poll_done(self, message: PollDone) -> None:
        result = message.result
        self.tracker.observe(list(result.events))
        self._notify_desktop(list(result.events))
        self._record_history(result)
        for event in result.events:
            if isinstance(event, QuakeMajor):
                q = event.quake
                self.notify(f"M{q.mag:.1f} {q.place}", title="Major earthquake", severity="warning", timeout=10)
            elif isinstance(event, GaugeAnomaly):
                self._last_anomaly = event
                body = f"{event.name}: {event.residual_m:+.3f} m from the tide"
                self.notify(body, title="Gauge anomaly", severity="warning", timeout=10)
            elif isinstance(event, SourceDegraded | SourceRecovered):
                log.info("%s", event)
        if not self.usgs.starting and not self._coverage_checked:
            self._coverage_checked = True
            self._backfill_if_needed(force=False)
        self.sync_tsunami()
        self.sync_gauges()
        if not self.screen_stack:  # a poll that lands while the app is shutting down
            return
        if self.dashboard.is_mounted and self.dashboard.query(
            QuakeList
        ):  # not before it composed or after it unmounted
            self.dashboard.refresh_view()
        if isinstance(self.screen, TsunamiScreen):
            self.screen.refresh_alerts()
        self.refresh_gauges()

    def _record_history(self, result: PollResult) -> None:
        """Save what the store now holds for the batch's quakes (not the raw batch: an older revision never wins)."""
        if self.history is None:
            return
        batch = result.batch
        if batch is None or batch.not_modified or not (batch.quakes or batch.alerts):
            return
        new_ids = {e.quake.id for e in result.events if isinstance(e, QuakeNew)}
        current = (q for b in batch.quakes if (q := self.store.get(b.id)) is not None)
        self.history.record(current, new_ids, batch.alerts, batch.fetched_at)

    def tsunami_feeds_down(self) -> tuple[list[tuple[str, Freshness]], bool]:
        """(tsunami feeds late or failing, whether no tsunami feed is fresh). A feed not polled yet is not an outage,
        but until one feed has answered, an empty alert list says nothing."""
        if not self.scheduler_running:
            return [], False
        names = [n for n in TSUNAMI_FEEDS if n in self.scheduler.statuses]
        state = [(n, self.scheduler.freshness(n)) for n in names]
        down = [(n, f) for n, f in state if f in (Freshness.STALE, Freshness.ERROR)]
        return down, bool(names) and all(f is not Freshness.FRESH for _, f in state)

    def sync_tsunami(self) -> None:
        """Forget tsunami attention for alerts that expired or were closed since the last look."""
        active = self.store.active_alerts(self.clock, actionable_only=True)
        self.tracker.tsunami_sync({a.id for a in active})

    # --- gauges -------------------------------------------------------------------------------------------------

    def sync_gauges(self) -> None:
        """Tell the gauge sources the alert cadence and which stations to poll, from the alerts in force.

        The cadence hint follows Warning/Advisory/Watch alerts on the injected clock. The selection (nearest DART
        buoys and tide gauges to every alert in force, plus the quake `o` was pressed on) is recomputed only when the
        alerts, that quake or the usable station list changed, and the new stations are polled right away.
        """
        if self.dart is None or self.coops is None:
            return
        hint = self.store.gauge_alert_active(self.clock)
        if hint != self._gauge_alert:
            self._gauge_alert = hint
            for source in (self.dart, self.coops):
                source.set_alert_active(hint)
                self.scheduler.reschedule(source.name)
        key = (
            tuple(
                sorted(
                    (a.id, a.origin, a.geometry, a.quake_ref, self._linked_position(a))
                    for a in self.store.active_alerts(self.clock)
                )
            ),
            self._gauge_quake.id if self._gauge_quake else None,
            self.dart.unavailable,
            len(self.dart.stations),
            len(self.coops.stations),
        )
        if key == self._gauge_key:
            return
        self._gauge_key = key
        chosen = choose_stations(
            self.store, self.clock, self.dart.stations, self.coops.stations, self.dart.unavailable, self._gauge_quake
        )
        old, self.gauge_selection = self.gauge_selection, chosen
        if chosen.dart_ids != old.dart_ids:
            self.dart.set_selection(chosen.dart_ids)
            self.scheduler.reschedule(self.dart.name, immediate=True)
        if chosen.coops_ids != old.coops_ids:
            self.coops.set_selection(chosen.coops_ids)
            self.scheduler.reschedule(self.coops.name, immediate=True)

    def _linked_position(self, alert) -> tuple[float, float] | None:
        """Where the linked quake is now; it can move when a later USGS revision relocates it."""
        quake = self.store.quake_for(alert)
        return (quake.lat, quake.lon) if quake else None

    def gauge_rows(self) -> list[GaugeRowData]:
        """One strip row per selected station, DART first; a station with no readings yet shows 'no data'."""
        rows = []
        for km, station in (*self.gauge_selection.dart, *self.gauge_selection.coops):
            readings = self.store.gauge_readings(station.id)
            newest = readings[-1] if readings else None
            rows.append(
                GaugeRowData(
                    station_id=station.id,
                    name=station.name.strip(),
                    distance_km=km,
                    mode=newest.mode if newest else "",
                    newest=newest.ts if newest else None,
                    residuals=tuple(r.residual_m for r in self.store.gauge_residuals(station.id)),
                    threshold_m=threshold_m(station.kind, self.config.alerts),
                )
            )
        return rows

    def refresh_gauges(self) -> None:
        screen = self.screen if self.screen_stack else None
        if isinstance(screen, TsunamiScreen | ObservationsScreen) and screen.query(GaugeStrip):  # composed
            screen.refresh_gauges()

    def gauge_ticker(self) -> str:
        """Ticker text for an anomaly still in progress (not acknowledged, under five minutes old)."""
        last = self._last_anomaly
        if last is None or last.station_id not in self.tracker.gauge_active():
            return ""
        return f"{last.name} {last.residual_m:+.2f} m"

    def _notify_desktop(self, events: list) -> None:
        """Evaluate the alert rules and hand each notification to the desktop notifier and the terminal bell."""
        for note in self.rules.evaluate(events):
            if self.bell_enabled and note.bell:
                self.bell()
            if self.notifier is not None:
                self._outbox.put_nowait(note)

    # --- view changes -------------------------------------------------------------------------------------------

    def _backfill_if_needed(self, force: bool) -> None:
        if not self.config.sources.usgs or self.offline:
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

    def open_tsunami(self) -> None:
        if not isinstance(self.screen, TsunamiScreen):
            self.push_screen(TsunamiScreen())

    def refresh_tsunami(self) -> None:
        self.sync_tsunami()
        self.dashboard.refresh_tsunami()
        self.dashboard.refresh_status()
        if isinstance(self.screen, TsunamiScreen):
            self.screen.refresh_alerts()

    def open_observations(self, quake: Earthquake) -> None:
        """`o`: select the stations nearest a USGS-flagged quake and show their gauges."""
        new = self._gauge_quake is None or quake.id != self._gauge_quake.id
        if self.dart is not None and quake.tsunami_flag and new:
            self._gauge_quake = quake
            self.sync_gauges()
        if not isinstance(self.screen, ObservationsScreen):
            self.push_screen(ObservationsScreen(quake))

    def open_history(self) -> None:
        if self.history is None:
            self.notify("No saved history is available", severity="warning", timeout=4)
        elif not isinstance(self.screen, HistoryScreen):
            self.push_screen(HistoryScreen())

    def open_detail(self, quake: Earthquake) -> None:
        self.push_screen(DetailScreen(quake))
