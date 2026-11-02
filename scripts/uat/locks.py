"""Gilead shared locks for UAT runs, and a runner that holds one around a command under a 4 GB Job Object.

    python scripts/uat/locks.py heavy "S6 kit colour test" -- python -m pytest tests/uat_kit -p no:xdist

Acquire is an atomic mkdir of ``<locks>/gilead-heavy`` with an ``owner.json`` inside. If it is held, wait and retry.
A lock older than STALE_HOURS is reported and never stolen. Free RAM must be >= 6 GB first. Release is in ``finally``.
Windows only for the Job Object; elsewhere the command just runs under the lock.

    python scripts/uat/locks.py --wait 600 desktop "WT pixel pass" -- python tools/wt_pass.py

``desktop`` also needs the desktop idle for IDLE_SECONDS (GetLastInputInfo) before the lock is taken, and again every
IDLE_RECHECK seconds while the command runs; when someone touches the machine the run is stopped, the lock released and
the exit status is 4 (retry later). Options such as ``--wait`` go before the kind (argparse hands later ones to the command).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOCKS = Path(os.environ.get("UAT_LOCKS", r"C:\Users\billp\uat-shared\locks"))
STALE_HOURS = 3
MIN_FREE_GB = 6
CAP_BYTES = 4 * 1024**3


IDLE_SECONDS = 300
IDLE_RECHECK = 60


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input on the Windows desktop (GetLastInputInfo); inf elsewhere."""
    if sys.platform != "win32":
        return float("inf")
    import ctypes
    from ctypes import wintypes

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetLastInputInfo")
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def free_ram_gb() -> float:
    import psutil

    return psutil.virtual_memory().available / 1024**3


class Held(Exception):
    pass


class Lock:
    def __init__(self, name: str, purpose: str, session: str = "earthwave-uat-spikes") -> None:
        self.dir = LOCKS / name
        self.purpose, self.session, self.name = purpose, session, name
        self.mine = False

    def try_acquire(self) -> bool:
        LOCKS.mkdir(parents=True, exist_ok=True)
        try:
            self.dir.mkdir()  # atomic: exactly one caller succeeds
        except FileExistsError:
            return False
        self.mine = True
        start = datetime.now().astimezone().isoformat(timespec="seconds")
        owner = {"app": "earthwave", "session": self.session, "pid": os.getpid(), "start": start, "purpose": self.purpose}
        (self.dir / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
        return True

    def holder(self) -> dict:
        try:
            return json.loads((self.dir / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def age_hours(self) -> float:
        try:
            return (time.time() - self.dir.stat().st_mtime) / 3600
        except OSError:
            return 0.0

    def acquire(self, wait: float = 3600, every: float = 30) -> None:
        deadline = time.monotonic() + wait
        while not self.try_acquire():
            if self.age_hours() > STALE_HOURS:
                raise Held(f"{self.name} is {self.age_hours():.1f} h old, held by {self.holder()}: report to the manager, do not steal")
            if time.monotonic() > deadline:
                raise Held(f"{self.name} still held after {wait:.0f} s by {self.holder()}")
            who = self.holder()
            print(f"[locks] {self.name} held by {who.get('app')} ({who.get('purpose')}); retry in {every:.0f} s", flush=True)
            time.sleep(every)

    def release(self) -> None:
        if not self.mine:
            return
        for f in self.dir.glob("*"):
            f.unlink(missing_ok=True)
        self.dir.rmdir()
        self.mine = False

    def __enter__(self) -> Lock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _wait_guarded(proc: subprocess.Popen, guard) -> int:
    """Wait for ``proc``; every IDLE_RECHECK seconds ask ``guard()`` and stop the wait (return 4) when it says no."""
    while True:
        try:
            return proc.wait(timeout=IDLE_RECHECK if guard else None)
        except subprocess.TimeoutExpired:
            if guard and not guard():
                print(f"[locks] desktop no longer idle (< {IDLE_SECONDS} s since input); stopping the run", file=sys.stderr)
                return 4


def run_capped(argv: list[str], cap: int = CAP_BYTES, guard=None) -> int:
    """Run argv in a Job Object that kills everything over ``cap`` bytes and when the job closes. ``guard`` (desktop
    runs) is rechecked every minute; when it returns False the job is closed, which ends the whole process tree."""
    if sys.platform != "win32":
        proc = subprocess.Popen(argv)
        code = _wait_guarded(proc, guard)
        if code == 4 and proc.poll() is None:
            proc.kill()
        return code
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinWS", ctypes.c_size_t),
            ("MaxWS", ctypes.c_size_t),
            ("Active", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("Priority", wintypes.DWORD),
            ("Sched", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "ro", "wo", "oo")]

    class Ext(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("Io", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcess", ctypes.c_size_t),
            ("PeakJob", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Ext()
    info.Basic.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
    info.JobMemoryLimit = cap
    if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    proc = subprocess.Popen(argv)
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    try:
        return _wait_guarded(proc, guard)
    finally:
        k32.CloseHandle(wintypes.HANDLE(job))  # kills anything still in the job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["heavy", "desktop"])
    ap.add_argument("purpose")
    ap.add_argument("--wait", type=float, default=3600)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command after the purpose (options such as --wait go before the kind)")
    desktop = a.kind == "desktop"
    if desktop and idle_seconds() < IDLE_SECONDS:
        print(f"[locks] desktop idle {idle_seconds():.0f} s < {IDLE_SECONDS} s; not acquiring, retry later", file=sys.stderr)
        return 4
    lock = Lock(f"gilead-{a.kind}", a.purpose, session="earthwave-uat-phase3")
    lock.acquire(a.wait)
    try:
        if desktop and idle_seconds() < IDLE_SECONDS:  # the wait for the lock may have been long
            print("[locks] desktop became active while waiting; releasing", file=sys.stderr)
            return 4
        if free_ram_gb() < MIN_FREE_GB:
            print(f"[locks] free RAM {free_ram_gb():.1f} GB < {MIN_FREE_GB} GB; not running", file=sys.stderr)
            return 3
        return run_capped(cmd, guard=(lambda: idle_seconds() >= IDLE_SECONDS) if desktop else None)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
