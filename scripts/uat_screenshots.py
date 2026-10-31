"""Write SVG (and PNG, when cairosvg happens to be installed) screenshots of every UAT state at every UAT size.

    .venv\\Scripts\\python scripts\\uat_screenshots.py OUT_DIR

Uses the same deterministic apps and fixtures as tests/uat; nothing is fetched from the network. Output goes only
to OUT_DIR (created if missing) and is not meant to be committed.
"""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))  # the UAT harness reuses the unit tests' builders and fixtures

from conftest import T0  # noqa: E402
from test_observations import gauge_app  # noqa: E402
from test_ui import mock_transport  # noqa: E402
from uat.harness import SIZES, TOO_SMALL_SIZES, info_alert, kam, kam_alert_app, strip_app, week_app  # noqa: E402

from earthwave.clock import ReplayClock  # noqa: E402
from earthwave.models import Batch  # noqa: E402
from earthwave.state.events import TsunamiIssued  # noqa: E402


def merge(app, *alerts):
    events = app.store.merge(Batch("tsunamigov", app.clock.now(), alerts=tuple(alerts)), app.clock)
    app.tracker.observe(events)


def settle(app):
    app.sync_tsunami()
    app.dashboard.refresh_view()


async def dashboard(app, pilot):
    pass


async def zoom_region(app, pilot):
    await pilot.press("z")


async def zoom_home(app, pilot):
    await pilot.press("z", "z")


async def full_map(app, pilot):
    await pilot.press("M")


async def detail(app, pilot):
    await pilot.press("enter")
    await asyncio.sleep(0.3)


async def filters(app, pilot):
    await pilot.press("f")


async def help_screen(app, pilot):
    await pilot.press("question_mark")


async def tsunami_none(app, pilot):
    pass


async def tsunami_watch(app, pilot):
    app.tracker.observe([TsunamiIssued(kam(0))])
    settle(app)


async def tsunami_warning(app, pilot):
    app.tracker.observe([TsunamiIssued(kam(1))])
    settle(app)


async def tsunami_information(app, pilot):
    merge(app, info_alert())
    settle(app)


async def tsunami_cancellation(app, pilot):
    merge(app, kam(2))
    settle(app)


async def tsunami_followup(app, pilot):
    second = kam(1)
    merge(app, replace(second, id="PAAQ-4-kam004", references=(second.id,), issued=app.clock.now()))
    settle(app)


async def tsunami_screen(app, pilot):
    await pilot.press("t")


async def gauge_strip(app, pilot):
    await pilot.press("t")


async def observations(app, pilot):
    await pilot.press("o")


def kam_info():
    app = kam_alert_app(quake=False)
    app.clock.advance_to(info_alert().issued)
    return app


def kam_follow():
    app = kam_alert_app(0, 1)
    app.tracker.observe([TsunamiIssued(kam(1))])
    return app


def kam_obs_clock():
    return gauge_app(ReplayClock(T0, real=lambda: 0.0))


# name -> (app factory, driver, sizes)
STATES = {
    "dashboard": (week_app, dashboard, SIZES + TOO_SMALL_SIZES),
    "map-region": (week_app, zoom_region, SIZES),
    "map-home": (week_app, zoom_home, SIZES),
    "map-fullscreen": (week_app, full_map, SIZES),
    "detail": (lambda: week_app(transport=mock_transport([])), detail, SIZES),
    "filters": (week_app, filters, SIZES),
    "help": (week_app, help_screen, SIZES),
    "tsunami-none": (lambda: kam_alert_app(), tsunami_none, SIZES),
    "tsunami-watch": (lambda: kam_alert_app(0), tsunami_watch, SIZES),
    "tsunami-warning": (lambda: kam_alert_app(0, 1), tsunami_warning, SIZES),
    "tsunami-information": (kam_info, tsunami_information, SIZES),
    "tsunami-cancellation": (lambda: kam_alert_app(0, 1), tsunami_cancellation, SIZES),
    "tsunami-followup": (kam_follow, tsunami_followup, SIZES),
    "tsunami-screen": (lambda: kam_alert_app(0, 1), tsunami_screen, SIZES),
    "gauge-strip": (strip_app, gauge_strip, SIZES),
    "observations": (kam_obs_clock, observations, SIZES),
}


def write_png(svg: str, target: Path) -> bool:
    try:
        import cairosvg  # type: ignore[import-not-found]
    except ImportError:
        return False
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(target))
    return True


async def capture(out: Path, name: str, size: tuple[int, int]) -> Path:
    factory, driver, _sizes = STATES[name]
    app = factory()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await driver(app, pilot)
        await pilot.pause()
        svg = app.export_screenshot(title=f"{name} {size[0]}x{size[1]}")
    target = out / f"{name}_{size[0]}x{size[1]}.svg"
    target.write_text(svg, encoding="utf-8")
    write_png(svg, target.with_suffix(".png"))
    return target


async def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, (_factory, _driver, sizes) in STATES.items():
        for size in sizes:
            print(f"{name} {size[0]}x{size[1]}", end=" ... ", flush=True)
            path = await capture(out, name, size)
            print(path.name)
            count += 1
    print(f"{count} screenshots in {out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: uat_screenshots.py OUT_DIR")
    sys.exit(asyncio.run(main(Path(sys.argv[1]))))
