"""Spike S4 driver: launch a TUI in a real terminal window, inject real input via several backends, screenshot, read back.

Usage: drive.py TERMINAL BACKENDS   e.g.  drive.py Terminal osa,cgevent,cliclick
Env: OUT, S4 (script dir), S4PY (python with textual), VNC_SERVER, VNC_PW (for backend vnc)
Writes $OUT/result.json and screenshots. Every backend is tried and recorded independently.
"""

import json
import os
import re
import subprocess
import sys
import time

OUT = os.environ["OUT"]
S4 = os.environ["S4"]
PY = os.environ.get("S4PY", sys.executable)
TERM = sys.argv[1]
BACKENDS = sys.argv[2].split(",")
APPNAME = {"Terminal": "Terminal", "iTerm2": "iTerm"}[TERM]
OWNER = {"Terminal": "Terminal", "iTerm2": "iTerm2"}[TERM]
LOGA = os.path.join(OUT, "app_events.jsonl")
LOGR = os.path.join(OUT, "raw_bytes.jsonl")
RES = {"terminal": TERM, "backends": BACKENDS, "steps": []}
KEYCODE = {"2": 19, "3": 20, "4": 21, "5": 23}
KEYFOR = {"osa": "2", "cgevent": "3", "cliclick": "4", "vnc": "5"}


NL = chr(10)


def step(name, ok, **kw):
    RES["steps"].append({"name": name, "ok": ok, **kw})
    print(("PASS " if ok else "FAIL ") + name, kw, flush=True)
    with open(os.path.join(OUT, "result.json"), "w") as f:
        json.dump(RES, f, indent=1, default=str)


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return 99, repr(e)


def osa(script):
    return sh(["osascript", "-e", script])


def jl(path):
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def wait_for(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.4)
    return None


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


def shot(name):
    wid, b = window()
    sh(f'screencapture -x "{OUT}/{name}_full.png"')
    if wid:
        rc, o = sh(f'screencapture -x -o -l {wid} "{OUT}/{name}_window.png"')
        return wid, b, rc, o
    return None, None, 1, "no window"


def activate():
    osa(f'tell application "{APPNAME}" to activate')
    time.sleep(0.6)


# ---- injection backends ----
def cg_post(ev):
    import Quartz

    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def inject_key(be, ch):
    if be == "osa":
        return osa(f'tell application "System Events" to keystroke "{ch}"')
    if be == "cgevent":
        import Quartz

        for down in (True, False):
            cg_post(Quartz.CGEventCreateKeyboardEvent(None, KEYCODE[ch], down))
            time.sleep(0.05)
        return 0, ""
    if be == "cliclick":
        return sh(["cliclick", f"t:{ch}"])
    if be == "vnc":
        return sh([os.path.join(os.path.dirname(PY), "vncdo"), "-s", os.environ["VNC_SERVER"], "-u", os.environ["VNC_USER"], "-p", os.environ["VNC_PW"], "key", ch])
    return 98, "unknown backend"


def inject_click(be, x, y):
    x, y = int(round(x)), int(round(y))
    if be == "osa":
        return osa(f'tell application "System Events" to click at {{{x}, {y}}}')
    if be == "cgevent":
        import Quartz

        pt = (x, y)
        cg_post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft))
        time.sleep(0.15)
        cg_post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, pt, Quartz.kCGMouseButtonLeft))
        time.sleep(0.08)
        cg_post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, pt, Quartz.kCGMouseButtonLeft))
        return 0, ""
    if be == "cliclick":
        return sh(["cliclick", f"m:{x},{y}", "w:150", f"c:{x},{y}"])
    if be == "vnc":
        s = float(RES.get("vnc_scale", 1.0))
        return sh([os.path.join(os.path.dirname(PY), "vncdo"), "-s", os.environ["VNC_SERVER"], "-u", os.environ["VNC_USER"], "-p", os.environ["VNC_PW"],
                   "move", str(int(x * s)), str(int(y * s)), "pause", "0.2", "click", "1"])
    return 98, "unknown backend"


# ---- geometry ----
class Geo:
    title = 28.0

    def __init__(self, bounds, cols, rows):
        self.x, self.y, self.w, self.h = bounds
        self.cols, self.rows = cols, rows
        self.cw = self.w / cols
        self.ch = (self.h - self.title) / rows
        self.dx = 0.0
        self.dy = 0.0

    def px(self, col0, row0):
        """pixel centre of 0-based cell"""
        return self.x + (col0 + 0.5) * self.cw + self.dx, self.y + self.title + (row0 + 0.5) * self.ch + self.dy


