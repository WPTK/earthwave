"""M4b: the standalone gauge strip widget (not on any screen yet): row text, sparkline framing, Pilot test, snapshot."""

from datetime import UTC, datetime, timedelta

from conftest import FIXTURES
from textual.app import App, ComposeResult
from textual.widgets import Sparkline

from earthwave.sources import coops, ndbc
from earthwave.ui.widgets.gauge_strip import (
    MAX_POINTS,
    GaugeRow,
    GaugeRowData,
    GaugeStrip,
    age_text,
    is_over_now,
    is_stale,
    max_residual,
    max_text,
    row_label,
    sparkline_data,
)

NOW = datetime(2026, 10, 31, 12, 5, tzinfo=UTC)
DART_THRESHOLD = 0.05
GAUGE_THRESHOLD = 0.3


def _row(**kwargs) -> GaugeRowData:
    base = {
        "station_id": "46402",
        "name": "46402 Aleutians",
        "distance_km": 1234.0,
        "mode": "15m",
        "newest": NOW - timedelta(hours=3),
        "residuals": (0.004, -0.012, 0.008),
        "threshold_m": DART_THRESHOLD,
    }
    return GaugeRowData(**{**base, **kwargs})


def _last_over(residuals, threshold):
    return next((r.ts for r in reversed(residuals) if abs(r.residual_m) >= threshold), None)


def fixture_rows() -> list[GaugeRowData]:
    """Three rows from saved live payloads: a standard-mode buoy, the 32401 event-mode buoy and a tide gauge."""
    dart = (FIXTURES / "ndbc" / "46402.dart").read_text(encoding="utf-8")
    standard = ndbc.detrend(ndbc.parse_dart(dart, "46402")[-120:])
    event_rows = ndbc.parse_dart((FIXTURES / "ndbc" / "32401.dart").read_text(encoding="utf-8"), "32401")
    event_end = datetime(2026, 10, 9, 22, 0, tzinfo=UTC)
    event_rows = [r for r in event_rows if event_end - timedelta(hours=4) <= r.ts <= event_end]
    event = [
        r
        for r in ndbc.detrend(ndbc.parse_dart((FIXTURES / "ndbc" / "32401.dart").read_text(encoding="utf-8"), "32401"))
        if r.ts in {x.ts for x in event_rows}
    ]
    obs = coops.parse_one_minute_water_level(
        (FIXTURES / "coops" / "one_minute_water_level_9414290.json").read_text(encoding="utf-8")
    )
    preds = coops.parse_predictions(
        (FIXTURES / "coops" / "predictions_1min_9414290.json").read_text(encoding="utf-8"), "9414290"
    )
    gauge = coops.residuals(obs, preds)
    return [
        GaugeRowData(
            "46402",
            "46402 Aleutians",
            1850.0,
            "15m",
            standard[-1].ts,
            tuple(r.residual_m for r in standard),
            DART_THRESHOLD,
            "dart",
            _last_over(standard, DART_THRESHOLD),
        ),
        GaugeRowData(
            "32401",
            "32401 event buoy",
            640.0,
            "1m",
            event_rows[-1].ts,
            tuple(r.residual_m for r in event),
            DART_THRESHOLD,
            "dart",
            _last_over(event, DART_THRESHOLD),
        ),
        GaugeRowData(
            "9414290",
            "San Francisco",
            95.0,
            "1m",
            gauge[-1].ts,
            tuple(r.residual_m for r in gauge),
            GAUGE_THRESHOLD,
            "coops",
            _last_over(gauge, GAUGE_THRESHOLD),
        ),
    ]


def test_age_text_says_last_batch_for_standard_mode_only():
    assert age_text(_row(), NOW) == "last batch 3h ago"
    assert age_text(_row(mode="1m", newest=NOW - timedelta(seconds=42)), NOW) == "42s ago"
    assert age_text(_row(mode="6m", newest=NOW - timedelta(minutes=12)), NOW) == "12m ago"
    assert age_text(_row(newest=None), NOW) == "no data"


def test_label_and_max_text():
    label = row_label(_row(), NOW).plain
    assert (
        label.startswith("46402 Aleutians") and "1234 km" in label and "15m" in label and "last batch 3h ago" in label
    )
    assert row_label(_row(distance_km=None), NOW).plain.count("km") == 0
    quiet = max_text(_row(), NOW)
    assert quiet.plain == "-0.012 m" and "green" in str(quiet.style)
    loud = max_text(_row(residuals=(0.01, -0.081, 0.02), exceeded_at=NOW - timedelta(minutes=10)), NOW)
    assert loud.plain == "-0.081 m !" and "red" in str(loud.style)
    assert max_text(_row(residuals=()), NOW).plain == "no residual"
    assert max_residual(()) is None and max_residual((0.01, -0.02, 0.015)) == -0.02


