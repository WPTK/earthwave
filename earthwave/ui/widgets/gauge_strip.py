"""Gauge strip: one row per selected DART buoy or tide gauge with a residual sparkline and the signed maximum."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Sparkline, Static

from earthwave.sources.geo import COOPS_MAX_KM
from earthwave.state.store import GAUGE_FRESH
from earthwave.ui.labels import fmt_age

MAX_POINTS = 240  # sparkline samples drawn per row; the "24 h max" summary reads every stored residual
NAME_WIDTH = 26
STALE_AFTER = timedelta(hours=1)  # a live gauge (6-minute, 1-minute, 15-second) silent this long is stale
RESIDUAL_LAG = timedelta(hours=1)  # residuals this far behind the newest reading no longer describe the water now
STALE_AFTER_BATCH = timedelta(hours=7)  # a 15-minute DART file arrives in batches, hours apart; 7 h is one past a gap


@dataclass(frozen=True, slots=True)
class GaugeRowData:
    station_id: str
    name: str
    distance_km: float | None  # from the quake or alert geometry
    mode: str  # 15m | 1m | 15s | 6m, the mode of the newest reading
    newest: datetime | None  # timestamp of the newest reading
    residuals: tuple[float, ...]  # signed metres, oldest first
    threshold_m: float  # the anomaly threshold for this station's kind
    kind: str = ""  # dart | coops
    exceeded_at: datetime | None = None  # timestamp of the newest residual at or past the threshold
    residual_at: datetime | None = None  # timestamp of the newest residual; it lags `newest` while detection is blind


def shown_residuals(residuals: Sequence[float]) -> list[float]:
    """The residuals the row draws: the newest MAX_POINTS, NaN dropped. The summary is not limited to these."""
    return [r for r in residuals[-MAX_POINTS:] if not math.isnan(r)]


def max_residual(residuals: Sequence[float]) -> float | None:
    """The residual with the largest magnitude, sign kept; None when there is none (NaN is ignored)."""
    finite = [r for r in residuals if not math.isnan(r)]
    return max(finite, key=abs) if finite else None


def is_stale(row: GaugeRowData, now: datetime) -> bool:
    """The newest reading is too old for the row to describe the water now (a row with no reading is 'no data')."""
    if row.newest is None:
        return False
    return now - row.newest > (STALE_AFTER_BATCH if row.mode == "15m" else STALE_AFTER)


def sparkline_data(residuals: Sequence[float], threshold_m: float) -> list[float]:
    """abs(residual) padded with [0, threshold] so the threshold frames the strip.

    Textual's Sparkline scales to the data it is given, so without the padding a +-2 cm tide wobble would draw as
    full-height noise. The two leading samples fix the scale at 0..threshold and read as a reference mark on the left.
    """
    return [0.0, threshold_m, *(abs(r) for r in shown_residuals(residuals))]


def short_name(name: str) -> str:
    """The name cut to NAME_WIDTH with an ellipsis, never ending on a dangling separator ("South Dutch Harbor -")."""
    if len(name) <= NAME_WIDTH:
        return name
    cut = name[: NAME_WIDTH - 1]
    while cut and cut[-1] in " -–—,.:/":  # noqa: RUF001
        cut = cut[:-1]
    return cut + "…"


def residual_lag(row: GaugeRowData) -> timedelta | None:
    """How far the newest residual trails the newest reading, when that is long enough to matter."""
    if row.newest is None or row.residual_at is None or row.newest - row.residual_at <= RESIDUAL_LAG:
        return None
    return row.newest - row.residual_at


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
    text.append(f"{short_name(name):<{NAME_WIDTH}} ", "bold")
    text.append(f"{dist} ", "grey62")
    text.append(f"{row.mode:<3} ", "cyan")
    text.append(age_text(row, now), "grey70")
    return text


def is_over_now(row: GaugeRowData, now: datetime) -> bool:
    """A live row whose newest exceedance is within GAUGE_FRESH; an older peak does not count."""
    return not is_stale(row, now) and row.exceeded_at is not None and now - row.exceeded_at <= GAUGE_FRESH


def max_text(row: GaugeRowData, now: datetime) -> Text:
    peak = max_residual(row.residuals)  # all stored residuals (about 24 h), not just the drawn tail
    if peak is None:
        return Text("no residual", style="grey50", no_wrap=True)
    if is_stale(row, now):  # old data is never flagged or coloured as if it were live
        return Text(f"{peak:+.3f} m stale", style="grey50", no_wrap=True)
    lag = residual_lag(row)
    if lag is not None:  # detection is blind here: an old residual must not read as the current, calm value
        return Text(f"resid {fmt_age(lag.total_seconds())} old", style="yellow", no_wrap=True)
    if is_over_now(row, now):
        return Text(f"{peak:+.3f} m !", style="bold red", no_wrap=True)
    if abs(peak) >= row.threshold_m:  # it settled: the peak is history, not a current exceedance
        return Text(f"{peak:+.3f} m peak", style="grey70", no_wrap=True)
    return Text(f"{peak:+.3f} m", style="green", no_wrap=True)


class GaugeRow(Horizontal):
    DEFAULT_CSS = """
    GaugeRow > Sparkline > .sparkline--min-color { color: $success; }
    GaugeRow > Sparkline > .sparkline--max-color { color: $warning; }
    GaugeRow.over > Sparkline > .sparkline--max-color { color: $error; }
    """

    def __init__(self, row: GaugeRowData, now: datetime):
        super().__init__(id=f"gauge-{row.station_id}")
        self.row = row
        self._now = now

    def compose(self) -> ComposeResult:
        self.set_class(is_over_now(self.row, self._now), "over")
        yield Static(row_label(self.row, self._now), classes="gauge-label")
        if shown_residuals(self.row.residuals):
            yield Sparkline(sparkline_data(self.row.residuals, self.row.threshold_m), classes="gauge-spark")
        else:  # nothing to draw: Sparkline would scale an empty series to a solid block
            yield Static("", classes="gauge-spark")
        yield Static(max_text(self.row, self._now), classes="gauge-max")


class GaugeHeader(Horizontal):
    """The column headings, in the same columns as the rows."""

    def compose(self) -> ComposeResult:
        yield Static(f"{'Station':<{NAME_WIDTH}} {'Distance':>8} {'Mode':<3} Reading", classes="gauge-label")
        yield Static("Residual", classes="gauge-spark")
        yield Static("24 h max", classes="gauge-max")


class GaugeStrip(Vertical):
    DEFAULT_CSS = """
    GaugeStrip { height: auto; }
    GaugeStrip > Horizontal { height: 1; }
    GaugeStrip > GaugeHeader { color: $text-muted; }
    GaugeStrip .gauge-label { width: 58; }
    GaugeStrip .gauge-spark { width: 1fr; min-width: 8; margin: 0 1; }
    GaugeStrip .gauge-max { width: 18; padding: 0 1; text-align: right; }
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
        yield GaugeHeader()
        for row in self._rows:
            yield GaugeRow(row, self._now)
        if any(r.kind == "dart" for r in self._rows) and not any(r.kind == "coops" for r in self._rows):
            coops = getattr(self.app, "coops", None)
            if coops is not None and not coops.stations:  # an outage or the first fetch, not a fact about the coast
                yield Static("Tide gauge list not loaded yet.", classes="gauge-empty")
            else:
                yield Static(f"No tide gauge within {COOPS_MAX_KM:.0f} km.", classes="gauge-empty")

    def show(self, rows: Sequence[GaugeRowData], now: datetime) -> None:
        self._rows = tuple(rows)
        self._now = now
        self.refresh(recompose=True)
