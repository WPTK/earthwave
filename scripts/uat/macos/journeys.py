"""earthwave input journeys on Terminal.app or iTerm2 of a GitHub macOS runner, with real keys and a real mouse click.

Method vendored from eyewall's spike S4 (docs: C:\\Users\\billp\\review-findings\\uat-shared\\spikes\\macos-input\\README.md;
`grant.sh`, `dismiss.py`, `iterm_readback.py`, `drive.py` in this folder are copied unchanged from eyewall branch
spike/s4-macos-input). Keys: cliclick (a real keyboard event). Mouse: cliclick move + click (a real mouse event).
Read-back: Terminal.app `contents of selected tab`, iTerm2 `async_get_screen_contents`. No tmux, no pty writes.

    OUT=dir S4=scripts/uat/macos S4PY=~/s4venv/bin/python EXE=./earthwave REPLAY=tests/fixtures/replay/usgs_short.jsonl \
        python journeys.py Terminal|iTerm2

One pass, no retries. Writes $OUT/drive.log (PASS/FAIL per step), $OUT/result.json, a window screenshot per step and the
read-back text per step. CI runner only (it kills Terminal and iTerm2 and takes over the keyboard).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

OUT = os.environ["OUT"]
S4 = os.environ["S4"]
PY = os.environ.get("S4PY", sys.executable)
EXE = os.environ["EXE"]
REPLAY = os.environ["REPLAY"]
TERM = sys.argv[1]
OWNER = {"Terminal": "Terminal", "iTerm2": "iTerm2"}[TERM]
APP = {"Terminal": "Terminal", "iTerm2": "iTerm"}[TERM]
NL = chr(10)
RES: dict = {"terminal": TERM, "steps": []}
ROW = re.compile(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d")
SELECTED = re.compile(r"^[▌>]\s*\d+[smhd]\s+\d\.\d")


def step(name: str, ok: bool, **kw) -> None:
    RES["steps"].append({"name": name, "ok": ok, **kw})
    print(("PASS " if ok else "FAIL ") + name, kw, flush=True)
    with open(os.path.join(OUT, "result.json"), "w") as f:
        json.dump(RES, f, indent=1, default=str)


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 99, repr(e)


def osa(script: str):
    return sh(["osascript", "-e", script])


def activate() -> None:
    osa(f'tell application "{APP}" to activate')
    time.sleep(0.6)


def window():
    import Quartz

    best = None
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
        if w.get("kCGWindowOwnerName") == OWNER and w.get("kCGWindowLayer") == 0:
            b = w["kCGWindowBounds"]
            area = b["Width"] * b["Height"]
            if area > 20000 and (best is None or area > best[0]):
                best = (area, int(w["kCGWindowNumber"]), (b["X"], b["Y"], b["Width"], b["Height"]))
    return best[1:] if best else (None, None)


def readback() -> str:
    if TERM == "Terminal":
        rc, txt = osa('tell application "Terminal" to get contents of selected tab of front window')
    else:
        rc, txt = sh([PY, os.path.join(S4, "iterm_readback.py")], timeout=40)
    return txt


def shot(name: str) -> None:
    wid, _ = window()
    if wid:
        sh(f'screencapture -x -o -l {wid} "{OUT}/{name}_window.png"')


def settle(seconds: float = 1.2) -> str:
    time.sleep(seconds)
    return readback()


def record(name: str, text: str) -> None:
    with open(os.path.join(OUT, f"readback-{name}.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    shot(name)


def key(*keys: str) -> None:
    activate()
    for k in keys:
        sh(["cliclick", f"kp:{k}"])
        time.sleep(0.15)


def typed(text: str) -> None:
    activate()
    sh(["cliclick", f"t:{text}"])


def selected(text: str) -> str | None:
    for ln in text.splitlines():
        if SELECTED.match(ln[:72]):
            return re.sub(r"^\S?\s*\d+[smhd]\s+", "", ln[:72].rstrip())
    return None


def rows(text: str) -> list[tuple[int, str]]:
    return [(i, ln[:72].rstrip()) for i, ln in enumerate(text.splitlines()) if ROW.match(ln[:72])]


def macos_major() -> int:
    return int(sh("sw_vers -productVersion")[1].split(".")[0])


# swept content origin (offset from the window's top-left, points) and cell size, from S4b; the S4 formula is the default
SWEPT = {
    ("Terminal", 26): (7.85, 38.74, 7.15, 15.26),
    ("Terminal", 15): (2.96, 30.0, 7.04, 14.0),
    ("iTerm2", 26): (4.96, 35.0, 7.04, 17.0),
}


def cell_px(win, cols: int, rows_: int, c: int, r: int) -> tuple[float, float, str]:
    x0, y0, w, h = win
    sw = SWEPT.get((TERM, macos_major()))
    if sw:
        return x0 + sw[0] + (c + 0.5) * sw[2], y0 + sw[1] + (r + 0.5) * sw[3], "swept"
    return x0 + (c + 0.5) * w / cols, y0 + 28 + (r + 0.5) * (h - 28) / rows_, "formula"


def click(x: float, y: float) -> None:
    activate()
    xi, yi = int(round(x)), int(round(y))
    sh(["cliclick", f"m:{xi},{yi}", "w:150", f"c:{xi},{yi}"])


def main() -> int:
    sh("pkill -x Terminal; pkill -x iTerm2; true")
    inner = os.path.join(OUT, "inner.sh")
    with open(inner, "w") as f:
        f.write(
            f'#!/bin/bash\nexport TERM=xterm-256color\ncd "{OUT}"\nstty size > "{OUT}/size.txt"\n'
            f'"{EXE}" --no-config --replay "{REPLAY}" --hours 720 --min-mag 2.5 --log "{OUT}/earthwave.log"\n'
            f'echo EWDONE rc=$?\nsleep 120\n'
        )
    os.chmod(inner, 0o755)
    if TERM == "Terminal":
        script = NL.join(
            ['tell application "Terminal"', "activate", f'do script "{inner}"', "set bounds of front window to {20, 40, 1010, 740}", "end tell"]
        )
    else:
        script = NL.join(
            [
                'tell application id "com.googlecode.iterm2"',
                "activate",
                "if (count of windows) is 0 then create window with default profile",
                f'tell current session of current window to write text "exec /bin/bash {inner}"',
                "set bounds of current window to {20, 40, 1010, 740}",
                "end tell",
            ]
        )
    rc, o = sh(["osascript", "-e", script], timeout=25)
    step("launch the terminal and earthwave --replay", rc == 0, rc=rc, out=o)
    if rc != 0:
        return 1
    if TERM == "iTerm2":
        sh([PY, os.path.join(S4, "dismiss.py"), "1"], timeout=120)
    # J-start: the dashboard is up
    text = ""
    end = time.time() + 90
    while time.time() < end:
        time.sleep(2)
        text = readback()
        if re.search(r"[1-9]\d* shown of", text):
            break
    step("dashboard shows quakes in a real terminal", bool(re.search(r"[1-9]\d* shown of", text)))
    record("00-start", text)
    if not re.search(r"[1-9]\d* shown of", text):
        return 1
    time.sleep(2)
    activate()
    try:
        r_, c_ = (int(v) for v in open(os.path.join(OUT, "size.txt")).read().split())
    except Exception:
        r_, c_ = 40, 140
    RES["size_cols_rows"] = [c_, r_]
    base = readback()
    first = selected(base)

    # keys: arrow down and j/k move the selection
    key("arrow-down")
    t1 = settle()
    record("01-down", t1)
    step("arrow-down moves the selection", selected(t1) != first and selected(t1) is not None, before=first, after=selected(t1))
    typed("j")
    t2 = settle()
    step("j moves the selection", selected(t2) != selected(t1), before=selected(t1), after=selected(t2))
    typed("k")
    t3 = settle()
    step("k moves it back", selected(t3) == selected(t1), after=selected(t3), expected=selected(t1))
    record("02-jk", t3)

    # Enter opens the detail, Escape returns
    key("return")
    d = settle()
    record("03-detail", d)
    step("Enter opens a different screen (the detail)", d != t3 and not rows(d)[:3] == rows(t3)[:3])
    key("esc")
    back = settle()
    step("Escape returns to the list", bool(re.search(r"[1-9]\d* shown of", back)))

    # ? help
    typed("?")
    h = settle()
    record("04-help", h)
    step("? opens the help screen", "Keys" in h and h != back)
    key("esc")
    step("Escape closes the help", bool(re.search(r"[1-9]\d* shown of", settle())))

    # f filters
    typed("f")
    f_ = settle()
    record("05-filters", f_)
    step("f opens the filters screen", f_ != back)
    key("esc")
    settle()

    # / search: type a word, Enter, then clear
    typed("/")
    time.sleep(0.6)
    typed("Japan")
    time.sleep(0.4)
    key("return")
    s_ = settle()
    record("06-search", s_)
    step("search for Japan changes the list", s_ != back)
    typed("/")
    time.sleep(0.4)
    for _ in range(5):
        key("delete")
    key("return")
    cleared = settle()
    step("clearing the search brings the list back", bool(re.search(r"[1-9]\d* shown of", cleared)))

    # m map
    typed("m")
    m1 = settle()
    record("07-map-off", m1)
    step("m changes the screen (map hidden or shown)", m1 != cleared)
    typed("m")
    m2 = settle()
    step("m again changes it back", m2 != m1)

    # real mouse click on a list row
    cur = readback()
    lr = rows(cur)
    if len(lr) >= 5:
        target_i, target_text = lr[4]
        wid, win = window()
        x, y, how = cell_px(win, c_, r_, 12, target_i)
        click(x, y)
        after = settle()
        record("08-click", after)
        want = re.sub(r"^\S?\s*\d+[smhd]\s+", "", target_text)
        got = selected(after)
        landed = [i for i, ln in enumerate(after.splitlines()) if SELECTED.match(ln[:72])]
        step(
            "a real mouse click selects the clicked list row",
            got == want,
            geometry=how,
            intended_row=target_i,
            landed_row=landed[0] if landed else None,
            window=win,
        )
    else:
        step("a real mouse click selects the clicked list row", False, note="fewer than 5 list rows to click")

    # q quits
    typed("q")
    time.sleep(3)
    end_text = readback()
    record("09-quit", end_text)
    step("q ends the program", "EWDONE" in end_text, tail=end_text[-120:])
    failed = [s["name"] for s in RES["steps"] if not s["ok"]]
    RES["failed"] = failed
    step("summary", True, failed=len(failed), total=len(RES["steps"]) - 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
