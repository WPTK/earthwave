"""Save one real capture of the feeds, headless: the app's own code and its own RecordingTransport, no terminal needed.

    python scripts/uat/capture.py tests/uat/corpus/2026-11-01.jsonl [seconds]

The capture is every HTTP exchange of the first poll rounds, in the format ``--replay`` reads. Public read-only GETs only.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

from earthwave.app import EarthwaveApp
from earthwave.clock import SystemClock
from earthwave.config import Config
from earthwave.net import RecordingTransport
from earthwave.ui.graphics.probe import BRAILLE


async def capture(path: Path, seconds: float) -> None:
    clock = SystemClock()
    transport = RecordingTransport(httpx.AsyncHTTPTransport(), path, clock)
    app = EarthwaveApp(Config(), clock=clock, transport=transport, graphics=BRAILLE)
    async with app.run_test(size=(130, 32)) as pilot:
        await pilot.pause(seconds)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    asyncio.run(capture(out, float(sys.argv[2]) if len(sys.argv) > 2 else 45.0))
    print(f"{out}: {sum(1 for _ in out.open(encoding='utf-8'))} exchanges")
