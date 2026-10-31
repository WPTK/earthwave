"""Command line entry point."""

import argparse
import importlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from earthwave import __version__

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MODULES = ("textual", "httpx", "platformdirs", "tomli_w")


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


def selftest(root: Path = FIXTURES) -> int:
    for name in MODULES:
        importlib.import_module(name)
    print("selftest: imports ok")
    if not root.is_dir():
        print("selftest: fixture parse SKIPPED (tests/fixtures not found)")
        return 0
    try:
        lines = _parse_fixtures(root)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(f"selftest: FAILED: {exc!r}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    print("selftest: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="earthwave", description="Earthquakes and tsunami alerts in your terminal.")
    parser.add_argument("--version", action="version", version=f"earthwave {__version__}")
    parser.add_argument("--selftest", action="store_true", help="check imports and parse bundled fixtures, then exit")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
