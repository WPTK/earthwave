"""Cold-start capture: a new user following only the README, on a fresh runner, through the 20 cold-start tasks.

    python scripts/uat/cold_start.py --out DIR [--long-minutes 15]

Nothing here judges. Every step writes plain text to <out>/NN-name.txt: the command (or the keys sent), its stdout, and
the screen text after the keys. A separate reader decides what it means. Real network, real profile of the (throw-away)
runner, release binary of the README's "Release binary" bullet, plus the README's `pip install git+...` path in a venv.
The keys come from the README and the app's own help screen; nothing from source or tests informs the steps.

The terminal is the tui-test kit (pseudo-terminal, ConPTY on Windows): it is the driver, not a real Terminal.app/Windows
Terminal, and the capture says so in 00-environment.txt.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts" / "uat"))

import readme_cmds  # noqa: E402
from uat_kit import Terminal  # noqa: E402

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
TAG = "v0.2.0"
BASE_URL = f"https://github.com/WPTK/earthwave/releases/download/{TAG}"
SIZE = (130, 32)


class Cap:
    def __init__(self, out: Path) -> None:
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.file: Path | None = None
        self.results: list[str] = []

    def step(self, name: str) -> None:
        self.n += 1
        self.file = self.out / f"{self.n:02d}-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}.txt"
        self.write(f"STEP {self.n}: {name}\n")

    def write(self, text: str) -> None:
        assert self.file is not None
        with self.file.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def cmd(self, argv: list[str], timeout: float = 300, cwd: Path | None = None, env: dict | None = None) -> str:
        self.write(f"$ {' '.join(argv)}")
        try:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=cwd,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
            self.write(f"[exit {p.returncode}]\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}")
            return p.stdout + p.stderr
        except Exception as exc:  # a missing command or a timeout is part of the capture
            self.write(f"[{type(exc).__name__}: {exc}]")
            return ""

    def keys(self, t: Terminal, *keys: str, wait: float = 1.2, label: str = "") -> str:
        self.write(f">>> keys: {' '.join(keys)} {label}")
        if keys:
            with contextlib.suppress(Exception):
                t.send_keys(*keys)
        return self.screen(t, wait)

    def typed(self, t: Terminal, text: str, *, wait: float = 1.2) -> str:
        self.write(f">>> typed: {text!r}")
        for ch in text:
            t.send_keys("space" if ch == " " else ch)
        return self.screen(t, wait)

    def screen(self, t: Terminal, wait: float = 1.2) -> str:
        with contextlib.suppress(Exception):
            t.settle(wait)
        try:
            text = t.screen_text()
        except Exception as exc:
            text = f"[no screen: {exc}]"
        alive = ""
        with contextlib.suppress(Exception):
            alive = f"[process alive: {t.alive()}]"
        self.write(f"--- screen {SIZE[0]}x{SIZE[1]} {alive} ---\n{text}\n--- end screen ---")
        return text

    def ok(self, name: str, ok: bool, note: str = "") -> None:
        self.results.append(f"{self.n:02d} {name}: {'captured' if ok else 'CAPTURE FAILED'} {note}")


def launch(exe: str, *args: str, env: dict | None = None) -> Terminal:
    e = {"WT_SESSION": "coldstart"} if IS_WIN else {}
    e.update(env or {})
    return Terminal([exe, *args], SIZE[0], SIZE[1], env=e, env_remove=("NO_COLOR",), isolate=False)


def wait_dashboard(t: Terminal, timeout: float = 120) -> bool:
    try:
        t.wait_for(lambda s: "shown of" in s.text, timeout, "dashboard")
        return True
    except Exception:
        return False


def wait_quakes(t: Terminal, timeout: float = 120) -> bool:
    try:
        t.wait_for(lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)), timeout, "quakes listed")
        return True
    except Exception:
        return False


def quit_app(t: Terminal) -> None:
    with contextlib.suppress(Exception):
        t.send_keys("q")
        t.wait_exit(10)
    with contextlib.suppress(Exception):
        t.close()


def home_dir() -> Path:
    return Path.home()


def find_new_files(before: set[Path]) -> list[Path]:
    roots = [home_dir()]
    if IS_WIN:
        roots += [Path(os.environ.get("APPDATA", "")), Path(os.environ.get("LOCALAPPDATA", ""))]
    found: set[Path] = set()
    for r in roots:
        if not r or not r.exists():
            continue
        for dirpath, dirnames, files in os.walk(r):
            dirnames[:] = [
                d for d in dirnames if d not in {"node_modules", ".git", "site-packages", "AppData"} or r != home_dir()
            ]
            if len(Path(dirpath).parts) - len(r.parts) > 5:
                dirnames[:] = []
            for f in files:
                if "earthwave" in dirpath.lower() or "earthwave" in f.lower():
                    found.add(Path(dirpath) / f)
    return sorted(found - before)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--long-minutes", type=float, default=15)
    a = ap.parse_args()
    cap = Cap(Path(a.out))
    work = Path(tempfile.mkdtemp(prefix="cold-"))
    exe = ""
    config_path: Path | None = None

    def guarded(name: str, fn) -> None:
        cap.step(name)
        try:
            ok = fn()
            cap.ok(name, ok is not False)
        except Exception:
            cap.write("[script error]\n" + traceback.format_exc())
            cap.ok(name, False, "script error")

    # -- environment ------------------------------------------------------------------------------------------------
    cap.step("environment")
    cap.write(f"platform: {platform.platform()} machine={platform.machine()} python={sys.version.split()[0]}")
    cap.write("driver: tui-test pseudo-terminal kit (not a real Terminal.app / Windows Terminal window)")
    cap.write(f"size: {SIZE[0]}x{SIZE[1]}  HOME={home_dir()}")
    cap.write("target: the README's release-binary bullet (v0.2.0) for this OS")
    pre_files = set(find_new_files(set()))

    # -- 2 install ---------------------------------------------------------------------------------------------------
    def install() -> bool:
        nonlocal exe
        if IS_WIN:
            asset = f"earthwave-{TAG}-windows-x86_64.exe"
        elif IS_MAC:
            asset = f"earthwave-{TAG}-macos-arm64.tar.gz"
        else:
            asset = f"earthwave-{TAG}-linux-x86_64.tar.gz"
        dl = work / "download"
        dl.mkdir()
        cap.cmd(["curl", "-fsSL", "-o", str(dl / asset), f"{BASE_URL}/{asset}"], cwd=dl)
        cap.cmd(["curl", "-fsSL", "-o", str(dl / "SHA256SUMS"), f"{BASE_URL}/SHA256SUMS"], cwd=dl)
        sums = (dl / "SHA256SUMS").read_text() if (dl / "SHA256SUMS").exists() else ""
        want = next((ln.split()[0] for ln in sums.splitlines() if asset in ln), "?")
        if IS_WIN:
            got = cap.cmd(["certutil", "-hashfile", str(dl / asset), "SHA256"]).splitlines()
            got = got[1].strip() if len(got) > 1 else "?"
        else:
            got = cap.cmd(["shasum", "-a", "256", str(dl / asset)]).split()[0:1]
            got = got[0] if got else "?"
        cap.write(f"SHA256SUMS says {want}; the download is {got}; match={want == got}")
        if asset.endswith(".tar.gz"):
            cap.cmd(["tar", "xzf", asset], cwd=dl)
            exe = str(dl / "earthwave")
            if IS_MAC:
                cap.cmd(["xattr", "-l", exe])  # no quarantine flag comes from curl; a browser download would have it
            os.chmod(exe, 0o755)
        else:
            exe = str(dl / asset)
        cap.cmd([exe, "--version"])
        cap.cmd([exe, "--selftest"], timeout=180)
        return Path(exe).exists()

    guarded("2 install the release binary, check it", install)

    def pip_path() -> None:
        v = work / "pipvenv"
        cap.cmd([sys.executable, "-m", "venv", str(v)])
        py = str(v / ("Scripts/python.exe" if IS_WIN else "bin/python"))
        cap.cmd([py, "-m", "pip", "install", "git+https://github.com/WPTK/earthwave.git"], timeout=600)
        cap.cmd([py, "-m", "earthwave", "--version"])

    guarded("2b install from source with pip, as the README says", pip_path)
    if not exe:
        cap.step("stop")
        cap.write("no binary installed; the remaining steps cannot run")
        return write_summary(cap)

    # -- 1 first start -----------------------------------------------------------------------------------------------
    def first_start() -> bool:
        nonlocal config_path
        t = launch(exe)
        try:
            ok = wait_quakes(t, 150)
            cap.screen(t, 2)
            new = find_new_files(pre_files)
            cap.write("files created under the profile that mention earthwave:\n" + "\n".join(f"  {p}" for p in new))
            for p in new:
                if p.suffix == ".toml" and config_path is None:
                    config_path = p
            return ok
        finally:
            quit_app(t)

    guarded("1 first start, where settings are saved", first_start)
    if config_path:
        cap.write(f"config file found: {config_path}")

    # -- 3, 4 strongest quake, detail, copy, back --------------------------------------------------------------------
    def strongest() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.screen(t, 1)
            cap.keys(t, "s", label="(sort)")
            cap.keys(t, "s", label="(sort again)")
            cap.keys(t, "enter", label="(open detail of the selected row)")
            cap.keys(t, "y", label="(copy link)")
            cap.keys(t, "escape", label="(back)")
            return ok
        finally:
            quit_app(t)

    guarded("3-4 strongest quake, details, copy link, back", strongest)

    # -- 5, 6 filter ---------------------------------------------------------------------------------------------------
    def filt() -> bool:
        t = launch(exe, "--min-mag", "4.5", "--hours", "72")
        try:
            ok = wait_quakes(t)
            cap.screen(t, 1)
            cap.write("(above: with the command-line options)")
        finally:
            quit_app(t)
        t = launch(exe)
        try:
            ok = wait_quakes(t) and ok
            cap.keys(t, "f", label="(filters screen)")
            cap.keys(t, "escape")
            return ok
        finally:
            quit_app(t)

    guarded("5 filter M4.5 and above 72 hours, option and in-app", filt)

    def filt_bad() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "f")
            cap.typed(t, "zzz")
            cap.keys(t, "tab", label="(next field)")
            cap.typed(t, "-5")
            cap.keys(t, "enter", label="(apply)")
            cap.keys(t, "escape", label="(cancel)")
            return ok
        finally:
            quit_app(t)

    guarded("6 invalid filter input then cancel", filt_bad)

    # -- 7 search ---------------------------------------------------------------------------------------------------
    def search() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "slash")
            cap.typed(t, "Japan")
            cap.keys(t, "enter")
            cap.keys(t, "slash")
            cap.typed(t, "Zzzzqqq")
            cap.keys(t, "enter", label="(a place with no quakes)")
            cap.keys(t, "slash")
            t.send_raw("")  # ctrl+u clears a text box
            cap.write(">>> keys: ctrl+u (clear the box)")
            cap.keys(t, "enter", label="(search with an empty box)")
            return ok
        finally:
            quit_app(t)

    guarded("7 search Japan, no-match place, clear", search)

    # -- 8 home ----------------------------------------------------------------------------------------------------
    def home() -> bool:
        t = launch(exe, "--home", "40.7,-74.0")
        try:
            ok = wait_quakes(t)
            cap.screen(t, 1)
            cap.keys(t, "enter", label="(latest quake detail: distance from home)")
            return ok
        finally:
            quit_app(t)

    guarded("8 home New York, distance to the latest quake", home)

    # -- 9 map ---------------------------------------------------------------------------------------------------------
    def the_map() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "m", label="(hide map)")
            cap.keys(t, "m", label="(show map)")
            cap.keys(t, "M", label="(shift+M: full screen, if that is what the help says)")
            cap.keys(t, "escape")
            for i in range(10):
                cap.keys(t, "z", wait=0.8, label=f"(zoom {i + 1})")
            return ok
        finally:
            quit_app(t)

    guarded("9 map show hide full screen zoom", the_map)

    # -- 10, 11, 12 ------------------------------------------------------------------------------------------------
    def tsunami() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.screen(t, 1)
            cap.write("(above: the dashboard status/sources)")
            cap.keys(t, "t", label="(tsunami alerts and bulletins)")
            cap.keys(t, "enter", label="(read a bulletin if there is one)")
            cap.keys(t, "escape")
            cap.keys(t, "?", label="(help)")
            cap.keys(t, "escape")
            return ok
        finally:
            quit_app(t)

    guarded("10-11 tsunami alerts, bulletin, source health", tsunami)

    def gauges() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "o", label="(sea-level gauges)")
            cap.keys(t, "enter", label="(look at one gauge)")
            cap.keys(t, "escape")
            return ok
        finally:
            quit_app(t)

    guarded("12 sea-level gauge", gauges)

    # -- 13 history ----------------------------------------------------------------------------------------------
    def history() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "h", label="(history by day)")
            cap.keys(t, "left", label="(earlier day)")
            cap.keys(t, "enter", label="(open a quake from that day)")
            cap.keys(t, "escape")
            return ok
        finally:
            quit_app(t)

    guarded("13 history by day", history)

    # -- 14 offline -----------------------------------------------------------------------------------------------
    def offline() -> bool:
        t = launch(exe, "--offline")
        try:
            ok = wait_dashboard(t)
            cap.screen(t, 3)
            cap.keys(t, "r", label="(refresh while offline)")
            cap.keys(t, "t", label="(tsunami screen offline)")
            return ok
        finally:
            quit_app(t)

    guarded("14 offline", offline)

    # -- 15 record, replay --------------------------------------------------------------------------------------
    def record_replay() -> bool:
        rec = work / "session.jsonl"
        t = launch(exe, "--record", str(rec))
        try:
            ok = wait_quakes(t)
            cap.keys(t, "r", wait=5)
        finally:
            quit_app(t)
        cap.write(f"recording: {rec} exists={rec.exists()} size={rec.stat().st_size if rec.exists() else 0}")
        t = launch(exe, "--replay", str(rec))
        try:
            ok = wait_dashboard(t, 90) and ok
            cap.screen(t, 4)
            cap.write("(above: playing the recording back; does anything on screen say it is a recording?)")
            return ok
        finally:
            quit_app(t)

    guarded("15 record, quit, replay", record_replay)

    # -- 17 persistence, 18 settings mistakes ---------------------------------------------------------------------
    def restart() -> bool:
        t = launch(exe, "--home", "40.7,-74.0")
        try:
            ok = wait_quakes(t)
            cap.keys(t, "j", label="(move down)")
            cap.keys(t, "m", label="(hide the map)")
        finally:
            quit_app(t)
        t = launch(exe)
        try:
            ok = wait_quakes(t) and ok
            cap.screen(t, 1)
            cap.write("(above: second start, no options; what was kept?)")
            return ok
        finally:
            quit_app(t)

    guarded("17 quit, start again, what was kept", restart)

    def bad_config() -> bool:
        if not config_path or not config_path.exists():
            cap.write("no config file was found in step 1; cannot corrupt it")
            return False
        backup = config_path.read_text(encoding="utf-8", errors="replace")
        cap.write(f"config before:\n{backup}")
        ok = True
        try:
            for label, text in (
                ("syntax error", backup + "\n[[[ this is not toml\n"),
                (
                    "wrong value type",
                    re.sub(r"(?m)^(\s*(?!#)\w+\s*=\s*)\d[\d.]*\s*$", r'\1"oops"', backup, count=1),
                ),
            ):
                config_path.write_text(text, encoding="utf-8")
                cap.write(f"== variant: {label} ==")
                out = cap.cmd([exe, "--version"])
                cap.write(f"(--version output above: {out.strip()[:80]!r})")
                t = launch(exe)
                try:
                    ok = wait_dashboard(t, 60) and ok
                    cap.screen(t, 3)
                finally:
                    quit_app(t)
        finally:
            config_path.write_text(backup, encoding="utf-8")
        return ok

    guarded("18 settings file with a mistake", bad_config)

    # -- 19 bug report -----------------------------------------------------------------------------------------------
    def bug() -> bool:
        cap.cmd([exe, "--version"])
        h = cap.cmd([exe, "--help"])
        cap.write(
            "lines of --help mentioning log, debug, bug, issue: "
            + str([ln for ln in h.splitlines() if re.search(r"log|debug|bug|issue", ln, re.I)])
        )
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "?", label="(help screen)")
            return ok
        finally:
            quit_app(t)

    guarded("19 how to report a bug: version and log", bug)

    # -- 20 every key of the help screen ---------------------------------------------------------------------------
    def every_key() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            help_text = cap.keys(t, "?", label="(help)")
            listed = sorted(readme_cmds.help_screen_keys(help_text))
            cap.write(f"keys read from the help screen: {listed}")
            cap.keys(t, "escape")
            for k in listed:
                if k in {"q", "?"}:
                    continue
                name = {"Enter": "enter", "Tab": "tab", "arrows": "down", "/": "slash", "Esc": "escape"}.get(k, k)
                cap.keys(t, name, wait=1.0, label=f"(help lists {k})")
                cap.keys(t, "escape", wait=0.6, label="(back out)")
            return ok
        finally:
            quit_app(t)

    guarded("20 every key the help lists", every_key)

    # -- 16 fifteen minutes (last: it is the long one) ------------------------------------------------------------------
    def long_run() -> bool:
        t = launch(exe)
        try:
            ok = wait_quakes(t)
            cap.keys(t, "j", "j", "j", label="(select the fourth quake)")
            start = time.monotonic()
            total = a.long_minutes * 60
            shot = 0
            while True:
                el = time.monotonic() - start
                cap.write(f"=== t+{el / 60:.1f} min ===")
                cap.screen(t, 0.5)
                shot += 1
                if el >= total:
                    break
                time.sleep(min(300, total - el))
            return ok
        finally:
            quit_app(t)

    guarded(f"16 leave it open {a.long_minutes:g} minutes on one selected quake", long_run)

    shutil.rmtree(work, ignore_errors=True)
    return write_summary(cap)


def write_summary(cap: Cap) -> int:
    cap.step("capture summary")
    cap.write("\n".join(cap.results))
    print("\n".join(cap.results))
    (cap.out / "SUMMARY.txt").write_text("\n".join(cap.results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
