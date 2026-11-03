"""Quake detail screen: every field, PAGER/ShakeMap/DYFI from the lazily fetched detail product, open and copy URL."""

import webbrowser
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from rich.text import Text
from textual import events
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from earthwave.models import Earthquake, QuakeDetail
from earthwave.seismo import ENERGY_PER_MAGNITUDE_STEP, arrivals, energy_joules, fmt_tnt, tnt_tons
from earthwave.sources.geo import haversine_km
from earthwave.ui.labels import fmt_age, fmt_countdown, fmt_distance, fmt_time, pager_badge, tsunami_badge
from earthwave.ui.theme import PAGER_COLORS, TSUNAMI_BADGE_COLOR, YELLOW, mag_color
from earthwave.ui.widgets.too_small import TooSmall, sync_too_small

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp


def is_console_browser() -> bool:
    """True when `webbrowser` would start a program that draws on this terminal and holds it until it exits.

    A custom GenericBrowser that launches a GUI (wslview, say) looks the same, so the app briefly suspends for it.
    """
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    if isinstance(browser, webbrowser.BackgroundBrowser):
        return False
    return isinstance(browser, webbrowser.GenericBrowser) or getattr(browser, "background", True) is False


def _row(text: Text, label: str, value: str, style: str = "white") -> None:
    text.append(f"{label:<16}", "grey62")
    text.append(f"{value}\n", style)


def _wave_arrival(arrival, now, time_zone: str) -> str:
    seconds = (arrival - now).total_seconds()
    when = "arrived" if seconds <= 0 else f"in {fmt_countdown(seconds)}"
    return f"{fmt_time(arrival, time_zone)} ({when})"


GONE_NOTE = "USGS no longer lists this quake; these are the last values received."


def render_detail(
    quake: Earthquake,
    detail: QuakeDetail | None,
    note: str,
    home: tuple[float, float] | None,
    time_zone: str,
    now,
    detail_current: bool = True,
) -> Text:
    """`detail_current`: the product was fetched for this revision; an older one yields to the quake's own values."""
    text = Text()
    text.append(f"M{quake.mag:.1f} ", mag_color(quake.mag))
    product = (detail.pager_alert if detail else None) if detail_current else None
    alert = product or quake.pager_alert or (detail.pager_alert if detail else None)
    for badge, color in (tsunami_badge(quake.tsunami_flag), pager_badge(alert)):  # the list's FL letters, in brackets
        if badge != " ":
            text.append(f"[{badge}] ", color)
    place = "".join(c if c.isprintable() else " " for c in quake.place).strip()
    text.append(f"{place or 'unknown location'}\n\n", "bold")
    _row(text, "Time", f"{fmt_time(quake.time, time_zone)} ({fmt_age((now - quake.time).total_seconds())} ago)")
    if quake.updated:
        _row(text, "Updated", fmt_time(quake.updated, time_zone))
    _row(text, "Magnitude", f"{quake.mag:.1f} {quake.mag_type}".strip())
    _row(text, "Depth", f"{quake.depth_km:.1f} km")
    _row(text, "Energy", f"{energy_joules(quake.mag):.1e} J (~{fmt_tnt(tnt_tons(quake.mag))} TNT)")
    _row(text, "", f"each +1.0 magnitude = {ENERGY_PER_MAGNITUDE_STEP:.1f}x energy", "grey70")
    _row(text, "Location", f"{quake.lat:.3f}, {quake.lon:.3f}")
    if home is not None:
        _row(text, "From home", fmt_distance(quake, *home))
        p_arrival, s_arrival = arrivals(haversine_km(*home, quake.lat, quake.lon), quake.depth_km, quake.time)
        _row(text, "P wave", _wave_arrival(p_arrival, now, time_zone))
        _row(text, "S wave", _wave_arrival(s_arrival, now, time_zone))
    _row(text, "Status", quake.status)
    _row(text, "Significance", str(quake.sig))
    if quake.felt:
        _row(text, "Felt reports", f"{quake.felt} (CDI {quake.cdi})" if quake.cdi is not None else str(quake.felt))
    if quake.mmi is not None:
        _row(text, "Max MMI", f"{quake.mmi:g}")
    if quake.tsunami_flag:
        _row(text, "Tsunami", "USGS links a tsunami product", TSUNAMI_BADGE_COLOR)
    if alert:
        _row(text, "PAGER alert", alert, PAGER_COLORS.get(alert, "white"))
    if detail:
        if detail.pager_max_mmi is not None:
            _row(text, "PAGER max MMI", f"{detail.pager_max_mmi:g}")
        if detail.shakemap_max_mmi is not None:
            _row(text, "ShakeMap MMI", f"{detail.shakemap_max_mmi:.1f}")
        if detail.shakemap_max_pga is not None:
            _row(text, "ShakeMap PGA", f"{detail.shakemap_max_pga:.1f} %g")
        if detail.shakemap_max_pgv is not None:
            _row(text, "ShakeMap PGV", f"{detail.shakemap_max_pgv:.1f} cm/s")
        if detail.dyfi_max_mmi is not None:
            _row(text, "DYFI MMI", f"{detail.dyfi_max_mmi:.1f} ({detail.dyfi_responses or 0} responses)")
        if detail.products:
            _row(text, "Products", ", ".join(detail.products), "grey70")
        if detail.nearby_cities:
            text.append("\nNearby cities\n", "grey62")
            for city in detail.nearby_cities:
                text.append(f"  {city}\n")
    if quake.also_ids:
        _row(text, "Other ids", ", ".join(quake.also_ids), "grey70")
    text.append("\n")
    _row(text, "Event page", quake.url, "underline")
    if note:
        text.append(f"\n{note}\n", YELLOW)
    return text


