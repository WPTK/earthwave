"""Write tests/fixtures/replay/dart_event_mode.jsonl: DART buoy 32401 going from standard to event mode.

The payloads are the captured files, not synthesized: tests/fixtures/ndbc/32401.dart holds the real 2026-10-09
event (T 2 rows from 17:00 UTC, T 3 rows at 18:11-18:15) and activestations.xml the station list. The recording
serves the 32401 file as it stood at five moments, so a replay sees a stale standard-mode batch first, then the
buoy switching to 1-minute and 15-second data with a residual pulse of about 3 cm (peak -0.031 m at 18:14:15).
Re-run after changing the cut-offs below:

    python scripts/make_gauge_fixtures.py
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
STATIONS_URL = "https://www.ndbc.noaa.gov/activestations.xml"
DART_URL = "https://www.ndbc.noaa.gov/data/realtime2/32401.dart"
DAY = "2026-10-09T"
KEEP = timedelta(hours=14)  # rows older than this before a file's newest row are dropped; the source reads 12 h

# (request time, newest row the file holds): a stale standard batch, event mode starting, the pulse, the aftermath
SERVED = [
    ("16:50:01", "10:00:00"),
    ("17:05:00", "17:04:00"),
    ("18:12:30", "18:12:30"),
    ("18:20:00", "18:20:00"),
    ("22:00:00", "22:00:00"),
]


def dart_until(text: str, newest: str) -> str:
    """The captured file as it stood when its newest row was `newest` (hh:mm:ss), keeping the last KEEP of rows."""
    stamp = f"2026 10 09 {newest.replace(':', ' ')}"  # the file's own column layout: YYYY MM DD hh mm ss
    head = datetime.fromisoformat(DAY + newest + "+00:00")
    floor = (head - KEEP).strftime("%Y %m %d %H %M %S")
    rows = [ln for ln in text.splitlines() if not ln.startswith("#") and floor <= ln[:19] <= stamp]
    return "\n".join([*[ln for ln in text.splitlines() if ln.startswith("#")], *rows]) + "\n"


def main() -> None:
    dart = (ROOT / "ndbc" / "32401.dart").read_text(encoding="utf-8")
    stations = (ROOT / "ndbc" / "activestations.xml").read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in stations if 'type="dart"' in ln]
    xml = "\n".join([stations[0], *kept, "</stations>"]) + "\n"
    records = [
        {"ts": DAY + "16:50:00Z", "method": "GET", "url": STATIONS_URL, "status": 200, "headers": {}, "body": xml}
    ]
    for at, newest in SERVED:
        records.append(
            {
                "ts": DAY + at + "Z",
                "method": "GET",
                "url": DART_URL,
                "status": 200,
                "headers": {},
                "body": dart_until(dart, newest),
            }
        )
    out = ROOT / "replay" / "dart_event_mode.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8", newline="\n")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(records)} records)")


if __name__ == "__main__":
    main()
