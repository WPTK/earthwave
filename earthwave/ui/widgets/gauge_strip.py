"""Gauge strip: one row per selected DART buoy or tide gauge with a residual sparkline and the signed maximum."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Sparkline, Static

from earthwave.ui.labels import fmt_age

MAX_POINTS = 240  # sparkline samples per row; older residuals are dropped from the drawing
NAME_WIDTH = 20


@dataclass(frozen=True, slots=True)
class GaugeRowData:
    station_id: str
    name: str
    distance_km: float | None  # from the quake or alert geometry
    mode: str  # 15m | 1m | 15s | 6m, the mode of the newest reading
    newest: datetime | None  # timestamp of the newest reading
    residuals: tuple[float, ...]  # signed metres, oldest first
    threshold_m: float  # the anomaly threshold for this station's kind


def shown_residuals(residuals: Sequence[float]) -> list[float]:
    """The residuals the row draws and summarises: the newest MAX_POINTS, NaN dropped."""
    return [r for r in residuals[-MAX_POINTS:] if not math.isnan(r)]


def max_residual(residuals: Sequence[float]) -> float | None:
    """The residual with the largest magnitude, sign kept; None when there is none (NaN is ignored)."""
    finite = [r for r in residuals if not math.isnan(r)]
    return max(finite, key=abs) if finite else None


def sparkline_data(residuals: Sequence[float], threshold_m: float) -> list[float]:
    """abs(residual) padded with [0, threshold] so the threshold frames the strip.

    Textual's Sparkline scales to the data it is given, so without the padding a +-2 cm tide wobble would draw as
    full-height noise. The two leading samples fix the scale at 0..threshold and read as a reference mark on the left.
    """
    return [0.0, threshold_m, *(abs(r) for r in shown_residuals(residuals))]


def age_text(row: GaugeRowData, now: datetime) -> str:
    """'42s ago' for live data; 'last batch 3h ago' for a 15-minute DART file, which arrives in batches."""
    if row.newest is None:
        return "no data"
    age = fmt_age((now - row.newest).total_seconds())
    return f"last batch {age} ago" if row.mode == "15m" else f"{age} ago"


def row_label(row: GaugeRowData, now: datetime) -> Text:
    name = row.name or row.station_id
    dist = f"{row.distance_km:5.0f} km" if row.distance_km is not None else "       "
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{name[:NAME_WIDTH]:<{NAME_WIDTH}} ", "bold")
    text.append(f"{dist} ", "grey62")
    text.append(f"{row.mode:<3} ", "cyan")
    text.append(age_text(row, now), "grey70")
    return text


def max_text(row: GaugeRowData) -> Text:
    peak = max_residual(shown_residuals(row.residuals))
    if peak is None:
        return Text("no residual", style="grey50", no_wrap=True)
    over = abs(peak) >= row.threshold_m
    return Text(f"{peak:+.3f} m{' !' if over else ''}", style="bold red" if over else "green", no_wrap=True)


class GaugeRow(Horizontal):
    DEFAULT_CSS = """
    GaugeRow { height: 1; }
    GaugeRow > .gauge-label { width: 52; }
    GaugeRow > Sparkline { width: 1fr; min-width: 8; margin: 0 1; }
    GaugeRow > .gauge-max { width: 14; text-align: right; }
    """

    def __init__(self, row: GaugeRowData, now: datetime):
        super().__init__(id=f"gauge-{row.station_id}")
        self.row = row
        self._now = now

    def compose(self) -> ComposeResult:
        over = any(abs(r) >= self.row.threshold_m for r in shown_residuals(self.row.residuals))
        yield Static(row_label(self.row, self._now), classes="gauge-label")
        yield Sparkline(
            sparkline_data(self.row.residuals, self.row.threshold_m),
            min_color="green",
            max_color="red" if over else "yellow",
        )
        yield Static(max_text(self.row), classes="gauge-max")


class GaugeStrip(Vertical):
    DEFAULT_CSS = """
    GaugeStrip { height: auto; }
    GaugeStrip > .gauge-empty { color: $text-muted; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rows: tuple[GaugeRowData, ...] = ()
        self._now: datetime | None = None

    def compose(self) -> ComposeResult:
        if not self._rows or self._now is None:
            yield Static("No gauge stations selected.", classes="gauge-empty")
            return
        for row in self._rows:
            yield GaugeRow(row, self._now)

    def show(self, rows: Sequence[GaugeRowData], now: datetime) -> None:
        self._rows = tuple(rows)
        self._now = now
        self.refresh(recompose=True)
