"""Drive the wxbrief 0.3.0 release exe in a REAL Windows terminal and read the screen back.

    python run_spike.py {wt,conhost,vscode} --wxbrief PATH --out DIR [--tool-exe PATH]

For one terminal this launches ``wxbrief --demo``, focuses the window, sends keys (``2``, ``v``, ``v``), clicks a station
row by cell->pixel math (motion, press, release), reads the screen text back (UIA TextPattern for Windows Terminal and
conhost, select-all + copy to the clipboard for VS Code), saves PNGs and text, presses ``q`` and checks the exit, then
tears down by PID. Exit code 0 only if every assertion passed. Results go to ``DIR/result-<terminal>.json``.

Assertions (the demo opens on KDEN, 1/10): after clicking the UUEE row the status bar says ``4/10`` and the detail pane
header names UUEE (Moscow/Sheremetyevo); ``q`` ends the app (the window closes, or in VS Code the prompt comes back).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import medium
import winterm as w

START_RE = r"Stations \(10\)"
INITIAL_RE = r"\b1/10\b"
TARGET_ROW = "UUEE"
GRID_RE = r"KDEN\s+LIFR\s+Denver"
LIST_RE = r"KDEN\s+LIFR\s+32005KT"
AFTER_CLICK = [r"\b4/10\b", r"UUEE\s+Moscow"]
VK_BACKTICK = 0xC0
VK_J, VK_K = ord("J"), ord("K")
VK_A, VK_C, VK_X, VK_M = ord("A"), ord("C"), ord("X"), ord("M")


class Abort(Exception):
    """A step the rest of the run depends on failed."""


class Report:
    def __init__(self, terminal: str, out: Path) -> None:
        self.terminal, self.out = terminal, out
        self.steps: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.t0 = time.monotonic()

    def check(self, name: str, ok: bool, detail: str = "", *, critical: bool = False) -> bool:
        self.steps.append({"step": name, "ok": ok, "detail": detail, "t": round(time.monotonic() - self.t0, 1)})
        w.log(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
        if critical and not ok:
            raise Abort(name)
        return ok

    def note(self, name: str, detail: str) -> None:
        self.steps.append({"step": name, "ok": None, "detail": detail, "t": round(time.monotonic() - self.t0, 1)})
        w.log(f"INFO {name} {detail}")

    @property
    def passed(self) -> bool:
        return all(s["ok"] is not False for s in self.steps) and any(s["ok"] for s in self.steps)

    def write(self) -> None:
        data = {
            "terminal": self.terminal,
            "passed": self.passed,
            "runtime_s": round(time.monotonic() - self.t0, 1),
            "info": self.info,
            "steps": self.steps,
        }
        (self.out / f"result-{self.terminal}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------------
# terminal adapters


class Term:
    """What differs between terminals: launch, finding the window, reading text, locating text in pixels, quitting."""

    name = "?"

    def __init__(
        self,
        rep: Report,
        wxbrief: Path,
        out: Path,
        tool_exe: Path | None,
        limited: bool = False,
        scheme: str = "dark",
        *,
        medium_spawn: bool = False,
        require_medium: bool = False,
        tag_suffix: str = "",
        app_args: str = "--demo --no-config",
        app_cmd: str | None = None,
        env_extra: dict[str, str] | None = None,
        codepage: int | None = None,
        font_pt: int | None = None,
        profile: str = "cmd",
        probe: bool = True,
    ) -> None:
        self.rep, self.wxbrief, self.out, self.tool_exe, self.limited = rep, wxbrief, out, tool_exe, limited
        self.scheme = scheme
        self.medium_spawn, self.require_medium = medium_spawn, require_medium
        self.app_args, self.app_cmd, self.env_extra = app_args, app_cmd, dict(env_extra or {})
        self.codepage, self.font_pt, self.profile, self.probe = codepage, font_pt, profile, probe
        self.tag = (
            self.name
            + ("-limited" if limited else "")
            + ("-light" if scheme == "light" else "")
            + (f"-{tag_suffix}" if tag_suffix else "")
        )
        self.proc: subprocess.Popen[bytes] | None = None
        self.hwnd = 0
        self.pids: set[int] = set()
        self.image_dirs: tuple[str, ...] = ()
        self.work = out / f"work-{self.tag}"
        self.work.mkdir(parents=True, exist_ok=True)
        self.run_cmd = self.work / "run-wxbrief.cmd"
        self.env = dict(os.environ, WXBRIEF_CACHE_DIR=str(self.work / "cache"))
        self.env.pop("NO_COLOR", None)

    def write_run_cmd(self, prefix: str = "") -> None:
        """The launcher the terminal runs: environment, code page, a pause for the profile probe, then the app."""
        lines = ["@echo off", f'set "WXBRIEF_CACHE_DIR={self.work / "cache"}"']
        lines += [f'set "{k}={v}"' for k, v in self.env_extra.items()]
        if self.codepage:
            lines.append(f"chcp {self.codepage} >nul")
        if prefix:
            lines.append(prefix.strip())
        if self.probe:  # for a few seconds the terminal shows only its own default colours: measured in prove_profile
            lines += [
                "echo wxbrief real-terminal profile probe: default text on the default background",
                "ping -n 6 127.0.0.1 >nul",
            ]
        lines.append(self.app_cmd or f'"{self.wxbrief}" {self.app_args}')
        lines.append("set RC=%errorlevel%")  # a non-zero exit keeps the window up so the screen can be read
        lines.append("if not %RC%==0 (echo wxbrief exited with code %RC% & ping -n 30 127.0.0.1 >nul)")
        lines.append("exit /b %RC%")
        self.run_cmd.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")

    def spawn(self, argv: list[str]) -> subprocess.Popen[bytes]:
        """Start the terminal. Limited: through ``runas /trustlevel:0x20000`` so it runs with a filtered (non-admin) token."""
        if self.medium_spawn:
            self.rep.note("spawn", f"Medium token (restricted, Administrators deny-only): {argv}")
            return medium.spawn_medium(argv, env=self.env)  # type: ignore[return-value]
        if self.limited:
            cmd = subprocess.list2cmdline(["runas", "/trustlevel:0x20000", subprocess.list2cmdline(argv)])
            self.rep.note("spawn", cmd)
            return subprocess.Popen(cmd, env=self.env)
        return subprocess.Popen(argv, env=self.env)

    def note_integrity(self) -> None:
        owner = w.window_info(self.hwnd).pid
        win, drv = w.integrity_of(owner), w.integrity_of(os.getpid())
        self.rep.note("integrity", f"window owner pid {owner}: {win}; driver pid {os.getpid()}: {drv}")
        self.rep.info.setdefault("integrity_window", win)
        self.rep.info.setdefault("integrity_driver", drv)
        if self.require_medium:
            self.rep.check("terminal window process is Medium integrity", win.startswith("Medium"), win, critical=True)

    def prove_profile(self) -> None:
        """Measure the terminal default colours from a screenshot while only the launcher echo is on screen."""
        if not self.probe:
            return
        w.focus(self.hwnd)
        time.sleep(0.5)
        path = self.out / f"{self.tag}-00-profile-probe-window.png"
        left, top, right, bottom = w.window_info(self.hwnd).rect
        sw, sh = w.screen_size()
        w.screenshot_screen(path, (max(left, 0), max(top, 0), min(right, sw), min(bottom, sh)))
        info = w.analyze_png(path)
        self.rep.info["profile_probe"] = info
        light = self.scheme == "light"
        ok = info["bg_luminance"] > 0.7 if light else info["bg_luminance"] < 0.3
        self.rep.check(
            f"{self.scheme} profile really applied (background measured from pixels)",
            ok,
            f"bg={info['bg']} luminance={info['bg_luminance']} text={info['text']} contrast={info['contrast']}",
        )

    def launch(self) -> None:
        raise NotImplementedError

    def read(self) -> str:
        raise NotImplementedError

    def rect_of(self, text: str) -> tuple[int, int, int, int] | None:
        raise NotImplementedError

    def after_keys(self) -> None:
        """Hook run after sending keys (VS Code needs to clear its selection)."""

    def tracked(self) -> set[int]:
        out = set(self.pids)
        if self.proc:
            out |= {p.pid for p in w.process_tree(self.proc.pid)}
        return out

    def find_window(self, cls: str, timeout: float = 60) -> int:
        before = {win.hwnd for win in self.baseline}

        def look() -> int:
            tree = {p.pid for p in w.process_tree(self.proc.pid)} if self.proc else set()
            cands = [win for win in w.all_windows() if win.cls == cls and win.visible and win.hwnd not in before]
            mine = [win for win in cands if win.pid in tree]
            pick = (mine or cands or [None])[0]
            return pick.hwnd if pick else 0

        return int(w.wait_for(look, timeout, f"a visible {cls} window"))

    def window_gone(self) -> bool:
        return not w.is_window(self.hwnd)

    def snapshot_baseline(self) -> None:
        self.baseline = w.all_windows()

    def shot(self, tag: str, *, printwindow: bool = False) -> None:
        base = self.out / f"{self.tag}-{tag}"
        sw, sh = w.screen_size()
        if self.hwnd and w.is_window(self.hwnd):
            left, top, right, bottom = w.window_info(self.hwnd).rect
            bbox = (max(left, 0), max(top, 0), min(right, sw), min(bottom, sh))
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                w.screenshot_screen(base.with_name(base.name + "-window.png"), bbox)
            if printwindow:
                ok, bright = w.screenshot_printwindow(self.hwnd, base.with_name(base.name + "-printwindow.png"))
                self.rep.note(f"printwindow {tag}", f"ok={ok} mean_brightness={bright:.1f}")
        w.screenshot_screen(base.with_name(base.name + "-desktop.png"))

    def save_text(self, tag: str, text: str) -> None:
        (self.out / f"{self.tag}-{tag}.txt").write_text(text, encoding="utf-8")

    def prepare_window(self) -> None:
        for line in w.dismiss_blockers(self.hwnd):
            self.rep.note("blocker", line)
        sw, sh = w.screen_size()
        width, height = min(sw - 20, 1280), min(sh - 60, 800)
        w.move_resize(self.hwnd, 0, 0, width, height)
        time.sleep(0.8)
        ok = w.focus(self.hwnd)
        fg = w.window_info(w.foreground()) if w.foreground() else None
        self.rep.check("focus window", ok, f"foreground={fg}", critical=True)

    def quit_check(self) -> bool:
        w.wait_for(self.window_gone, 20, "the window to close")
        return True

    def teardown(self) -> None:
        pids = self.tracked()
        if self.hwnd and w.is_window(self.hwnd):
            w.close_window(self.hwnd)
            time.sleep(1.0)
        if self.proc:
            killed = w.kill_tree(self.proc.pid)
            self.rep.note("teardown", f"killed by PID: {killed}")
        dirs = tuple(d.lower() for d in (*self.image_dirs, str(self.wxbrief.parent)))
        for p in psutil.process_iter(
            ["exe"]
        ):  # a process the tree walk missed (a hand-off): found by path, killed by PID
            with contextlib.suppress(psutil.Error):
                if (p.info["exe"] or "").lower().startswith(dirs):
                    self.rep.note("teardown", f"killed stray pid {p.pid} {p.info['exe']}")
                    p.kill()
        time.sleep(1.0)
        left = w.survivors(pids, (*self.image_dirs, str(self.wxbrief.parent)))
        self.rep.check("teardown leaves no survivors", not left, f"survivors={left}")


class Conhost(Term):
    name = "conhost"
    ROOT = "HKCU\\Console"
    KEYS = ("HKCU\\Console\\C:_Windows_System32_conhost.exe", "HKCU\\Console\\C:_Windows_System32_cmd.exe")
    LIGHT = {"ScreenColors": 0xF0, "PopupColors": 0xF5}  # black on white

    def _reg(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["reg", *args], check=False, capture_output=True, text=True)

    def _apply_light(self) -> None:
        """Default colours live in HKCU\\Console, and a per-title key wins over it: set both, remember the old values."""
        self.saved: dict[str, str | None] = {}
        for key in (self.ROOT, *self.KEYS):
            for name, val in self.LIGHT.items():
                q = self._reg("query", key, "/v", name)
                prev = re.search(r"REG_DWORD\s+(0x[0-9a-fA-F]+)", q.stdout)
                self.saved[f"{key}|{name}"] = prev.group(1) if prev else None
                self._reg("add", key, "/v", name, "/t", "REG_DWORD", "/d", hex(val), "/f")
        self.rep.note("light profile", f"conhost ScreenColors=0xf0 PopupColors=0xf5 under {self.ROOT} and {self.KEYS}")

    def teardown(self) -> None:
        super().teardown()
        for ident, prev in getattr(self, "saved", {}).items():
            key, name = ident.split("|")
            if prev is None:
                self._reg("delete", key, "/v", name, "/f")
            else:
                self._reg("add", key, "/v", name, "/t", "REG_DWORD", "/d", prev, "/f")
        if self.scheme == "light":
            for key in self.KEYS:
                self._reg("delete", key, "/f")

    def launch(self) -> None:
        self.write_run_cmd("mode con: cols=120 lines=30")
        if self.scheme == "light":
            self._apply_light()
        self.snapshot_baseline()
        conhost = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "conhost.exe"
        self.proc = self.spawn([str(conhost), "cmd.exe", "/c", str(self.run_cmd)])
        self.hwnd = self.find_window("ConsoleWindowClass")
        self.rep.note("window", str(w.window_info(self.hwnd)))
        self.note_integrity()
        self._uia = w.UiaText(self.hwnd)

    def read(self) -> str:
        return self._uia.read()

    def rect_of(self, text: str) -> tuple[int, int, int, int] | None:
        return self._uia.find_rect(text)


WT_LIGHT_SCHEME = {
    "name": "wx-light",
    "background": "#FFFFFF",
    "foreground": "#000000",
    "cursorColor": "#000000",
    "selectionBackground": "#ADD6FF",
    "black": "#383A42",
    "red": "#E45649",
    "green": "#50A14F",
    "yellow": "#C18401",
    "blue": "#0184BC",
    "purple": "#A626A4",
    "cyan": "#0997B3",
    "white": "#FAFAFA",
    "brightBlack": "#4F525D",
    "brightRed": "#DF6C75",
    "brightGreen": "#98C379",
    "brightYellow": "#E4C07A",
    "brightBlue": "#61AFEF",
    "brightPurple": "#C577DD",
    "brightCyan": "#56B5C1",
    "brightWhite": "#FFFFFF",
}


class WindowsTerminal(Term):
    name = "wt"

    def launch(self) -> None:
        assert self.tool_exe
        (self.tool_exe.parent / ".portable").write_text("", encoding="ascii")  # portable mode: settings beside the exe
        self.image_dirs = (str(self.tool_exe.parent),)
        defaults: dict[str, Any] = {}
        cmd_guid = "{0caa0dad-35be-5f56-a8ff-afceeeaa6101}"  # the built-in Command Prompt profile
        settings: dict[str, Any] = {
            "$schema": "https://aka.ms/terminal-profiles-schema",
            "defaultProfile": cmd_guid,
            "profiles": {
                "defaults": defaults,
                "list": [{"guid": cmd_guid, "name": "Command Prompt", "commandline": r"%SystemRoot%\System32\cmd.exe"}],
            },
            "schemes": [],
        }
        if self.scheme == "light":  # an explicit scheme, so the result does not depend on a built-in's name
            settings["schemes"].append(WT_LIGHT_SCHEME)
            defaults["colorScheme"] = WT_LIGHT_SCHEME["name"]
            settings["theme"] = "light"
        if self.font_pt:
            defaults["font"] = {"size": self.font_pt}
        cfg = self.tool_exe.parent / "settings"
        cfg.mkdir(exist_ok=True)
        text = json.dumps(settings, indent=2)
        (cfg / "settings.json").write_text(text, encoding="utf-8")
        # Where an unpackaged build looks when portable mode is not recognised: write there too, the proof says which won.
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows Terminal"
        if os.environ.get("LOCALAPPDATA"):
            local.mkdir(parents=True, exist_ok=True)
            (local / "settings.json").write_text(text, encoding="utf-8")
        self.rep.note("wt settings", json.dumps(settings)[:400])
        self.write_run_cmd()
        self.snapshot_baseline()
        self.proc = self.spawn([str(self.tool_exe), "cmd.exe", "/c", str(self.run_cmd)])
        self.hwnd = self.find_window("CASCADIA_HOSTING_WINDOW_CLASS")
        with contextlib.suppress(Exception):
            listing = sorted(p.name for p in (self.tool_exe.parent).iterdir() if p.name in {".portable", "settings"})
            self.rep.note("wt portable files", f"{listing}; settings dir: {sorted(p.name for p in cfg.iterdir())}")
        self.rep.note("window", str(w.window_info(self.hwnd)))
        self.note_integrity()
        self._uia = w.UiaText(self.hwnd)

    def read(self) -> str:
        return self._uia.read()

    def rect_of(self, text: str) -> tuple[int, int, int, int] | None:
        return self._uia.find_rect(text)


class VSCode(Term):
    name = "vscode"

    @property
    def ps(self) -> str:
        return "& " if self.profile == "powershell" else ""

    SETTINGS = {
        "security.workspace.trust.enabled": False,
        "workbench.startupEditor": "none",
        "workbench.tips.enabled": False,
        "workbench.welcomePage.walkthroughs.openOnInstall": False,
        "telemetry.telemetryLevel": "off",
        "update.mode": "none",
        "extensions.autoUpdate": False,
        "extensions.autoCheckUpdates": False,
        "window.restoreWindows": "none",
        "window.confirmBeforeClose": "never",
        "chat.disableAIFeatures": True,
        "terminal.integrated.defaultProfile.windows": "Command Prompt",
        "terminal.integrated.fontSize": 14,
        "terminal.integrated.gpuAcceleration": "off",
        "terminal.integrated.enablePersistentSessions": False,
        "terminal.integrated.confirmOnExit": "never",
        "editor.accessibilitySupport": "on",
    }
    KEYBINDINGS = [
        {"key": "ctrl+alt+a", "command": "workbench.action.terminal.selectAll", "when": "terminalFocus"},
        {"key": "ctrl+alt+c", "command": "workbench.action.terminal.copySelection", "when": "terminalFocus"},
        {"key": "ctrl+alt+x", "command": "workbench.action.terminal.clearSelection", "when": "terminalFocus"},
        {"key": "ctrl+alt+m", "command": "workbench.action.toggleMaximizedPanel"},
        {"key": "ctrl+alt+j", "command": "workbench.action.closeSidebar"},
        {"key": "ctrl+alt+k", "command": "workbench.action.closeAuxiliaryBar"},
    ]

    def launch(self) -> None:
        assert self.tool_exe
        root = self.tool_exe.parent
        user = root / "data" / "user-data" / "User"
        user.mkdir(parents=True, exist_ok=True)
        settings = dict(self.SETTINGS)
        if self.profile == "powershell":
            settings["terminal.integrated.defaultProfile.windows"] = "PowerShell"
            self.rep.note("profile", "VS Code default terminal profile: PowerShell")
        if self.scheme == "light":
            settings["workbench.colorTheme"] = "Default Light Modern"
            self.rep.note("light profile", "VS Code settings.json: workbench.colorTheme = Default Light Modern")
        (user / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        (user / "keybindings.json").write_text(json.dumps(self.KEYBINDINGS, indent=2), encoding="utf-8")
        self.image_dirs = (str(root),)
        self.write_run_cmd()
        folder = self.work / "folder"
        folder.mkdir(exist_ok=True)
        self.snapshot_baseline()
        self.proc = self.spawn(
            [
                str(self.tool_exe),
                "--disable-extensions",
                "--disable-workspace-trust",
                "--skip-release-notes",
                "--skip-welcome",
                "--force-renderer-accessibility",
                "--new-window",
                str(folder),
            ]
        )
        self.hwnd = self.find_window("Chrome_WidgetWin_1", timeout=90)
        self.rep.note("window", str(w.window_info(self.hwnd)))
        self.note_integrity()
        self._uia: w.UiaText | None = None

    def prepare_window(self) -> None:
        super().prepare_window()
        w.maximize(self.hwnd)
        time.sleep(15)  # a first start builds caches; there is no ready signal on a window with no UIA text yet
        w.focus(self.hwnd)
        self.shot("pre-terminal")
        w.chord(w.VK_CONTROL, VK_BACKTICK)  # Ctrl+` opens and focuses the integrated terminal
        time.sleep(4)
        # Close both side bars, then maximise the panel, so the TUI gets the whole window (it needs 60x16 at least).
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_J)
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_K)
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_M)
        time.sleep(2)
        self.shot("terminal-open")
        self.shot("terminal-maximised")
        self.geom = self.calibrate()
        w.send_text(f'{self.ps}"{self.run_cmd}"\n', delay=0.02)

    def read(self) -> str:
        w.clear_clipboard()
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_A)
        time.sleep(0.4)
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_C)
        time.sleep(0.4)
        text = w.clipboard_text()
        w.chord(w.VK_CONTROL, w.VK_MENU, VK_X)
        return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))

    # --- geometry: VS Code exposes no terminal cells through UIA, so the pixel -> cell map is measured with clicks
    def _probe_click(self, x: int, y: int, log: Path) -> tuple[int, int] | None:
        """Click a pixel and return the (col, row) the probe in the terminal reports, or None if nothing arrived."""
        before = len(log.read_text(encoding="ascii").splitlines()) if log.exists() else 0
        w.click_at(x, y, approach=1)
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            if log.exists():
                lines = log.read_text(encoding="ascii").splitlines()
                if len(lines) > before:
                    col, row = lines[-1].split()
                    return int(col), int(row)
            time.sleep(0.05)
        return None

    def calibrate(self) -> tuple[float, float, float, float]:
        """Return (origin_x, origin_y, cell_w, cell_h) in screen pixels, measured by clicking with a mouse probe."""
        probe = Path(__file__).with_name("probe_mouse.py")
        log = self.work / "probe.log"
        log.unlink(missing_ok=True)
        w.send_text(f'{self.ps}"{sys.executable}" "{probe}" "{log}"\n', delay=0.02)
        time.sleep(3)
        left, top, right, bottom = w.window_info(self.hwnd).rect
        left, top = max(left, 0), max(top, 0)

        def hit(x: int, y: int) -> tuple[int, int] | None:
            return self._probe_click(int(x), int(y), log)

        start = None  # a point inside the terminal viewport: scan around the middle of the window
        for fx, fy in ((0.5, 0.5), (0.4, 0.6), (0.6, 0.4), (0.3, 0.5), (0.7, 0.6), (0.5, 0.3)):
            x0, y0 = int(left + (right - left) * fx), int(top + (bottom - top) * fy)
            got = hit(x0, y0)
            if got:
                start = (x0, y0, got)
                break
        if start is None:
            raise Abort("calibration: the probe never reported a click (mouse reporting not reaching the terminal)")
        x0, y0, (c0, r0) = start

        def edge(axis: int, p0: int, a0: int, direction: int) -> tuple[int, int]:
            """Step from p0 until the cell index on ``axis`` changes, then bisect; returns (first pixel of the new cell side, new index)."""

            def at(p: int) -> tuple[int, int] | None:
                return hit(p, y0) if axis == 0 else hit(x0, p)

            lo, hi, idx_hi = p0, p0, a0
            while idx_hi == a0:
                hi = lo + direction * 10
                got = at(hi)
                if got is None:
                    raise Abort("calibration: ran off the terminal viewport")
                if got[axis] == a0:
                    lo = hi
                else:
                    idx_hi = got[axis]
            while abs(hi - lo) > 1:
                mid = (lo + hi) // 2
                got = at(mid)
                if got is None:
                    raise Abort("calibration: lost the viewport during the search")
                if got[axis] == a0:
                    lo = mid
                else:
                    hi, idx_hi = mid, got[axis]
            return hi, idx_hi

        ex, cx_next = edge(0, x0, c0, +1)  # left edge of column c0+1 (first pixel of it)
        ey, ry_next = edge(1, y0, r0, +1)
        far_x = min(x0 + 520, right - 40)
        far_got = hit(far_x, y0)
        if far_got is None:
            raise Abort("calibration: the far x point is outside the viewport")
        fx_last, _ = edge(0, far_x, far_got[0], -1)  # last pixel of the previous column, so the far cell starts +1
        far_y = min(y0 + 260, bottom - 60)
        far_got_y = hit(x0, far_y)
        if far_got_y is None:
            raise Abort("calibration: the far y point is outside the viewport")
        fy_last, _ = edge(1, far_y, far_got_y[1], -1)
        if far_got[0] == cx_next or far_got_y[1] == ry_next:
            raise Abort("calibration: far point in the same cell as the near one")
        cw = (fx_last + 1 - ex) / (far_got[0] - cx_next)
        ch = (fy_last + 1 - ey) / (far_got_y[1] - ry_next)
        ox = ex - (cx_next - 1) * cw
        oy = ey - (ry_next - 1) * ch
        w.send_text("q")
        time.sleep(1.0)
        self.rep.note("calibration", f"origin=({ox:.1f},{oy:.1f}) cell={cw:.2f}x{ch:.2f} px (probe clicks, probe.log)")
        return ox, oy, cw, ch

    def rect_of(self, text: str) -> tuple[int, int, int, int] | None:
        grid = self.read().split("\n")
        row = next((i for i, ln in enumerate(grid) if re.search(rf"{text}\s+(L?IFR|MVFR|VFR)", ln)), None)
        if row is None:
            return None
        col = grid[row].index(text)
        ox, oy, cw, ch = self.geom
        return (
            round(ox + col * cw),
            round(oy + row * ch),
            round(ox + (col + len(text)) * cw),
            round(oy + (row + 1) * ch),
        )

    def window_gone(self) -> bool:
        return False  # VS Code stays open; the terminal returns to its prompt

    def quit_check(self) -> bool:
        def prompt_back() -> bool:
            text = self.read()
            return bool(re.search(r">\s*$", text.rstrip())) and not re.search(START_RE, text)

        w.wait_for(prompt_back, 25, "the cmd prompt to return and the TUI to be gone")
        return True


TERMS: dict[str, type[Term]] = {"conhost": Conhost, "wt": WindowsTerminal, "vscode": VSCode}


# ---------------------------------------------------------------------------------------------------------------------


def diagnostics(out: Path) -> dict[str, Any]:
    d: dict[str, Any] = {
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "os_version": platform.version(),
        "screen": w.screen_size(),
        "idle_seconds": round(w.idle_seconds(), 1),
    }
    with contextlib.suppress(Exception):
        sid = w.wt.DWORD()
        w.kernel32.ProcessIdToSessionId(os.getpid(), w.ctypes.byref(sid))
        d["session_id"] = sid.value
    with contextlib.suppress(Exception):
        d["is_admin"] = bool(w.ctypes.windll.shell32.IsUserAnAdmin())
    with contextlib.suppress(Exception):
        d["dpi"] = int(w.user32.GetDpiForSystem())
    with contextlib.suppress(Exception):
        groups = subprocess.run(["whoami", "/groups"], capture_output=True, text=True, timeout=30).stdout
        d["integrity"] = ", ".join(re.findall(r"Mandatory Label\\\S+ \S+", groups))
    with contextlib.suppress(Exception):
        reg = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "/v", "EnableLUA"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        d["EnableLUA"] = reg.strip().split()[-1] if reg.strip() else "?"
    fg = w.foreground()
    d["foreground_window"] = str(w.window_info(fg)) if fg else "none"
    d["visible_top_level_windows"] = sum(1 for x in w.all_windows() if x.visible and x.title)
    with contextlib.suppress(Exception):
        d["console_cp"] = subprocess.run(
            ["cmd", "/c", "chcp"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    return d


def journey(t: Term, rep: Report) -> None:
    t.launch()
    t.prepare_window()
    t.prove_profile()
    last = {"text": ""}

    def drawn() -> str:
        last["text"] = t.read()
        ok = re.search(START_RE, last["text"]) and re.search(INITIAL_RE, last["text"])
        return last["text"] if ok else ""

    try:
        text = str(w.wait_for(drawn, 90, "the wxbrief list screen"))
    except TimeoutError:
        t.save_text("00-last-read", last["text"])
        t.shot("00-not-drawn", printwindow=True)
        uia = getattr(t, "_uia", None)
        if uia is not None:
            with contextlib.suppress(Exception):
                rep.note("uia candidates", " | ".join(getattr(uia, "candidates", [])) or "none")
            with contextlib.suppress(Exception):
                w.dump_uia_tree(t.hwnd, t.out / f"{t.tag}-uia-tree.txt", max_depth=12, max_nodes=500)
        raise
    t.save_text("01-start", text)
    t.shot("01-start", printwindow=True)
    rep.check("app drawn", True, f"{len(text.splitlines())} lines read", critical=True)
    rep.check("initial selection is the first station", bool(re.search(INITIAL_RE, text)) and "KDEN" in text)
    uia = getattr(t, "_uia", None)
    if uia is not None:
        rep.note("uia text element", uia.describe())
        rep.note("uia candidates", " | ".join(getattr(uia, "candidates", [])))
    with contextlib.suppress(Exception):
        n = w.dump_uia_tree(t.hwnd, t.out / f"{t.tag}-uia-tree.txt", max_depth=12, max_nodes=500)
        rep.note("uia tree", f"{n} nodes")

    def keys(label: str) -> str:
        w.focus(t.hwnd)
        sent = w.send_text(label) if label.isprintable() else 0
        rep.note(f"send {label!r}", f"{sent} input events inserted")
        time.sleep(1.2)
        t.after_keys()
        return t.read()

    after2 = keys("2")
    t.save_text("02-after-2", after2)
    t.shot("02-after-2")
    rep.note("key 2 changed the screen", str(after2 != text))

    after_v = keys("v")
    t.save_text("03-after-v", after_v)
    t.shot("03-after-v")
    rep.check("v switches to the grid (cards show the station name)", bool(re.search(GRID_RE, after_v)))
    back = keys("v")
    if not re.search(LIST_RE, back) or re.search(GRID_RE, back):  # the clipboard read can lag one frame: read once more
        time.sleep(2)
        back = t.read()
    rep.check("v again returns to the list", bool(re.search(LIST_RE, back)) and not re.search(GRID_RE, back))

    w.focus(t.hwnd)
    rect = None
    for _ in range(3):
        rect = t.rect_of(TARGET_ROW)
        if rect:
            break
        time.sleep(1)
    rep.check("located the row in pixels", rect is not None, f"rect of {TARGET_ROW!r} = {rect}", critical=True)
    assert rect
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    rep.note("click point", f"({cx}, {cy}) from rect {rect}; window {w.window_info(t.hwnd).rect}")
    inserted = w.click_at(cx, cy)
    rep.note("click", f"{inserted} mouse events inserted")
    time.sleep(1.5)
    t.after_keys()
    seen = ""

    def selected() -> bool:
        nonlocal seen
        seen = t.read()
        return all(re.search(p, seen) for p in AFTER_CLICK)

    try:
        w.wait_for(selected, 10, "the clicked row to be selected")
        rep.check("clicked row becomes selected (4/10 and UUEE detail header)", True)
    except TimeoutError:
        rep.check("clicked row becomes selected (4/10 and UUEE detail header)", False, "last screen saved")
    t.save_text("04-after-click", seen)
    t.shot("04-after-click", printwindow=True)

    w.focus(t.hwnd)
    w.send_text("q")
    time.sleep(0.5)
    try:
        t.quit_check()
        rep.check("q exits the app", True)
    except TimeoutError as exc:
        rep.check("q exits the app", False, str(exc))
    t.shot("05-after-q")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("terminal", choices=sorted(TERMS))
    ap.add_argument("--wxbrief", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tool-exe", type=Path)
    ap.add_argument("--scheme", choices=["dark", "light"], default="dark")
    ap.add_argument("--limited", action="store_true", help="start the terminal with a filtered (non-elevated) token")
    ap.add_argument(
        "--medium-spawn", action="store_true", help="start the terminal at Medium integrity (restricted token)"
    )
    ap.add_argument("--require-medium", action="store_true", help="fail unless the terminal window process is Medium")
    ap.add_argument("--profile", choices=["cmd", "powershell"], default="cmd", help="VS Code terminal profile")
    ap.add_argument("--tag", default="", help="suffix for result and artifact names")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rep = Report(
        args.terminal
        + ("-limited" if args.limited else "")
        + ("-light" if args.scheme == "light" else "")
        + (f"-{args.tag}" if args.tag else ""),
        args.out,
    )
    rep.info = diagnostics(args.out)
    w.log(f"diagnostics: {json.dumps(rep.info)}")
    term = TERMS[args.terminal](
        rep,
        args.wxbrief.resolve(),
        args.out,
        args.tool_exe.resolve() if args.tool_exe else None,
        args.limited,
        args.scheme,
        medium_spawn=args.medium_spawn,
        require_medium=args.require_medium,
        profile=args.profile,
        tag_suffix=args.tag,
    )
    try:
        journey(term, rep)
    except Abort as exc:
        w.log(f"aborted at: {exc}")
    except Exception:
        rep.check("unexpected error", False, traceback.format_exc()[-1500:])
    finally:
        with contextlib.suppress(Exception):
            term.shot("99-before-teardown")
        with contextlib.suppress(Exception):
            term.teardown()
        rep.info["leftover_python_procs"] = len(
            [p for p in psutil.process_iter(["name"]) if p.info["name"] == "wxbrief.exe"]
        )
        rep.write()
    return 0 if rep.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
