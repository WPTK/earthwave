"""Command line entry point."""

import argparse
import asyncio
import importlib
import json
import logging
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from earthwave import __version__
from earthwave.app import EarthwaveApp
from earthwave.clock import Clock, ReplayClock, SystemClock
from earthwave.config import Config, ConfigError, load_config, parse_home, with_overrides
from earthwave.logging_setup import setup_logging
from earthwave.models import Batch, Earthquake
from earthwave.net import RecordingTransport, ReplayTransport, recording_start
from earthwave.notify.desktop import default_notifier
from earthwave.sources.usgs import parse_collection
from earthwave.state.history import History, default_path
from earthwave.state.store import Store
from earthwave.ui.graphics.probe import BRAILLE
from earthwave.ui.graphics.probe import resolve as resolve_graphics
from earthwave.ui.widgets.quake_list import QuakeList

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MODULES = ("textual", "httpx", "platformdirs")


def _need(count: int, what: str) -> int:
    if count <= 0:
        raise ValueError(f"selftest: {what} is empty")
    return count


def _parse_fixtures(root: Path) -> list[str]:
    """Parse every captured fixture format; return one line per source. Raises if a fixture is empty."""
    lines = []
    for name in ("all_hour", "4.5_week", "significant_month"):
        data = json.loads((root / "usgs" / f"{name}.geojson").read_text(encoding="utf-8"))
        lines.append(f"usgs/{name}: {_need(len(data['features']), f'usgs/{name}')} quakes")
    detail = json.loads((root / "usgs" / "detail.geojson").read_text(encoding="utf-8"))
    products = detail["properties"]["products"]
    missing = {"losspager", "shakemap"} - products.keys()
    if missing:
        raise ValueError(f"selftest: usgs/detail lacks {sorted(missing)}")
    lines.append("usgs/detail: losspager + shakemap")
    xml_files = sorted((root / "tsunamigov").glob("*.xml"))
    for path in xml_files:
        ET.parse(path)
    texts = sorted((root / "tsunamigov").glob("*.txt"))
    for path in texts:
        _need(len(path.read_text(encoding="utf-8").strip()), f"tsunamigov/{path.name}")
    _need(len(xml_files), "tsunamigov xml")
    lines.append(f"tsunamigov: {len(xml_files)} xml, {_need(len(texts), 'tsunamigov txt')} bulletins")
    stations = ET.parse(root / "ndbc" / "activestations.xml").getroot().findall("station")
    lines.append(f"ndbc stations: {_need(len(stations), 'ndbc stations')}")
    dart = [r for r in (root / "ndbc" / "46402.dart").read_text(encoding="utf-8").splitlines() if r[:1] != "#"]
    lines.append(f"ndbc 46402.dart: {_need(len(dart), 'ndbc dart')} rows")
    water = json.loads((root / "coops" / "water_level_9414290.json").read_text(encoding="utf-8"))
    lines.append(f"coops water_level: {_need(len(water['data']), 'coops water_level')} rows")
    events = json.loads((root / "jma" / "list.json").read_text(encoding="utf-8"))
    lines.append(f"jma list: {_need(len(events), 'jma list')} events")
    _need(len(json.loads((root / "jma" / "detail.json").read_text(encoding="utf-8"))), "jma detail")
    return lines


def _frame_quakes(root: Path) -> tuple[list[Earthquake], datetime]:
    """Quakes for the headless frame: the captured all_hour feed when present, else two built-in samples."""
    path = root / "usgs" / "all_hour.geojson"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(parse_collection(data)), datetime.fromtimestamp(data["metadata"]["generated"] / 1000, tz=UTC)
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    return [
        Earthquake("sample1", now - timedelta(minutes=5), 38.8, -122.8, 1.5, 1.5, place="7 km NW of The Geysers, CA"),
        Earthquake("sample2", now - timedelta(minutes=40), -36.4, -98.0, 6.4, 10.0, place="southeast of Easter Island"),
    ], now


