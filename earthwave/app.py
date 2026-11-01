"""The Textual application: owns the store, scheduler and attention tracker and wires them to the screens."""

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import httpx
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.filter import Monochrome
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
from earthwave.state.events import (
    GaugeAnomaly,
    QuakeMajor,
    QuakeNew,
    QuakeRemoved,
    QuakeRenamed,
    SourceDegraded,
    SourceRecovered,
)
from earthwave.state.gauges import GaugeSelection, choose_stations
from earthwave.state.history import History
from earthwave.state.rules import AlertRules, Notification, quake_line
from earthwave.state.store import RETAIN, SortKey, Store, ViewFilter
from earthwave.ui.glyphs import resolve_glyphs
from earthwave.ui.graphics.probe import BRAILLE, GraphicsChoice
from earthwave.ui.labels import fmt_age, fmt_time
from earthwave.ui.screens.dashboard import DashboardScreen
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HelpScreen
from earthwave.ui.screens.history import HistoryScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.theme import LegibleMonochrome
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
        history_error: str | None = None,
        graphics: GraphicsChoice | None = None,
    ):
        super().__init__()
        self._filters = [LegibleMonochrome() if isinstance(f, Monochrome) else f for f in self._filters]
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.store = store if store is not None else Store(config.alerts, config.home, launched=self.clock.now())
        self.tracker = AttentionTracker(self.clock)
        self.glyphs = resolve_glyphs(config.view.glyphs)
        self.graphics = graphics or BRAILLE
        self._overlays: list[Callable[[Callable[[str], None]], None]] = []
        self.view = ViewFilter(config.view.min_magnitude, config.view.hours)
        self.sort = SortKey.TIME
        # The steady feed must hold every quake that can alert, whatever magnitude the display filter hides.
        alert_floor = min(
            config.alerts.major_magnitude, config.alerts.near_home_magnitude if config.home.enabled else math.inf
        )
        self.usgs = UsgsSource(self.clock, config.poll.usgs_seconds, config.view.min_magnitude, alert_floor=alert_floor)
        self.client = build_client(config.notify.contact, transport, self.clock)
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
        self.history_error = history_error  # why there is no history, when it was wanted
        self.scheduler_running = run_scheduler and not offline
        self.ticking = ticks
        self.dashboard = DashboardScreen()
        self._previous_visible: set[str] = set()
        self._coverage_checked = False
        self._backfill: asyncio.Task | None = None

    def add_overlay(self, emit: Callable[[Callable[[str], None]], None]) -> None:
        """Register a widget that draws outside Textual's cell grid (the sixel map); it is called with the driver's
        write function at the end of every screen update."""
        self._overlays.append(emit)

    def remove_overlay(self, emit: Callable[[Callable[[str], None]], None]) -> None:
        if emit in self._overlays:
            self._overlays.remove(emit)

    def _end_update(self) -> None:
        """Textual brackets each screen update in a synchronized update; overlays draw inside it, so the cells and the
        image they sit on appear together."""
        if self._driver is not None:
            for emit in tuple(self._overlays):
                emit(self._driver.write)
        super()._end_update()

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
        if self.history is None and self.history_error:
            self.notify(
                f"History unavailable, nothing will be saved: {self.history_error}",
                severity="warning",
                timeout=10,
                markup=False,
            )
        if self.offline:
            if self.history is None:
                self.notify("Offline, and there is no saved history to show", severity="warning", timeout=10)
            else:
                self.notify("Offline: showing saved history, no data is fetched", timeout=8)

    def _restore_history(self) -> None:
        """Refill the store from saved quakes, silently (no events, so no flash storm and no stale alert: only quakes
        are loaded), and bring back the highlight of quakes that were announced but never seen."""
        if self.history is None:
            return
        try:
            quakes, unseen = self.history.load_since(self.clock.now() - RETAIN)
        except Exception as exc:
            log.exception("history could not be read; continuing without it")
            self.history.close_now()
            self.history, self.history_error = None, f"{type(exc).__name__}: {exc}"
            return
        self.history.start()
        events = self.store.merge(Batch("history", self.clock.now(), quakes=tuple(quakes), backfill=True), self.clock)
        major = {q.id for q in quakes if q.id in unseen and q.mag >= self.config.alerts.major_magnitude}
        self.tracker.restore(unseen - major, major)
        renamed = [e for e in events if isinstance(e, QuakeRenamed)]  # saved before a preferred id changed
        self.tracker.observe(renamed)
        self.history.record((), set(), (), self.clock.now(), replaced={e.old_id for e in renamed})

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
            self.history.mark_seen(self.tracker.take_seen())
            await self.history.close()
        await self.client.aclose()

    def _tick(self) -> None:
        """Half-second heartbeat: attention decay, seen tracking, blink phases and the status bar countdown."""
        self.tracker.prune()
        if self.history is not None:
            self.history.mark_seen(self.tracker.take_seen())
        self.sync_tsunami()
        self.sync_gauges()
        self._ticks += 1
        if self._ticks % GAUGE_REFRESH_TICKS == 0:
            self.refresh_gauges()
        if not self.screen_stack or not self.dashboard.query(QuakeList):  # shutting down, or not yet composed
            return
        if self.screen is self.dashboard:
            quake_list = self.dashboard.query_one(QuakeList)
            visible = quake_list.visible_ids() if self.dashboard.list_shown else set()  # a hidden list is not seen
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
        notes = self._notify_desktop(list(result.events))
        self._record_history(result)
        for event in result.events:
            if isinstance(event, QuakeMajor):
                self.notify(
                    quake_line(event.quake, self.clock.now()),
                    title="Major earthquake",
                    severity="warning",
                    timeout=10,
                    markup=False,
                )
            elif isinstance(event, GaugeAnomaly):
                self._last_anomaly = event
                body = f"{event.name}: {event.residual_m:+.3f} m from the tide"
                if event.late:
                    body += f" at {fmt_time(event.ts, self.config.view.time_zone)}"
                self.notify(body, title="Gauge anomaly", severity="warning", timeout=10, markup=False)
            elif isinstance(event, SourceDegraded | SourceRecovered):
                log.info("%s", event)
        if self.screen_stack and self.screen is not self.dashboard:
            # The banner and panel live on the dashboard only: elsewhere the tsunami notes also toast in the app.
            for note in notes:
                if note.key.startswith("tsunami:"):
                    severity = "error" if note.urgency == "critical" else "warning"
                    self.notify(note.body, title=note.title, severity=severity, timeout=30, markup=False)
        if result.source == "usgs" and result.error:  # a failed backfill (deadline included) is asked again
            self._coverage_checked = False
        steady = result.source == "usgs" and result.batch is not None and not result.batch.backfill
        if (
            steady
            and not result.error
            and not self.usgs.starting
            and (not self._coverage_checked or self.store.coverage_lapsed)
            and self.scheduler.backfill_ready("usgs")  # a failed query backs off and a refused one is not repeated
        ):
            # the first good steady answer, or one after an outage longer than the feed's window: the network is up,
            # so the backfill can run. A lapse stays marked until a backfill lands, so a failed one retries on the
            # next poll, and never two at once.
            self._coverage_checked = True
            if self._backfill is None or self._backfill.done():
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
        if batch is None or batch.not_modified:
            return
        gone = {e.old_id for e in result.events if isinstance(e, QuakeRenamed)}
        gone |= {e.quake_id for e in result.events if isinstance(e, QuakeRemoved)}  # a removed quake must not return
        if not (batch.quakes or batch.alerts or gone):
            return
        unseen = {e.quake.id for e in result.events if isinstance(e, QuakeNew | QuakeMajor)}
        unseen |= {
            e.quake.id for e in result.events if isinstance(e, QuakeRenamed) and self.tracker.is_unseen(e.quake.id)
        }
        current = (q for b in batch.quakes if (q := self.store.get(b.id)) is not None)
        merged = {a.id: a for b in batch.alerts if (a := self.store.get_alert(b.id)) is not None}  # one per product
        self.history.record(current, unseen, merged.values(), batch.fetched_at, replaced=gone)

    def tsunami_feeds_down(self) -> tuple[list[tuple[str, Freshness]], bool]:
        """(tsunami feeds late, failing or partial, whether no tsunami feed is fresh). A feed not polled yet is not an
        outage; `tsunami_feeds_loading` says an empty alert list still means nothing."""
        if not self.scheduler_running:
            return [], False
        names = [n for n in TSUNAMI_FEEDS if n in self.scheduler.statuses]
        state = [(n, self.scheduler.freshness(n)) for n in names]
        down = [(n, f) for n, f in state if f in (Freshness.STALE, Freshness.ERROR)]
        # A feed that answers but skipped something (one tsunami.gov feed down, a product it could not fetch) is
        # partial: the list may be missing an alert, so it must not read as clear.
        partial = [(n, f) for n, f in state if f is Freshness.FRESH and self.scheduler.statuses[n].health.incomplete]
        return down + partial, bool(names) and all(f is not Freshness.FRESH for _, f in state)

    def quake_feed_wait(self) -> str:
        """Why the quake list cannot be trusted as it stands ("" when USGS is answering and a live batch has arrived).

        An offline run or a disabled USGS feed never loads it. A USGS that is failing or late says so, with the age of
        the last data, whether or not the store is warm: an empty list under a filter is then no answer about quakes.
        """
        if self.offline or not self.config.sources.usgs:
            return "Quakes are not being fetched"
        if self.scheduler.freshness("usgs") in (Freshness.ERROR, Freshness.STALE):
            age = self.scheduler.age("usgs")
            return "USGS is not answering" + (f"; last data {fmt_age(age)} ago" if age is not None else "")
        return "" if self.store.warm else "Waiting for USGS"

    def tsunami_feeds_loading(self) -> bool:
        """True while an enabled tsunami feed has not answered yet: an empty list says nothing until it has, because
        another feed (NWS, JMA) answering first does not cover what only this one carries."""
        return self.scheduler_running and any(
            self.scheduler.freshness(n) is Freshness.LOADING for n in TSUNAMI_FEEDS if n in self.scheduler.statuses
        )

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
            (self._gauge_quake.id, self._gauge_quake.lat, self._gauge_quake.lon) if self._gauge_quake else None,
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
        self.store.select_gauges(chosen.ids)
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
            residuals = self.store.gauge_residuals(station.id)
            limit = threshold_m(station.kind, self.config.alerts)
            exceeded = [r.ts for r in residuals if abs(r.residual_m) >= limit]  # NaN compares false
            rows.append(
                GaugeRowData(
                    station_id=station.id,
                    name=station.name.strip(),
                    distance_km=km,
                    mode=newest.mode if newest else "",
                    newest=newest.ts if newest else None,
                    residuals=tuple(r.residual_m for r in residuals),
                    threshold_m=limit,
                    kind=station.kind,
                    exceeded_at=exceeded[-1] if exceeded else None,
                    residual_at=residuals[-1].ts if residuals else None,
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

    def _notify_desktop(self, events: list) -> list[Notification]:
        """Evaluate the alert rules and hand each notification to the desktop notifier and the terminal bell."""
        notes = self.rules.evaluate(events, self.clock.now())
        for note in notes:
            if self.bell_enabled and note.bell:
                self.bell()
            if self.notifier is not None:
                self._outbox.put_nowait(note)
        return notes

    # --- view changes -------------------------------------------------------------------------------------------

    def _backfill_if_needed(self, force: bool) -> None:
        if not self.config.sources.usgs or self.offline:
            return
        if force or not self.store.is_covered(self.view.hours, self.view.min_magnitude):
            end = self.clock.now()
            start = end - timedelta(hours=self.view.hours)
            min_mag = self.view.min_magnitude

            async def job(client: httpx.AsyncClient) -> Batch:
                return await self.usgs.fetch_range(client, start, end, min_mag)

            self._backfill = self.scheduler.submit_backfill("usgs", self.client, job)
        else:
            self.store.coverage_lapsed = False  # what lapsed does not touch this view

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

    def close_observations(self) -> None:
        """Leaving the observations screen releases the quake `o` pinned, so its stations stop being polled."""
        if self._gauge_quake is not None:
            self._gauge_quake = None
            self.sync_gauges()

    def pin_gauge_quake(self, quake: Earthquake) -> None:
        """Select the stations nearest `quake` once USGS has flagged it (or while it is already pinned), and follow it
        to its newest stored revision, so a relocation re-selects. Safe to call on every refresh."""
        if self.dart is None:
            return
        current = self.store.resolve(quake.id) or quake
        pinned = self._gauge_quake
        held = self.store.resolve(pinned.id) if pinned is not None else None  # a renamed quake is still the pinned one
        if not (current.tsunami_flag or (pinned is not None and current.id in (pinned.id, held.id if held else None))):
            return
        self._gauge_quake = current
        self.sync_gauges()

    def open_observations(self, quake: Earthquake) -> None:
        """`o`: select the stations nearest a USGS-flagged quake and show their gauges."""
        self.pin_gauge_quake(quake)
        if not isinstance(self.screen, ObservationsScreen):
            self.push_screen(ObservationsScreen(quake))

    def open_history(self) -> None:
        if self.history is None:
            self.notify(
                self.history_error or "No saved history is available", severity="warning", timeout=4, markup=False
            )
        elif not isinstance(self.screen, HistoryScreen):
            self.push_screen(HistoryScreen())

    def open_detail(self, quake: Earthquake) -> None:
        self.push_screen(DetailScreen(quake))
