"""Seeded defects: does the UAT suite notice when the app breaks in a known way?

    python scripts/uat/mutants.py --list
    python scripts/uat/mutants.py --run B1 B2 R1            # or --run all; --class bindings
    python scripts/uat/mutants.py --run all --json mutants.json

Each mutant is one exact text substitution in earthwave/. The package is copied to a temporary directory, the mutation
applied there (the working tree is never touched, nothing is committed or pushed), and the mutant's killer test is
run with that copy first on PYTHONPATH. A mutant is KILLED when the killer fails with the mutant and passed on the
clean copy just before; SURVIVED when it still passes (a hole in the suite); BASELINE-FAIL when the killer fails even
on the clean copy (the result says nothing about the mutant). Real-input killers need UAT_REAL=1 and the lock rules in
docs/uat.md apply to the machine they run on.

Classes (the plan asks for at least ten per class; the count per class is printed by --list): bindings, mouse, state,
data, rendering, packaging. ``packaging`` mutants need a wheel build and are not in this file yet.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
J = "tests/uat/test_journeys.py"


@dataclass(frozen=True)
class Mutant:
    id: str
    cls: str
    file: str
    old: str
    new: str
    killer: str  # pytest args
    why: str


def m(id, cls, file, old, new, killer, why):
    return Mutant(id, cls, file, old, new, killer, why)


DASH = "earthwave/ui/screens/dashboard.py"
MUTANTS = [
    m("B1", "bindings", DASH, 'Binding("f", "filters", "Filters")', 'Binding("g", "filters", "Filters")', f"{J} -k j6", "f no longer opens filters"),
    m("B2", "bindings", DASH, 'Binding("s", "sort", "Sort")', 'Binding("x", "sort", "Sort")', f"{J} -k j8", "s no longer sorts"),
    m("B3", "bindings", DASH, 'Binding("slash", "search", "Search")', 'Binding("backslash", "search", "Search")', f"{J} -k j7", "/ no longer searches"),
    m("B4", "bindings", DASH, 'Binding("m", "toggle_map", "Map")', 'Binding("n", "toggle_map", "Map")', f"{J} -k j9", "m no longer toggles the map"),
    m("B5", "bindings", DASH, 'Binding("z", "zoom_map", "Zoom", show=False)', 'Binding("x", "zoom_map", "Zoom", show=False)', f"{J} -k j9", "z no longer zooms"),
    m("B6", "bindings", DASH, 'Binding("t", "tsunami", "Tsunami")', 'Binding("y", "tsunami", "Tsunami")', f"{J} -k j13", "t no longer opens the tsunami screen"),
    m("B7", "bindings", DASH, 'Binding("question_mark", "help", "Help")', 'Binding("exclam", "help", "Help")', f"{J} -k j10", "? no longer opens help"),
    m("B8", "bindings", "earthwave/ui/screens/detail.py", 'Binding("escape", "back", "Back")', 'Binding("x", "back", "Back")', f"{J} -k j5", "Escape no longer leaves the detail"),
    m("B9", "bindings", "earthwave/ui/widgets/quake_list.py", 'Binding("j,down", "cursor_down", "Down", show=False)', 'Binding("down", "cursor_down", "Down", show=False)', f"{J} -k j5", "j no longer moves down"),
    m("B10", "bindings", "earthwave/ui/widgets/quake_list.py", 'Binding("end", "cursor_last", "Last", show=False)', 'Binding("insert", "cursor_last", "Last", show=False)', f"{J} -k j5", "End no longer jumps to the last row"),
    m("B11", "bindings", "earthwave/app.py", 'BINDINGS = [Binding("q", "quit", "Quit")]', 'BINDINGS = [Binding("ctrl+x", "quit", "Quit")]', f"{J} -k j1", "q no longer quits"),
    m("B12", "bindings", "earthwave/ui/screens/filters.py", 'Binding("escape", "cancel", "Cancel")', 'Binding("x", "cancel", "Cancel")', f"{J} -k j6", "Escape no longer cancels the filters"),
    m("B13", "bindings", "earthwave/ui/screens/help.py", 'Binding("escape,question_mark", "close", "Close")', 'Binding("escape", "close", "Close")', f"{J} -k j10", "? no longer closes help"),
    m("M1", "mouse", "earthwave/ui/widgets/quake_list.py", "            self._move(index)\n\n", "            self._move(index + 1)\n\n", f"{J} -k j5b", "a click selects the row below the one clicked"),
    m("D1", "data", "earthwave/ui/widgets/quake_list.py", "{quake.mag:{MAG_W}.1f}", "{quake.mag:{MAG_W}.0f}", f"{J} -k j8", "magnitude shown without its decimal"),
    m("D2", "data", "earthwave/ui/widgets/status_bar.py", "shown of {info.total}", "showing of {info.total}", f"{J} -k j1", "status count wording changed"),
    m("R1", "rendering", "earthwave/ui/widgets/too_small.py", "MIN_WIDTH = 100", "MIN_WIDTH = 200", f"{J} -k j1", "the dashboard is refused at 130 columns"),
    m("R2", "rendering", "earthwave/ui/widgets/too_small.py", "MIN_HEIGHT = 30", "MIN_HEIGHT = 40", f"{J} -k j1", "the dashboard is refused at 32 rows"),
    m("R3", "rendering", "earthwave/ui/widgets/status_bar.py", "if info.sources and not unhealthy:", "if info.sources:", f"{J} -k j17", "'feeds ok' shown beside a failing feed"),
    m("R4", "rendering", "earthwave/ui/widgets/tsunami_panel.py", 'return Text("No tsunami alerts in force", style="grey50")', 'return Text("Nothing in force", style="grey50")', f"{J} -k j16", "the quiet-day tsunami panel text changed"),
    m("R5", "rendering", "earthwave/ui/screens/detail.py", '_row(text, "Event page", quake.url, "underline")', '_row(text, "Page", quake.url, "underline")', f"{J} -k j5", "the detail screen lost its Event page row"),
]  # fmt: skip


def apply(mutant: Mutant, tree: Path) -> None:
    path = tree / mutant.file
    text = path.read_text(encoding="utf-8")
    if text.count(mutant.old) != 1:
        raise SystemExit(
            f"{mutant.id}: expected exactly one occurrence of {mutant.old!r} in {mutant.file}, found {text.count(mutant.old)}"
        )
    path.write_text(text.replace(mutant.old, mutant.new), encoding="utf-8")


def run_killer(killer: str, src: Path) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(src), UAT_REAL="1", PYTHONUTF8="1")
    env.pop("NO_COLOR", None)
    env.pop("EARTHWAVE_EXE", None)  # a mutant is a change to the source; an exe cannot carry it
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *killer.split(),
        "-p",
        "no:cacheprovider",
        "-x",
        "-q",
        "--tb=line",
        "--no-header",
    ]
    got = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    return got.returncode, (got.stdout + got.stderr)[-600:]


def run_one(mutant: Mutant) -> dict:
    with tempfile.TemporaryDirectory(prefix="ew-mut-") as tmp:
        src = Path(tmp)
        shutil.copytree(ROOT / "earthwave", src / "earthwave", ignore=shutil.ignore_patterns("__pycache__"))
        base_code, base_out = run_killer(mutant.killer, src)
        if base_code != 0:
            return {"id": mutant.id, "result": "BASELINE-FAIL", "tail": base_out}
        apply(mutant, src)
        code, out = run_killer(mutant.killer, src)
        return {"id": mutant.id, "class": mutant.cls, "why": mutant.why, "result": "SURVIVED" if code == 0 else "KILLED",
                "tail": "" if code == 0 else out.strip().splitlines()[-1] if out.strip() else ""}  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", nargs="+", metavar="ID")
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--json")
    args = ap.parse_args()
    chosen = MUTANTS
    if args.cls:
        chosen = [x for x in chosen if x.cls == args.cls]
    if args.run and args.run != ["all"]:
        chosen = [x for x in chosen if x.id in args.run]
    if args.list or not args.run:
        for x in chosen:
            print(f"{x.id:4} {x.cls:10} {x.file}: {x.why}")
        counts = {
            c: sum(1 for x in MUTANTS if x.cls == c)
            for c in ("bindings", "mouse", "state", "data", "rendering", "packaging")
        }
        print(f"{len(MUTANTS)} mutants; per class {counts}")
        return 0
    results = []
    for x in chosen:
        res = run_one(x)
        results.append(res)
        print(f"{res['id']:4} {res['result']:13} {x.why}  {res.get('tail', '')[:120]}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    survived = [r["id"] for r in results if r["result"] == "SURVIVED"]
    broken = [r["id"] for r in results if r["result"] == "BASELINE-FAIL"]
    print(
        f"killed {sum(r['result'] == 'KILLED' for r in results)}/{len(results)}; survived {survived}; baseline-fail {broken}"
    )
    return 1 if survived or broken else 0


if __name__ == "__main__":
    sys.exit(main())