def test_a_row_with_old_data_says_stale_instead_of_flagging_it():
    old = _row(mode="1m", newest=NOW - timedelta(hours=20), residuals=(0.01, -0.081, 0.02))
    text = max_text(old, NOW)
    assert text.plain == "-0.081 m stale" and "!" not in text.plain and "red" not in str(text.style)
    live = _row(
        mode="1m",
        newest=NOW - timedelta(minutes=2),
        residuals=(0.01, 0.02, -0.081),
        exceeded_at=NOW - timedelta(minutes=2),
    )
    assert max_text(live, NOW).plain == "-0.081 m !"
    # a 15-minute DART file arrives in batches, so hours-old data is normal for it, a day is not
    assert not is_stale(_row(), NOW) and is_stale(_row(newest=NOW - timedelta(hours=20)), NOW)
    assert is_stale(_row(mode="6m", newest=NOW - timedelta(hours=2)), NOW)


def test_an_exceedance_older_than_gauge_fresh_reads_peak_even_on_a_live_batch_row():
    # a 15-minute DART batch 6 h old is still live (not stale), but its exceedance is 6 h old, so no red "!"
    six_hours = NOW - timedelta(hours=6)
    row = _row(newest=six_hours, residuals=(0.01, -0.081, 0.02), exceeded_at=six_hours - timedelta(minutes=15))
    assert not is_stale(row, NOW) and not is_over_now(row, NOW)
    text = max_text(row, NOW)
    assert text.plain == "-0.081 m peak" and "red" not in str(text.style)
    fresh = _row(newest=NOW - timedelta(minutes=5), residuals=(0.01, -0.081), exceeded_at=NOW - timedelta(minutes=20))
    assert is_over_now(fresh, NOW) and max_text(fresh, NOW).plain.endswith("!")


async def test_strip_says_when_no_tide_gauge_is_in_range():
    dart_only = [r for r in fixture_rows() if r.kind == "dart"]
    app = StripApp(dart_only)
    async with app.run_test(size=(110, 8)) as pilot:
        await pilot.pause()
        text = " ".join(str(w.render()) for w in app.query("Static"))
        assert "No tide gauge within 1500 km" in text
        app.query_one(GaugeStrip).show(fixture_rows(), NOW)
        await pilot.pause()
        assert "No tide gauge" not in " ".join(str(w.render()) for w in app.query("Static"))


def test_sparkline_is_framed_by_zero_and_threshold():
    data = sparkline_data((0.001, -0.02, 0.5), DART_THRESHOLD)
    assert data[:2] == [0.0, DART_THRESHOLD] and data[2:] == [0.001, 0.02, 0.5]
    # a quiet series never exceeds the frame, so a 2 cm wobble cannot fill the strip
    quiet = sparkline_data((0.02, -0.01), DART_THRESHOLD)
    assert max(quiet) == DART_THRESHOLD
    assert len(sparkline_data(tuple([0.01] * 1000), DART_THRESHOLD)) == 242


def test_summary_and_colour_use_only_the_drawn_window():
    from earthwave.ui.widgets.gauge_strip import MAX_POINTS

    residuals = (0.9, *([0.001] * (MAX_POINTS + 59)))  # a spike 300 samples back, outside the drawn 240
    row = _row(residuals=residuals)
    assert max_text(row, NOW).plain == "+0.001 m" and "green" in str(max_text(row, NOW).style)
    assert max(sparkline_data(residuals, DART_THRESHOLD)) == DART_THRESHOLD
    assert max_residual((float("nan"), -0.02)) == -0.02 and max_residual((float("nan"),)) is None


class StripApp(App):
    def __init__(self, rows: list[GaugeRowData]):
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        yield GaugeStrip(id="strip")

    def on_mount(self) -> None:
        self.query_one(GaugeStrip).show(self._rows, NOW)


async def test_strip_renders_one_row_per_station_and_updates():
    rows = fixture_rows()
    app = StripApp(rows)
    async with app.run_test(size=(110, 8)) as pilot:
        await pilot.pause()
        strip = app.query_one(GaugeStrip)
        assert [r.row.station_id for r in strip.query(GaugeRow)] == ["46402", "32401", "9414290"]
        for row, data in zip(strip.query(GaugeRow), rows, strict=True):
            spark = row.query_one(Sparkline)
            assert (
                list(spark.data[:2]) == [0.0, data.threshold_m]
                and len(spark.data) == min(len(data.residuals), MAX_POINTS) + 2
            )
        strip.show(rows[:1], NOW)
        await pilot.pause()
        assert len(strip.query(GaugeRow)) == 1
        strip.show([], NOW)
        await pilot.pause()
        assert not strip.query(GaugeRow) and strip.query(".gauge-empty")


def test_gauge_strip_snapshot_110x8(snap_compare):
    assert snap_compare(StripApp(fixture_rows()), terminal_size=(110, 8))
