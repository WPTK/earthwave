"""S7 closure: does earthwave 0.2.0 pick and draw sixel in a REAL Windows terminal, proven by screen pixels?

    python run_wt.py {wt,conhost} --exe EARTHWAVE.exe --tool-exe WindowsTerminal.exe --replay usgs_short.jsonl --out DIR

Uses the vendored real-terminal driver (realterm/, copied from WPTK/wxbrief tests/uat_realterm at bc62698). Per terminal:
  1. probe     da1_probe.py in the terminal: the exact reply to the app's own query (CSI 16t, 14t, c), escaped.
  2. auto      the unmodified exe, config map_renderer = "auto", `--debug --log`: dashboard, then the full-screen map (M).
               The log line "map renderer: ..." says what the app chose; the window PNG and the terminal's own text say what
               was drawn.
  3. braille   control: the same run with the config forcing map_renderer = "braille".
  4. analyze   pixel comparison of the two map PNGs, braille glyph counts from the terminal text.
Nothing in the app is changed; the config file is the user-facing setting. Exit 0 always (the verdict is in result.json):
the caller reads it, a harness error is a nonzero exit with a message.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVE = HERE / "realterm" / "drive.py"
BRAILLE_RE = re.compile("[⠁-⣿]")  # braille patterns other than the blank cell


def drive(term: str, out: Path, tag: str, cmd: str, steps: list[dict], tool_exe: Path | None, first_frame: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    steps_file = out / f"{tag}-steps.json"
    steps_file.write_text(json.dumps(steps), encoding="utf-8")
    argv = [sys.executable, str(DRIVE), term, "--out", str(out), "--cmd", cmd, "--steps", str(steps_file), "--tag", tag]
    if tool_exe:
        argv += ["--tool-exe", str(tool_exe)]
    if first_frame:
        argv += ["--first-frame", first_frame]
    print(">>", " ".join(argv), flush=True)
    return subprocess.run(argv, check=False).returncode


def newest(out: Path, pattern: str) -> Path | None:
    hits = sorted(out.glob(pattern), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("terminal", choices=["wt", "conhost"])
    ap.add_argument("--exe", required=True, type=Path)
    ap.add_argument("--tool-exe", type=Path)
    ap.add_argument("--replay", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    term, out = args.terminal, args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = out / f"launch-{term}"  # the driver tears down by PID and by executable folder: keep only launchers here
    work.mkdir(exist_ok=True)
    result: dict[str, object] = {"terminal": term, "exe": str(args.exe), "harness_errors": []}

    # 1. what the terminal answers to the app's own probe
    probe_json = out / f"{term}-da1.json"
    launcher = work / "probe.cmd"
    launcher.write_text(
        f'@echo off\r\n"{sys.executable}" "{HERE / "da1_probe.py"}" "{probe_json}"\r\n', encoding="ascii"
    )
    rc = drive(
        term,
        out / f"{term}-probe",
        "probe",
        f'"{launcher}"',
        [{"wait": "DA1PROBE DONE", "timeout": 60}, {"read": "probe-screen"}, {"shot": "probe"}, {"sleep": 1}],
        args.tool_exe,
        "",
    )
    result["probe_driver_exit"] = rc
    if probe_json.is_file():
        result["probe"] = json.loads(probe_json.read_text(encoding="utf-8"))
    else:
        result["harness_errors"].append("da1 probe wrote no json")  # type: ignore[union-attr]

    # 2 + 3. the exe, auto and forced-braille control
    runs: dict[str, dict] = {}
    for name in ("auto", "braille"):
        cfg = work / f"{name}.toml"
        cfg.write_text(f'[view]\nmap_renderer = "{name}"\n', encoding="utf-8")
        log = out / f"{term}-{name}-app.log"
        log.unlink(missing_ok=True)
        cmd = f'"{args.exe}" --config "{cfg}" --log "{log}" --debug --replay "{args.replay}" --hours 720 --min-mag 2.5'
        steps = [
            {"wait": r"[1-9]\d* shown of", "timeout": 60},
            {"sleep": 3},
            {"read": "dash"},
            {"shot": "dash"},
            {"keys": "M", "settle": 4},
            {"read": "map"},
            {"shot": "map"},
            {"quit": "q"},
        ]
        rc = drive(term, out / f"{term}-{name}", name, cmd, steps, args.tool_exe, r"shown of")
        info: dict[str, object] = {"driver_exit": rc}
        text = newest(out / f"{term}-{name}", "*map*.txt")
        png = newest(out / f"{term}-{name}", "*map-window.png")
        if text:
            braille_cells = len(BRAILLE_RE.findall(text.read_text(encoding="utf-8", errors="replace")))
            info["braille_glyphs_in_terminal_text"] = braille_cells
            info["text"] = text.name
        if png:
            info["png"] = str(png)
        if log.is_file():
            lines = [
                ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if "map renderer" in ln
            ]
            info["app_log_renderer_lines"] = lines
        else:
            result["harness_errors"].append(f"{name}: no app log")  # type: ignore[union-attr]
        runs[name] = info
    result["runs"] = runs

    # 4. pixels
    a, b = runs["auto"].get("png"), runs["braille"].get("png")
    if a and b:
        sys.path.insert(0, str(HERE))
        import analyze

        result["pixels"] = {
            "auto": analyze.area_stats(Path(a)),
            "braille": analyze.area_stats(Path(b)),
        }
        result["pixels"].update(analyze.verdict(result["pixels"]["auto"], result["pixels"]["braille"]))  # type: ignore[attr-defined]
    else:
        result["harness_errors"].append("missing map PNG")  # type: ignore[union-attr]
    (out / f"result-{term}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 1 if result["harness_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