async def _headless_frame(root: Path) -> str:
    quakes, now = _frame_quakes(root)
    if not quakes:
        raise ValueError("selftest: no quakes for the headless frame")
    clock = ReplayClock(now, real=lambda: 0.0)
    store = Store()
    store.merge(Batch("usgs", now, quakes=tuple(quakes), backfill=True), clock)
    config = with_overrides(Config(), min_magnitude=-2.0, hours=24, glyphs="ascii")
    app = EarthwaveApp(config, store=store, clock=clock, run_scheduler=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        quake_list = app.dashboard.query_one(QuakeList)
        newest = max(quakes, key=lambda q: q.time)
        line = quake_list.render_line(0).text
        if newest.place[:20] not in line:
            raise ValueError(f"selftest: first row {line.strip()!r} does not show {newest.place!r}")
        frame = app.export_screenshot()
    return f"frame: {len(quake_list.rows)} rows, {len(frame)} bytes"


def selftest(root: Path = FIXTURES) -> int:
    for name in MODULES:
        importlib.import_module(name)
    print("selftest: imports ok")
    if root.is_dir():
        try:
            lines = _parse_fixtures(root)
        except (OSError, ValueError, KeyError, ET.ParseError) as exc:
            print(f"selftest: FAILED: {exc!r}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
    else:
        print("selftest: fixture parse SKIPPED (tests/fixtures not found)")
    try:
        print(f"selftest: {asyncio.run(_headless_frame(root))}")
    except Exception as exc:
        print(f"selftest: FAILED: {exc!r}", file=sys.stderr)
        return 1
    print("selftest: ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="earthwave", description="Earthquakes and tsunami alerts in your terminal.")
    parser.add_argument("--version", action="version", version=f"earthwave {__version__}")
    parser.add_argument("--selftest", action="store_true", help="check imports, parse fixtures, render one frame, exit")
    parser.add_argument("--config", metavar="PATH", help="config file (default: the user config dir)")
    parser.add_argument("--no-config", action="store_true", help="ignore the config file and use defaults")
    parser.add_argument("--min-mag", type=float, metavar="M", help="minimum magnitude shown")
    parser.add_argument("--hours", type=float, metavar="N", help="show quakes from the last N hours")
    parser.add_argument("--home", metavar="LAT,LON", help="home location for distances and near-home alerts")
    parser.add_argument("--glyphs", choices=("auto", "unicode", "ascii"), help="glyph set")
    parser.add_argument("--record", metavar="PATH", help="append every HTTP exchange to a JSONL recording")
    parser.add_argument("--replay", metavar="PATH", help="serve a recording instead of the network")
    parser.add_argument("--speed", type=float, default=1.0, metavar="N", help="replay speed multiplier (default 1)")
    parser.add_argument("--offline", action="store_true", help="fetch nothing; browse the saved history")
    parser.add_argument("--notify", action="store_true", help="force toasts and bell on (off under replay)")
    parser.add_argument("--log", metavar="PATH", help="write a rotating log file (default level WARNING)")
    parser.add_argument("--debug", action="store_true", help="DEBUG logging to the default log file")
    return parser


def open_history(config: Config, clock: Clock) -> tuple[History | None, str | None]:
    """The saved-history database and None, or None and the reason it could not be opened (the app still runs)."""
    history = History(default_path(), config.history.retain_days, clock)
    try:
        history.open()
    except (sqlite3.Error, OSError) as exc:
        logging.getLogger(__name__).warning("history unavailable (%s): %r", history.path, exc)
        return None, f"{type(exc).__name__}: {exc}"
    return history, None


def run(args: argparse.Namespace) -> int:
    if args.record and args.replay:
        print("earthwave: --record and --replay cannot be combined", file=sys.stderr)
        return 2
    if args.offline and (args.record or args.replay):
        print("earthwave: --offline cannot be combined with --record or --replay", file=sys.stderr)
        return 2
    try:
        config = Config() if args.no_config else load_config(Path(args.config) if args.config else None)
        home = parse_home(args.home) if args.home else None
        config = with_overrides(config, args.min_mag, args.hours, home, args.glyphs)
    except ConfigError as exc:
        print(f"earthwave: {exc}", file=sys.stderr)
        return 2
    try:
        log_path = setup_logging(Path(args.log) if args.log else None, args.debug)
    except OSError as exc:
        print(f"earthwave: cannot open the log file: {exc}", file=sys.stderr)
        return 2
    clock: Clock = SystemClock()
    transport: httpx.AsyncBaseTransport | None = None
    try:
        if args.replay:
            replay_path = Path(args.replay)
            clock = ReplayClock(recording_start(replay_path), args.speed)
            transport = ReplayTransport(replay_path, clock)
        elif args.record:
            transport = RecordingTransport(httpx.AsyncHTTPTransport(), Path(args.record), clock)
    except (OSError, ValueError, KeyError) as exc:
        print(f"earthwave: {exc}", file=sys.stderr)
        return 2
    if log_path:
        logging.getLogger(__name__).info("logging to %s", log_path)
    # Toasts and the bell follow the config, except under replay where only --notify turns them on.
    notifying = args.notify or (config.notify.desktop and not args.replay)
    bell = args.notify or (config.notify.bell and not args.replay)
    notifier = default_notifier() if notifying else None
    # a replayed past must not enter the real history
    history, history_error = (None, None) if args.replay else open_history(config, clock)
    graphics = resolve_graphics(config.view.map_renderer) if config.view.map else BRAILLE  # before Textual owns stdin
    app = EarthwaveApp(
        config,
        clock=clock,
        transport=transport,
        notifier=notifier,
        bell=bell,
        history=history,
        offline=args.offline,
        history_error=history_error,
        graphics=graphics,
    )
    app.run()
    return app.return_code or 0


def _attach_home_value(argv: list[str]) -> list[str]:
    """argparse takes `-33.87,151.21` for an option, so `--home -33.87,151.21` becomes `--home=-33.87,151.21`."""
    out: list[str] = []
    for arg in argv:
        if out and out[-1] == "--home" and arg.startswith("-"):
            out[-1] = f"--home={arg}"
        else:
            out.append(arg)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_attach_home_value(sys.argv[1:] if argv is None else argv))
    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
