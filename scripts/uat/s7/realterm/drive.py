"""Generic driver: run any console app in a real Windows terminal, play a script of inputs, read the screen back.

    python drive.py {wt,conhost,vscode} --out DIR --cmd "C:\\path\\app.exe --flag" --steps steps.json
        [--tool-exe PATH]        Windows Terminal or Code.exe (not needed for conhost)
        [--scheme dark|light] [--medium-spawn] [--env NAME=VALUE ...] [--codepage 437] [--font-pt 12]
        [--profile cmd|powershell]  (vscode)   [--tag NAME]

``--cmd`` is the app command line (one string, run through ``cmd.exe``). ``steps.json`` is a list of objects, run in order;
a failing ``wait``/``expect`` is recorded and the run goes on, except that the app must have drawn for pixel steps to work:

    {"wait": "regex", "timeout": 30}          poll the screen text until the regex matches
    {"expect": "regex"} / {"expect_not": "regex"}   assert on the current screen text
    {"keys": "text"}                          type text as real key presses
    {"key": "enter|escape|tab|up|down|left|right|pgup|pgdn"}
    {"click": "text", "button": "left|right|middle", "double": false}   click the first occurrence of text on screen
    {"hover": "text"}  {"wheel": "text", "notches": -3}  {"drag": ["from text", "to text"]}
    {"click_xy": [x, y]}                      absolute screen pixels
    {"read": "tag"}                           save the screen text as DIR/<terminal>-<tag>.txt (and print it)
    {"shot": "tag"}                           save DIR/<terminal>-<tag>-window.png (+ printwindow, desktop)
    {"sleep": 1.5}
    {"quit": "q"}                             send the key and wait for the app to exit

Output: ``DIR/result-<terminal>-drive.json`` (steps with ok/detail), the .txt and .png files named above, exit code 0 when
every recorded check passed. The terminal is torn down by PID at the end.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_spike as rs
import winterm as w

KEYS = {
    "enter": 0x0D,
    "escape": 0x1B,
    "tab": 0x09,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "pgup": 0x21,
    "pgdn": 0x22,
}


def center(t: rs.Term, text: str) -> tuple[int, int]:
    rect = None
    for _ in range(3):
        rect = t.rect_of(text)
        if rect:
            break
        time.sleep(0.7)
    if not rect:
        raise LookupError(f"text {text!r} not found on screen")
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def run_step(t: rs.Term, rep: rs.Report, step: dict, n: int) -> None:
    w.focus(t.hwnd)
    if "wait" in step:
        rx = step["wait"]
        try:
            w.wait_for(lambda: re.search(rx, t.read()), float(step.get("timeout", 30)), f"/{rx}/")
            rep.check(f"wait {rx!r}", True)
        except TimeoutError:
            rep.check(f"wait {rx!r}", False, t.read()[-300:])
    elif "expect" in step:
        rep.check(f"expect {step['expect']!r}", bool(re.search(step["expect"], t.read())))
    elif "expect_not" in step:
        rep.check(f"expect_not {step['expect_not']!r}", not re.search(step["expect_not"], t.read()))
    elif "keys" in step:
        w.send_text(step["keys"])
        time.sleep(float(step.get("settle", 1.0)))
    elif "key" in step:
        w.tap(KEYS[step["key"]])
        time.sleep(float(step.get("settle", 1.0)))
    elif "click" in step:
        x, y = center(t, step["click"])
        button = step.get("button", "left")
        if step.get("double"):
            w.double_click_at(x, y)
        else:
            w.click_at(x, y, button=button)
        time.sleep(float(step.get("settle", 1.0)))
    elif "click_xy" in step:
        w.click_at(*step["click_xy"], button=step.get("button", "left"))
        time.sleep(float(step.get("settle", 1.0)))
    elif "hover" in step:
        w.hover_at(*center(t, step["hover"]))
    elif "wheel" in step:
        w.wheel_at(*center(t, step["wheel"]), int(step.get("notches", -3)))
        time.sleep(float(step.get("settle", 1.0)))
    elif "drag" in step:
        (x1, y1), (x2, y2) = center(t, step["drag"][0]), center(t, step["drag"][1])
        w.drag_between(x1, y1, x2, y2)
        time.sleep(float(step.get("settle", 1.0)))
    elif "read" in step:
        text = t.read()
        t.save_text(step["read"], text)
        print(text)
        rep.note(f"read {step['read']}", f"{len(text.splitlines())} lines")
    elif "shot" in step:
        t.shot(step["shot"], printwindow=True)
    elif "sleep" in step:
        time.sleep(float(step["sleep"]))
    elif "quit" in step:
        w.send_text(step["quit"])
        time.sleep(0.5)
        try:
            t.quit_check()
            rep.check("app exited", True)
        except TimeoutError as exc:
            rep.check("app exited", False, str(exc))
    else:
        rep.check(f"step {n} known", False, json.dumps(step))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("terminal", choices=sorted(rs.TERMS))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cmd", required=True, help="the app command line")
    ap.add_argument("--steps", required=True, type=Path)
    ap.add_argument("--tool-exe", type=Path)
    ap.add_argument("--scheme", choices=["dark", "light"], default="dark")
    ap.add_argument(
        "--medium-spawn", action="store_true", help="start the terminal at Medium integrity (driver stays as is)"
    )
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--codepage", type=int)
    ap.add_argument("--font-pt", type=int)
    ap.add_argument("--profile", choices=["cmd", "powershell"], default="cmd")
    ap.add_argument("--first-frame", default="", help="regex that means the app has drawn; default: just wait 3 s")
    ap.add_argument("--tag", default="drive")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rep = rs.Report(f"{args.terminal}-{args.tag}", args.out)
    rep.info = rs.diagnostics(args.out)
    env = dict(e.split("=", 1) for e in args.env)
    t = rs.TERMS[args.terminal](
        rep,
        Path(args.cmd.split()[0].strip('"')),
        args.out,
        args.tool_exe.resolve() if args.tool_exe else None,
        False,
        args.scheme,
        medium_spawn=args.medium_spawn,
        app_cmd=args.cmd,
        env_extra=env,
        codepage=args.codepage,
        font_pt=args.font_pt,
        profile=args.profile,
        probe=False,
        tag_suffix=args.tag,
    )
    try:
        t.launch()
        t.prepare_window()
        if args.first_frame:
            w.wait_for(lambda: re.search(args.first_frame, t.read()), 60, f"first frame /{args.first_frame}/")
        else:
            time.sleep(3)
        for n, step in enumerate(json.loads(args.steps.read_text(encoding="utf-8"))):
            run_step(t, rep, step, n)
    except rs.Abort as exc:
        w.log(f"aborted at: {exc}")
    except Exception:
        rep.check("unexpected error", False, traceback.format_exc()[-1500:])
    finally:
        with contextlib.suppress(Exception):
            t.shot("99-end")
        with contextlib.suppress(Exception):
            t.teardown()
        rep.write()
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
