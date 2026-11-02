"""L5 contrast, L6 size sweep, L7 first inputs: what a person looking at the screen would notice, computed from the cells
and text of real runs. Report-only: each writes its list to ``$UAT_OUT`` and prints a count; a non-empty list is an
xfail (non-strict) so the run stays green and the report carries the numbers."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

import pytest

from uat.known import report as report_
from uat.replays import all_quiet
from uat.target import BASE, KAM, USGS, launch, needs_kit, ready

pytestmark = needs_kit

OUT = Path(os.environ.get("UAT_OUT", "."))


def write(name: str, lines: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text("\n".join(lines), encoding="utf-8")
    print(f"{name}: {len(lines)} lines")
    for ln in lines[:30]:
        print("  ", ln)


# -- screens -------------------------------------------------------------------------------------------------------


def go(t, screen: str) -> None:
    if screen == "dashboard":
        return
    keys = {"detail": ("enter",), "filters": ("f",), "help": ("?",), "tsunami": ("t",), "gauges": ("o",)}
    if screen == "map":
        t.send_raw("M")
    else:
        t.send_keys(*keys[screen])
    t.settle(1.0)


SCREENS = ["dashboard", "detail", "filters", "help", "tsunami", "gauges", "map", "empty"]


def run_screen(screen: str, size: tuple[int, int], tmp_path: Path, then):
    if screen == "tsunami":
        args = ["--replay", str(KAM), "--speed", "20"]
    elif screen == "empty":
        args = ["--replay", str(all_quiet().save(tmp_path / "quiet.jsonl")), "--speed", "60"]
    else:
        args = ["--replay", str(USGS), *BASE]
    with launch(*args, size=size) as t:
        marker = {"empty": "No quakes match", "tsunami": "TSUNAMI"}.get(screen, "shown of")
        t.wait_for(
            lambda s: marker in s.text or "Terminal too small" in s.text, 90, f"{screen} (or the too-small message)"
        )
        if "Terminal too small" not in t.screen_text():
            t.settle(1.0)
            if screen in ("tsunami",):
                go(t, "tsunami")
            elif screen not in ("empty", "dashboard"):
                go(t, screen)
        t.settle(0.8)
        return then(t)


# -- L5: contrast --------------------------------------------------------------------------------------------------

PROFILES = {"dark": ("#e5e5e5", "#1e1e1e"), "light": ("#1e1e1e", "#ffffff")}


def rgb(c: str) -> tuple[float, float, float]:
    return tuple(int(c[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def lin(v: float) -> float:
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def lum(c: str) -> float:
    r, g, b = (lin(v) for v in rgb(c))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, k: float) -> str:
    ra, rb = rgb(a), rgb(b)
    return "#" + "".join(f"{round((x * k + y * (1 - k)) * 255):02x}" for x, y in zip(ra, rb, strict=True))


def styles(t, profile: str) -> dict[tuple, list[str]]:
    dfg, dbg = PROFILES[profile]
    found: dict[tuple, list[str]] = defaultdict(list)
    for row in t.cells():
        for c in row:
            if not c.char.strip():
                continue
            fg = dfg if str(c.fg) == "default" else str(c.fg)
            bg = dbg if str(c.bg) == "default" else str(c.bg)
            if c.inverse:
                fg, bg = bg, fg
            if c.dim:
                fg = mix(fg, bg, 0.6)
            found[(fg, bg, bool(c.bold), bool(c.dim))].append(c.char)
    return found


def decorative(chars: list[str]) -> bool:
    """Box drawing, braille, blocks, bars and dots: shapes, not text (3:1)."""
    return all(not ch.isalnum() for ch in chars)


def test_l5_contrast_per_text_style(tmp_path: Path) -> None:
    low: dict[tuple, dict] = {}
    total = 0
    for screen in ("dashboard", "detail", "filters", "help", "tsunami", "gauges", "empty"):

        def both(t):  # both profiles from one run: the same cells, default colours replaced by each profile's
            return {p: styles(t, p) for p in PROFILES}

        data = run_screen(screen, (130, 32), tmp_path, both)
        for prof, found in data.items():
            for (fg, bg, bold, dim), chars in found.items():
                total += 1
                need = 3.0 if decorative(chars) else 4.5
                r = ratio(fg, bg)
                if r < need:
                    key = (prof, fg, bg, bold, dim, need)
                    entry = low.setdefault(key, {"r": r, "screens": set(), "sample": "".join(chars[:14]).strip()})
                    entry["screens"].add(screen)
    lines = [f"{total} (profile,style) pairs over 7 screens; {len(low)} below threshold"]
    for (prof, fg, bg, bold, dim, need), e in sorted(low.items(), key=lambda kv: kv[1]["r"]):
        lines.append(
            f"{prof}: fg {fg} on {bg} bold={bold} dim={dim} = {e['r']:.2f}:1 (need {need}) on {sorted(e['screens'])} e.g. {e['sample']!r}"
        )
    write("l5-contrast.txt", lines)
    report_("text styles below 4.5:1 (3:1 decorative) in the dark or light profile (l5-contrast.txt)", low)


# -- L6: size sweep ------------------------------------------------------------------------------------------------

SIZES = [(80, 24), (100, 30), (120, 40), (250, 40)]


def layout_findings(text: str, cols: int, rows: int) -> list[str]:
    out = []
    lines = text.splitlines()
    if "Terminal too small" in text:
        return out
    for i, ln in enumerate(lines):
        if re.search(r"\d…|…\d", ln):
            out.append(f"row {i}: a number is cut by an ellipsis: {ln.strip()[:90]!r}")
        if ln.count("╭") != ln.count("╮") or ln.count("╰") != ln.count("╯"):
            out.append(f"row {i}: a panel border is cut: {ln.strip()[:90]!r}")
        if re.search(r"╭─ [^─╮]*…", ln):
            out.append(f"row {i}: a panel title is clipped: {ln.strip()[:90]!r}")
    body = lines[:-1]
    run = best = 0
    for ln in body:
        run = run + 1 if not ln.strip() else 0
        best = max(best, run)
    if best >= max(6, rows // 4):
        out.append(f"{best} blank rows in a row (reserved empty space)")
    used = max((len(ln.rstrip()) for ln in lines), default=0)
    if cols >= 200 and used < cols * 0.6:
        out.append(f"only {used} of {cols} columns are used on a wide terminal")
    return out


def test_l6_size_sweep_on_every_screen(tmp_path: Path) -> None:
    report = []
    cells = 0
    for screen in SCREENS:
        for size in SIZES:
            cells += 1
            text = run_screen(screen, size, tmp_path, lambda t: t.screen_text())
            if "Terminal too small" in text:
                m = re.search(r"too small: (\d+)x(\d+)", text)
                if size[0] >= 100 and size[1] >= 30:
                    report.append(f"{screen} {size[0]}x{size[1]}: says too small although README says 100x30 is enough")
                elif not (m and (int(m[1]), int(m[2])) == size):
                    report.append(f"{screen} {size[0]}x{size[1]}: the too-small message names the wrong size")
                continue
            for f in layout_findings(text, *size):
                report.append(f"{screen} {size[0]}x{size[1]}: {f}")
    write("l6-sizes.txt", [f"{cells} screen/size cells, {len(report)} findings", *report])
    report_("layout findings across screens and sizes (l6-sizes.txt)", report)


# -- L7: a fresh profile, first input -------------------------------------------------------------------------------


def signature(t) -> list:
    return [[(c.char, str(c.fg), str(c.bg), bool(c.inverse), bool(c.bold)) for c in row] for row in t.cells()]


FIRST_INPUTS = ["enter", "tab", "down", "click", "?", "q"]


@pytest.mark.parametrize("first", FIRST_INPUTS)
def test_l7_first_input_on_a_fresh_profile_does_something_visible(first: str) -> None:
    with launch("--replay", str(USGS), *BASE) as t:  # --no-config and an isolated profile: nothing remembered
        ready(t)
        before = signature(t)
        if first == "click":
            from uat.target import list_rows

            row = list_rows(t.screen().lines)[2][0]
            t.click(10, row)
        elif first == "?":
            t.send_keys("?")
        else:
            t.send_keys(first)
        t.settle(1.0)
        if first == "q":
            assert t.wait_exit(10) is not None, "q did not end the program"
            return
        assert signature(t) != before, f"{first!r} as the first input changed nothing a person could see"