def main():
    sh("pkill -x Terminal; pkill -x iTerm2; true")
    for p in (LOGA, LOGR):
        if os.path.exists(p):
            os.remove(p)
    inner = os.path.join(OUT, "inner.sh")
    with open(inner, "w") as f:
        f.write(
            f'#!/bin/bash\nexport S4_LOG="{LOGA}" S4_RAW="{LOGR}" TERM=xterm-256color\ncd "{S4}"\n'
            f'"{PY}" rawprobe.py\n"{PY}" app.py\necho S4DONE\nsleep 60\n'
        )
    os.chmod(inner, 0o755)
    if TERM == "Terminal":
        script = NL.join(['tell application "Terminal"', "activate", 'do script "' + inner + '"',
                           "set bounds of front window to {40, 60, 900, 660}", "end tell"])
    else:
        # iTerm opens a default window on launch; reuse it and start the app in its shell (launch only, not the input under test)
        script = NL.join(['tell application id "com.googlecode.iterm2"', "activate",
                           "if (count of windows) is 0 then create window with default profile",
                           'tell current session of current window to write text "exec /bin/bash ' + inner + '"',
                           "set bounds of current window to {40, 60, 900, 660}", "end tell"])
    rc, o = sh(["osascript", "-e", script], timeout=25)
    if rc != 0:
        step("launch via AppleScript (Apple Events)", False, rc=rc, out=o)
        sh("pkill -x Terminal; pkill -x iTerm2; true")
        time.sleep(1)
        cmdf = os.path.join(OUT, "inner.command")
        sh(["cp", inner, cmdf])
        os.chmod(cmdf, 0o755)
        rc, o = sh(["open", "-a", APPNAME, cmdf])
        step("fallback launch via open -a (no Apple Events)", rc == 0, rc=rc, out=o)
        rc = 0
    step("terminal window launched", rc == 0, rc=rc, out=o)
    if rc != 0:
        return
    ready = wait_for(lambda: [e for e in jl(LOGR) if e.get("event") == "ready"], 40)
    step("probe started in window (reads terminal size)", bool(ready), ready=ready)
    if not ready:
        shot("00_launch_failed")
        return
    if TERM == "iTerm2":
        rc, o = sh([PY, os.path.join(S4, "dismiss.py"), "1"], timeout=120)
        step("dismiss iTerm first-launch dialogs", True, out=o[-400:])
    # wait until the window size settles (probe logs resize events)
    last, stable = None, 0
    for _ in range(30):
        cur = ([e for e in jl(LOGR) if "cols" in e][-1]["cols"], [e for e in jl(LOGR) if "cols" in e][-1]["rows"], window()[1])
        stable = stable + 1 if cur == last else 0
        last = cur
        if stable >= 6:
            break
        time.sleep(0.5)
    activate()
    sz = [e for e in jl(LOGR) if "cols" in e][-1]
    cols, rows = sz["cols"], sz["rows"]
    wid, b = window()
    step("find window id/bounds via Quartz", bool(wid), wid=wid, bounds=b)
    if not wid:
        return
    geo = Geo(b, cols, rows)
    shot("01_probe")
    # display scale for VNC
    try:
        import Quartz

        disp = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        rc, o = sh(f'sips -g pixelWidth "{OUT}/01_probe_full.png"')
        m = re.search(r"pixelWidth: (\d+)", o)
        if m:
            RES["vnc_scale"] = int(m.group(1)) / disp.size.width
        RES["display_points"] = [disp.size.width, disp.size.height]
        RES["screenshot_px_width"] = m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        RES["display_err"] = repr(e)

    # ---- probe: raw SGR bytes + landing accuracy ----
    tc, tr = 20, 8  # intended 0-based cell
    mouse_ok = []
    n_before = 0
    for be in BACKENDS:
        activate()
        n_before = len(jl(LOGR))
        x, y = geo.px(tc, tr)
        rc, o = inject_click(be, x, y)
        got = wait_for(lambda: [e for e in jl(LOGR)[n_before:] if "hex" in e], 5)
        time.sleep(0.8)
        if got:
            data = b"".join(bytes.fromhex(e["hex"]) for e in jl(LOGR)[n_before:] if "hex" in e)
            m = re.findall(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", data)
            cell = None
            if m:
                cell = (int(m[0][1]) - 1, int(m[0][2]) - 1)
            ok = cell is not None
            step(f"probe click via {be}", ok, inject_rc=rc, inject_out=o, intended=[tc, tr], received_cell=cell,
                 raw=repr(data), px=[x, y], encoding="SGR 1006" if m else "other/legacy")
            if ok:
                mouse_ok.append(be)
                if cell != (tc, tr):
                    geo.dx += (tc - cell[0]) * geo.cw
                    geo.dy += (tr - cell[1]) * geo.ch
                    RES["calibration"] = {"dx": geo.dx, "dy": geo.dy, "first_miss": [cell[0] - tc, cell[1] - tr]}
                else:
                    RES["calibration"] = {"first_miss": [0, 0]}
        else:
            step(f"probe click via {be}", False, inject_rc=rc, inject_out=o, note="no bytes reached the app", px=[x, y])
        if len(mouse_ok) >= 2:
            break
    # corrective click to verify calibration (also lets probe exit)
    if mouse_ok:
        be = mouse_ok[0]
        n_before = len(jl(LOGR))
        x, y = geo.px(tc, tr)
        inject_click(be, x, y)
        time.sleep(1.5)
        data = b"".join(bytes.fromhex(e["hex"]) for e in jl(LOGR)[n_before:] if "hex" in e)
        m = re.findall(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", data)
        cell = (int(m[0][1]) - 1, int(m[0][2]) - 1) if m else None
        step("calibrated click lands on intended cell", cell == (tc, tr), intended=[tc, tr], received_cell=cell)
    # wait for probe to finish (25s max) and the app to start
    app_ready = wait_for(lambda: [e for e in jl(LOGA) if e.get("kind") == "ready"], 45)
    step("Textual app started after probe", bool(app_ready), ready=app_ready)
    if not app_ready:
        shot("02_app_failed")
        return
    lay = app_ready[0]
    time.sleep(1)
    wid, b = window()
    off = (geo.dx, geo.dy)
    geo = Geo(b, lay["size"][0], lay["size"][1])
    geo.dx, geo.dy = off
    activate()
    shot("02_app_initial")

    # ---- keys ----
    for be in BACKENDS:
        activate()
        ch = KEYFOR[be]
        n = len(jl(LOGA))
        rc, o = inject_key(be, ch)
        got = wait_for(lambda: [e for e in jl(LOGA)[n:] if e.get("kind") == "key" and e.get("key") == ch], 5)
        step(f"key '{ch}' via {be} reaches app", bool(got), inject_rc=rc, inject_out=o)
        if got:
            shot(f"03_key_{be}")

    # ---- mouse on button ----
    bt = lay["btn"]
    for be in BACKENDS:
        activate()
        c0, r0 = bt["x"] + bt["w"] // 2, bt["y"] + bt["h"] // 2
        x, y = geo.px(c0, r0)
        n = len(jl(LOGA))
        rc, o = inject_click(be, x, y)
        got = wait_for(lambda: [e for e in jl(LOGA)[n:] if e.get("kind") == "button"], 5)
        md = [e for e in jl(LOGA)[n:] if e.get("kind") == "mouse_down"]
        step(f"click on button via {be} increments counter", bool(got), inject_rc=rc, inject_out=o,
             intended_cell=[c0, r0], app_saw_mouse_down=[(e["x"], e["y"]) for e in md], counter=got[0]["count"] if got else None)
        if got:
            shot(f"04_click_{be}")
    # ---- list click ----
    ls = lay["lst"]
    for be in BACKENDS:
        activate()
        c0, r0 = ls["x"] + 4, ls["y"] + 3
        x, y = geo.px(c0, r0)
        n = len(jl(LOGA))
        inject_click(be, x, y)
        got = wait_for(lambda: [e for e in jl(LOGA)[n:] if e.get("kind") in ("list_selected", "list_highlighted")], 5)
        md = [(e["x"], e["y"]) for e in jl(LOGA)[n:] if e.get("kind") == "mouse_down"]
        step(f"click on list row via {be}", bool(got), intended_cell=[c0, r0], app_saw_mouse_down=md,
             events=[(e["kind"], e["item"]) for e in got or []])
        if got:
            break

    # ---- read-back + final screenshot ----
    activate()
    wid, b, rc, o = shot("05_final")
    step("window screenshot (screencapture -l)", rc == 0 and os.path.exists(f"{OUT}/05_final_window.png"), wid=wid, rc=rc, out=o)
    if TERM == "Terminal":
        rc, txt = osa('tell application "Terminal" to get contents of selected tab of front window')
    else:
        helper = os.path.join(S4, "iterm_readback.py")
        rc, txt = sh([PY, helper], timeout=40)
        RES["iterm_api_readback"] = {"rc": rc, "out": txt[:600]}
        if rc != 0 or "count=" not in txt:
            rc, txt = osa('tell application id "com.googlecode.iterm2" to get text of current session of current window')
            RES["iterm_readback_via"] = "AppleScript text of session (API failed)"
    with open(os.path.join(OUT, "readback.txt"), "w") as f:
        f.write(txt)
    last = max([e.get("count", 0) for e in jl(LOGA) if e.get("kind") == "button"] or [0])
    step("terminal read-back shows app state", rc == 0 and f"count={last}" in txt and last > 0, rc=rc, expect=f"count={last}",
         sample=txt[:400])


main()
