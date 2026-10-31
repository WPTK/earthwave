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


def _parse_fixtures(root: Path) -> list[str]:
    """Parse every captured fixture format; return one line per source."""
    lines = []
    for name in ("all_hour", "4.5_week", "significant_month", "detail"):
        data = json.loads((root / "usgs" / f"{name}.geojson").read_text(encoding="utf-8"))
        lines.append(f"usgs/{name}: {len(data.get('features', [data]))} item(s)")
    for path in sorted((root / "tsunamigov").glob("*.xml")):
        ET.parse(path)
    lines.append("tsunamigov: xml ok")
    stations = ET.parse(root / "ndbc" / "activestations.xml").getroot()
    lines.append(f"ndbc stations: {len(stations.findall('station'))}")
    dart = [r for r in (root / "ndbc" / "46402.dart").read_text(encoding="utf-8").splitlines() if r[:1] != "#"]
    lines.append(f"ndbc 46402.dart: {len(dart)} rows")
    water = json.loads((root / "coops" / "water_level_9414290.json").read_text(encoding="utf-8"))
    lines.append(f"coops water_level: {len(water['data'])} rows")
    lines.append(f"jma list: {len(json.loads((root / 'jma' / 'list.json').read_text(encoding='utf-8')))} events")
    json.loads((root / "jma" / "detail.json").read_text(encoding="utf-8"))
    return lines


def selftest(root: Path = FIXTURES) -> int:
    for name in MODULES:
        importlib.import_module(name)
    if not root.is_dir():
        print("selftest: imports ok (fixtures not present, skipping parse checks)")
        return 0
    for line in _parse_fixtures(root):
        print(line)
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
