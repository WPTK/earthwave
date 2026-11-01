"""Re-record the README demo: the real app replaying a recorded Kamchatka quake and tsunami alert sequence.

    python scripts\record_demo.py [OUT.svg]

Runs EarthwaveApp as `python -m earthwave --replay` does, over tests/fixtures/replay, on a frozen clock stepped by
hand as the UAT tests do, so the run is repeatable. It samples Textual's own screenshot export and joins the frames
into one animated SVG (no new dependency, nothing mocked). Nothing is fetched from the network. The default output
is docs/img/demo.svg.
"""

import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RECORDING = ROOT / "tests" / "fixtures" / "replay" / "kamchatka_watch_to_warning.jsonl"
SIZE = (120, 30)
STEP = 30.0  # replay seconds the frozen clock moves between polls of the app
STEPS = 240  # two replay hours
BUSY = 20  # the watch, the warning and the quake all arrive in the first steps: one frame per step until here,
EVERY = 20  # then one frame per this many steps, as the clock runs on
INTERVAL = 0.5  # seconds each frame stays on screen in the animation
HOLD = 4  # the last frame stays on screen this many extra intervals

from earthwave.app import EarthwaveApp  # noqa: E402
from earthwave.clock import ReplayClock  # noqa: E402
from earthwave.config import Config, with_overrides  # noqa: E402
from earthwave.net import ReplayTransport, recording_start  # noqa: E402

_BODY = re.compile(r"<svg[^>]*>(.*)</svg>", re.S)
_ROOT = re.compile(r"<svg([^>]*)>")


async def sample() -> list[str]:
    """Play the recording with a frozen replay clock that only this loop and the recorded responses move."""
    clock = ReplayClock(recording_start(RECORDING), real=lambda: 0.0)
    config = with_overrides(Config(), 2.5, 24, None, "unicode")
    config = replace(config, view=replace(config.view, time_zone="UTC"))
    app = EarthwaveApp(config, clock=clock, transport=ReplayTransport(RECORDING, clock), history=None)
    frames: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for step in range(STEPS):
            clock.advance(STEP)
            await asyncio.sleep(0.25 if step < BUSY else 0.05)  # the polls need real time to settle
            if step < BUSY or step % EVERY == 0:
                await pilot.pause()
                frames.append(app.export_screenshot(title="earthwave --replay (recorded data)"))
    return frames


def join(frames: list[str]) -> str:
    """One SVG that shows each distinct frame in turn; a repeated frame just stays on screen longer."""
    runs: list[list] = []  # [frame, ticks]
    for frame in frames:
        if runs and runs[-1][0] == frame:
            runs[-1][1] += 1
        else:
            runs.append([frame, 1])
    runs[-1][1] += HOLD
    total = sum(ticks for _f, ticks in runs) * INTERVAL
    attrs = _ROOT.search(runs[0][0]).group(1)  # type: ignore[union-attr]
    out = [f"<svg{attrs}>", f'<set id="loop" attributeName="x" to="0" begin="0s;loop.end" dur="{total:.2f}s"/>']
    at = 0.0
    for n, (frame, ticks) in enumerate(runs):
        body = _BODY.search(frame).group(1)  # type: ignore[union-attr]
        out.append(f'<g display="{"inline" if n == 0 else "none"}">')
        out.append(
            f'<set attributeName="display" to="inline" begin="loop.begin+{at:.2f}s" dur="{ticks * INTERVAL:.2f}s"/>'
        )
        out.append(body)
        out.append("</g>")
        at += ticks * INTERVAL
    out.append("</svg>")
    return "\n".join(out)


def main(target: Path) -> None:
    frames = asyncio.run(sample())
    target.parent.mkdir(parents=True, exist_ok=True)
    svg = join(frames)
    target.write_text(svg, encoding="utf-8")
    distinct = len(set(frames))
    print(f"{target}: {len(frames)} samples, {distinct} distinct frames, {len(svg) // 1024} KB")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "img" / "demo.svg")
