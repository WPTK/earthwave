"""Gilead shared locks for UAT runs, and a runner that holds one around a command under a 4 GB Job Object.

    python scripts/uat/locks.py heavy "S6 kit colour test" -- python -m pytest tests/uat_kit -p no:xdist

Acquire is an atomic mkdir of ``<locks>/gilead-heavy`` with an ``owner.json`` inside. If it is held, wait and retry.
A lock older than STALE_HOURS is reported and never stolen. Free RAM must be >= 6 GB first. Release is in ``finally``.
Windows only for the Job Object; elsewhere the command just runs under the lock.
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


def run_capped(argv: list[str], cap: int = CAP_BYTES) -> int:
    """Run argv in a Job Object that kills everything over ``cap`` bytes and when the job closes."""
    if sys.platform != "win32":
        return subprocess.call(argv)
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
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))  # noqa: SLF001
    try:
        return proc.wait()
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
    lock = Lock(f"gilead-{a.kind}", a.purpose)
    lock.acquire(a.wait)
    try:
        if free_ram_gb() < MIN_FREE_GB:
            print(f"[locks] free RAM {free_ram_gb():.1f} GB < {MIN_FREE_GB} GB; not running", file=sys.stderr)
            return 3
        return run_capped(cmd)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
