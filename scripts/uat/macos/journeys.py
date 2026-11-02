"""earthwave input journeys on Terminal.app or iTerm2 of a GitHub macOS runner, with real keys and a real mouse click.

Method vendored from eyewall's spike S4 (docs: C:\\Users\\billp\\review-findings\\uat-shared\\spikes\\macos-input\\README.md;
`grant.sh`, `dismiss.py`, `iterm_readback.py`, `drive.py` in this folder are copied unchanged from eyewall branch
spike/s4-macos-input). Read-back: Terminal.app `contents of selected tab`, iTerm2 `async_get_screen_contents`.
No tmux, no pty writes.

    OUT=dir S4=scripts/uat/macos S4PY=~/s4venv/bin/python EXE=./earthwave REPLAY=tests/fixtures/replay/usgs_short.jsonl \
        python journeys.py Terminal|iTerm2

Phase 0 (probe.py runs in the terminal before the app): the window is sized until the terminal is big enough, and each
special key (arrow-down, Escape, Return, Delete) is injected by three backends (cliclick kp:, System Events key code, CGEvent)
while the probe logs the exact bytes that arrive. The journey then uses, per key, the first backend whose bytes were right,
and records which. After every input the read-back is polled until it changes (the read-back lags the screen).

Writes $OUT/drive.log (PASS/FAIL per step), $OUT/result.json (incl. key_probe), probe.jsonl, a window screenshot and the
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
RES: dict = {"terminal": TERM, "steps": [], "key_probe": {}}
ROW = re.compile(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d")
SELECTED = re.compile(r"^[▌>]\s*\d+[smhd]\s+\d\.\d")
LISTED = re.compile(r"[1-9]\d* shown of")
BOUNDS = "{20, 40, 1010, 740}"


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


def record(name: str, text: str) -> None:
    with open(os.path.join(OUT, f"readback-{name}.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    shot(name)


def set_bounds() -> None:
    if TERM == "Terminal":
        osa(f'tell application "Terminal" to set bounds of front window to {BOUNDS}')
    else:
        osa(f'tell application id "com.googlecode.iterm2" to set bounds of current window to {BOUNDS}')


def probe_events() -> list[dict]:
    out = []
    try:
        for line in open(os.path.join(OUT, "probe.jsonl")):
            out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


def act_and_wait(action, prev: str, timeout: float = 6.0) -> tuple[str, float]:
    """Run an input, then poll the read-back until it differs from `prev` (it lags the screen), plus a short settle.
    Returns the text and the seconds taken (the timeout if it never changed)."""
    t0 = time.time()
    action()
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        text = readback()
        if text != prev:
            time.sleep(0.6)
            return readback(), round(time.time() - t0, 1)
    return prev, round(time.time() - t0, 1)


KEYCODE = {"arrow-down": 125, "esc": 53, "return": 36, "delete": 51}
EXPECT = {"arrow-down": ("1b5b42", "1b4f42"), "esc": ("1b",), "return": ("0d",), "delete": ("7f",)}
BACKENDS = ["cliclick", "osa", "cgevent"]
BACKEND = {k: "cliclick" for k in KEYCODE}


def inject_key(name: str, be: str) -> None:
    code = KEYCODE[name]
    if be == "cliclick":
        sh(["cliclick", f"kp:{name}"])
    elif be == "osa":
        osa(f'tell application "System Events" to key code {code}')
    else:
        import Quartz

        for down in (True, False):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, code, down))
            time.sleep(0.05)


def key(*keys: str) -> None:
    activate()
    for k in keys:
        inject_key(k, BACKEND[k])
        time.sleep(0.3)


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


def phase0() -> tuple[int, int] | None:
    """Size the window, then probe what bytes each backend delivers for each special key."""
    end = time.time() + 45
    while time.time() < end and not any(e["ev"] == "ready" for e in probe_events()):
        time.sleep(0.5)
    cols = rows_ = 0
    for _ in range(8):
        activate()
        set_bounds()
        time.sleep(2.5)
        sizes = [e for e in probe_events() if e["ev"] == "size"]
        if sizes:
            cols, rows_ = sizes[-1]["cols"], sizes[-1]["rows"]
        if cols >= 130 and rows_ >= 35:
            break
    RES["size_cols_rows"] = [cols, rows_]
    step("window is big enough for the app (>= 130x35)", cols >= 130 and rows_ >= 35, cols=cols, rows=rows_)
    if cols < 130:
        return None
    # chars first (cliclick t: is the S4-proven path)
    for ch, hexv in (("j", "6a"), ("?", "3f"), ("/", "2f")):
        n = len(probe_events())
        typed(ch)
        time.sleep(0.8)
        got = "".join(e["hex"] for e in probe_events()[n:] if e["ev"] == "bytes")
        RES["key_probe"][f"char {ch}"] = {"cliclick t:": got}
        step(f"probe: typed {ch!r} arrives as {hexv}", got == hexv, got=got)
    for name, expect in EXPECT.items():
        RES["key_probe"][name] = {}
        chosen = None
        for be in BACKENDS:
            activate()
            n = len(probe_events())
            inject_key(name, be)
            time.sleep(0.9)
            got = "".join(e["hex"] for e in probe_events()[n:] if e["ev"] == "bytes")
            RES["key_probe"][name][be] = got
            if got in expect and chosen is None:
                chosen = be
        if chosen:
            BACKEND[name] = chosen
        step(
            f"probe: {name} reaches the terminal as {'/'.join(expect)}",
            chosen is not None,
            by_backend=RES["key_probe"][name],
            used=chosen,
        )
    typed("Z")  # ends the probe; inner.sh then starts the app
    time.sleep(1)
    return cols, rows_


def main() -> int:
    sh("pkill -x Terminal; pkill -x iTerm2; true")
    inner = os.path.join(OUT, "inner.sh")
    with open(inner, "w") as f:
        f.write(
            f'#!/bin/bash\nexport TERM=xterm-256color\ncd "{OUT}"\n"{PY}" "{S4}/probe.py" "{OUT}"\n'
            f'stty size > "{OUT}/size.txt"\n'
            f'"{EXE}" --no-config --replay "{REPLAY}" --hours 720 --min-mag 2.5 --log "{OUT}/earthwave.log"\n'
            f"echo EWDONE rc=$?\nsleep 120\n"
        )
    os.chmod(inner, 0o755)
    if TERM == "Terminal":
        script = NL.join(['tell application "Terminal"', "activate", f'do script "{inner}"', f"set bounds of front window to {BOUNDS}", "end tell"])
    else:
        script = NL.join(
            [
                'tell application id "com.googlecode.iterm2"',
                "activate",
                "if (count of windows) is 0 then create window with default profile",
                f'tell current session of current window to write text "exec /bin/bash {inner}"',
                f"set bounds of current window to {BOUNDS}",
                "end tell",
            ]
        )
    rc, o = sh(["osascript", "-e", script], timeout=25)
    step("launch the terminal and the probe", rc == 0, rc=rc, out=o)
    if rc != 0:
        return 1
    if TERM == "iTerm2":
        sh([PY, os.path.join(S4, "dismiss.py"), "1"], timeout=120)
    size = phase0()
    if size is None:
        record("00-too-small", readback())
        return 1
    cols, rows_ = size
    # the app starts after the probe
    text = ""
    end = time.time() + 90
    while time.time() < end:
        time.sleep(2)
        text = readback()
        if LISTED.search(text):
            break
    step("dashboard shows quakes in a real terminal", bool(LISTED.search(text)))
    record("00-start", text)
    if not LISTED.search(text):
        return 1
    time.sleep(2)
    base = readback()
    first = selected(base)
    step("read-back sees the dashboard rows", len(rows(base)) >= 5, rows=len(rows(base)), selected=first)

    # arrow-down and j/k move the selection
    t1, took = act_and_wait(lambda: key("arrow-down"), base)
    record("01-down", t1)
    step("arrow-down moves the selection", selected(t1) not in (None, first), before=first, after=selected(t1), seconds=took, backend=BACKEND["arrow-down"])
    t2, took = act_and_wait(lambda: typed("j"), t1)
    step("j moves the selection", selected(t2) not in (None, selected(t1)), before=selected(t1), after=selected(t2), seconds=took)
    t3, took = act_and_wait(lambda: typed("k"), t2)
    step("k moves it back", selected(t3) == selected(t1), after=selected(t3), expected=selected(t1), seconds=took)
    record("02-jk", t3)

    # a real mouse click on a list row, on the clean list
    lr = rows(t3)
    if len(lr) >= 5:
        target_i, target_text = lr[4]
        wid, win = window()
        x, y, how = cell_px(win, cols, rows_, 12, target_i)
        after, took = act_and_wait(lambda: click(x, y), t3)
        record("03-click", after)
        want = re.sub(r"^\S?\s*\d+[smhd]\s+", "", target_text)
        landed = [i for i, ln in enumerate(after.splitlines()) if SELECTED.match(ln[:72])]
        step(
            "a real mouse click selects the clicked list row",
            selected(after) == want,
            geometry=how,
            intended_row=target_i,
            landed_row=landed[0] if landed else None,
            window=win,
            seconds=took,
        )
    else:
        step("a real mouse click selects the clicked list row", False, note="fewer than 5 list rows", rows=len(lr))
    cur = readback()

    # Enter opens the detail, Escape returns
    d, took = act_and_wait(lambda: key("return"), cur)
    record("04-detail", d)
    step("Enter opens the detail screen", not LISTED.search(d) or rows(d)[:3] != rows(cur)[:3], seconds=took, backend=BACKEND["return"])
    back, took = act_and_wait(lambda: key("esc"), d)
    step("Escape returns to the list", bool(LISTED.search(back)), seconds=took, backend=BACKEND["esc"])

    # ? help
    h, took = act_and_wait(lambda: typed("?"), back)
    record("05-help", h)
    step("? opens the help screen", "Keys" in h, seconds=took)
    back, took = act_and_wait(lambda: key("esc"), h)
    step("Escape closes the help", bool(LISTED.search(back)), seconds=took)

    # f filters
    f_, took = act_and_wait(lambda: typed("f"), back)
    record("06-filters", f_)
    step("f opens the filters screen", f_ != back, seconds=took)
    back, took = act_and_wait(lambda: key("esc"), f_)
    step("Escape closes the filters", bool(LISTED.search(back)), seconds=took)

    # / search: type a word, Enter, then clear
    s0, took = act_and_wait(lambda: typed("/"), back)
    record("07-search-open", s0)
    typed("Japan")
    time.sleep(0.5)
    s_, took = act_and_wait(lambda: key("return"), s0)
    record("07-search", s_)
    step("search for Japan changes the list", s_ != back, seconds=took)
    key("esc")
    time.sleep(1)
    s1, _ = act_and_wait(lambda: typed("/"), readback())
    for _ in range(6):
        key("delete")
    cleared, took = act_and_wait(lambda: key("return"), s1)
    record("07-search-cleared", cleared)
    step("clearing the search brings the full list back", len(rows(cleared)) >= 5, rows=len(rows(cleared)), seconds=took)

    # m map
    cur = readback()
    m1, took = act_and_wait(lambda: typed("m"), cur)
    record("08-map-off", m1)
    step("m changes the screen (map hidden)", m1 != cur, seconds=took)
    m2, took = act_and_wait(lambda: typed("m"), m1)
    step("m again changes it back", m2 != m1, seconds=took)

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
