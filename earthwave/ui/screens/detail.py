"""Quake detail screen: every field, PAGER/ShakeMap/DYFI from the lazily fetched detail product, open and copy URL."""

import webbrowser
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from earthwave.models import Earthquake, QuakeDetail
from earthwave.ui.labels import fmt_age, fmt_distance, fmt_time
from earthwave.ui.theme import PAGER_COLORS, mag_color

if TYPE_CHECKING:
    from earthwave.app import EarthwaveApp


def _row(text: Text, label: str, value: str, style: str = "white") -> None:
    text.append(f"{label:<14}", "grey62")
    text.append(f"{value}\n", style)


def render_detail(
    quake: Earthquake, detail: QuakeDetail | None, note: str, home: tuple[float, float] | None, time_zone: str, now
) -> Text:
    text = Text()
    text.append(f"M{quake.mag:.1f} ", mag_color(quake.mag))
    text.append(f"{quake.place or 'unknown location'}\n\n", "bold")
    _row(text, "Time", f"{fmt_time(quake.time, time_zone)} ({fmt_age((now - quake.time).total_seconds())} ago)")
    if quake.updated:
        _row(text, "Updated", fmt_time(quake.updated, time_zone))
    _row(text, "Magnitude", f"{quake.mag:.1f} {quake.mag_type}".strip())
    _row(text, "Depth", f"{quake.depth_km:.1f} km")
    _row(text, "Location", f"{quake.lat:.3f}, {quake.lon:.3f}")
    if home is not None:
        _row(text, "From home", fmt_distance(quake, *home))
    _row(text, "Status", quake.status)
    _row(text, "Significance", str(quake.sig))
    if quake.felt:
        _row(text, "Felt reports", f"{quake.felt} (CDI {quake.cdi})" if quake.cdi is not None else str(quake.felt))
    if quake.mmi is not None:
        _row(text, "Max MMI", f"{quake.mmi:g}")
    if quake.tsunami_flag:
        _row(text, "Tsunami", "USGS links a tsunami product", "bold blue")
    alert = (detail.pager_alert if detail else None) or quake.pager_alert
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
        text.append(f"\n{note}\n", "yellow")
    text.append("\n[b] open in browser   [y] copy URL   [esc] back", "grey50")
    return text


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

    @property
    def earthwave(self) -> "EarthwaveApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail_scroll"):
            yield Static("", id="detail")

    def on_mount(self) -> None:
        self._show()
        self.run_worker(self._load(), exit_on_error=False)

    async def _load(self) -> None:
        app = self.earthwave
        if app.offline:
            self.note = "Offline: PAGER, ShakeMap and nearby cities are not available."
            self._show()
            return
        try:
            self.detail = await app.usgs.fetch_detail(app.client, self.quake)
            self.note = ""
        except Exception as exc:
            self.note = f"Detail unavailable: {type(exc).__name__}: {exc}"
        self._show()

    def _show(self) -> None:
        app = self.earthwave
        home = (app.config.home.lat, app.config.home.lon) if app.config.home.enabled else None
        self.query_one("#detail", Static).update(
            render_detail(self.quake, self.detail, self.note, home, app.config.view.time_zone, app.clock.now())
        )

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_open_browser(self) -> None:
        if self.quake.url:
            webbrowser.open(self.quake.url)

    def action_copy_url(self) -> None:
        if self.quake.url:
            self.app.copy_to_clipboard(self.quake.url)
            self.app.notify("URL copied", timeout=2)