REFRESH_SECONDS = 1.0


class DetailScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "open_browser", "Browser"),
        Binding("y", "copy_url", "Copy URL"),
    ]

    def __init__(self, quake: Earthquake):
        super().__init__()
        self.quake = quake
        self.detail: QuakeDetail | None = None
        self.note = "Loading detail..."
        self.gone = False  # the store held the quake when this screen opened and has dropped it since
        self._held = False  # the store has held the quake while this screen was open
        self._requested: tuple[str, object] | None = None  # (id, updated) the detail was last asked for
        self._fetched: tuple[str, object] | None = None  # (id, updated) the shown detail belongs to

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail_scroll"):
            yield Static("", id="detail")
        yield Footer(compact=True, show_command_palette=False)
        yield TooSmall(id="too_small")

    def on_resize(self, event: events.Resize) -> None:
        if not sync_too_small(self, event):  # hiding the scroll area dropped its focus, and the scroll keys with it
            self.call_after_refresh(self._focus_scroll)

    def _focus_scroll(self) -> None:
        for scroll in self.query("#detail_scroll"):
            scroll.focus()

    def on_mount(self) -> None:
        self._show()
        self.set_interval(REFRESH_SECONDS, self._show)  # the store holds revisions, and the age moves on

    def _key(self) -> tuple[str, object]:
        return self.quake.id, self.quake.updated

    def _load_if_changed(self) -> None:
        """Fetch the detail product once per revision: on open, and again when `updated` or the id moves."""
        if self._requested == self._key():
            return
        self._requested = self._key()
        self.run_worker(self._load(self.quake), group="detail", exclusive=True, exit_on_error=False)

    async def _load(self, quake: Earthquake) -> None:
        app = self.earthwave
        if app.offline:
            self.note = "Offline: PAGER, ShakeMap and nearby cities are not available."
            self._show()
            return
        try:
            self.detail = await app.usgs.fetch_detail(app.client, quake)
            self._fetched = (quake.id, quake.updated)
            self.note = ""
        except Exception as exc:
            self.note = f"Detail unavailable: {type(exc).__name__}: {exc}"
        self._show()

    def _show(self) -> None:
        app = self.earthwave
        current = app.store.resolve(self.quake.id)  # follows a preferred-id change through the store's alias map
        self._held = self._held or current is not None
        # A quake the store never held (a history row older than its 35 days) is not "gone": USGS still lists it.
        self.gone = current is None and self._held
        self.quake = current or self.quake  # the snapshot only if the store dropped the quake or never held it
        self._load_if_changed()  # once per revision, whether or not the store holds it
        home = (app.config.home.lat, app.config.home.lon) if app.config.home.enabled else None
        note = "\n".join(n for n in (self.note, GONE_NOTE if self.gone else "") if n)
        self.query_one("#detail", Static).update(
            render_detail(
                self.quake,
                self.detail,
                note,
                home,
                app.config.view.time_zone,
                app.clock.now(),
                detail_current=self._fetched == self._key(),
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_open_browser(self) -> None:
        """Only web pages open: on Windows webbrowser.open hands any string to os.startfile (files, programs).

        A graphical browser opens from a thread: webbrowser.open can wait on it. A console browser (w3m, lynx, links
        on a machine with no display, over ssh) takes over the terminal this app is drawing on, so the app steps
        aside with App.suspend() until the browser exits; started beside the running app it left the screen a mix of
        both and the keyboard dead (#246). Where the terminal cannot be handed over, the URL is shown instead.
        """
        url = self.quake.url
        if urlsplit(url).scheme not in ("http", "https"):
            return
        if not is_console_browser():
            self.run_worker(lambda: webbrowser.open(url), thread=True, exit_on_error=False)
            return
        try:
            with self.app.suspend():
                webbrowser.open(url)
        except SuspendNotSupported:
            self.app.notify(f"No graphical browser here. Open this page yourself: {url}", timeout=15)

    def action_copy_url(self) -> None:
        if self.quake.url:
            self.app.copy_to_clipboard(self.quake.url)
            self.app.notify("URL copied", timeout=2)
