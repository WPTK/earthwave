"""A minimal ConPTY host with its own reader, for S7: it runs a program in a pseudo console, records every byte the
program writes, and can play the terminal's side of the probe (answer DA1 and the cell-size queries).

passthrough=True creates the pseudo console with PSEUDOCONSOLE_PASSTHROUGH_MODE (0x8, Windows 11 22H2+), so the
query bytes reach this host instead of being answered by conhost. Pure ctypes, no dependencies.
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
HPCON = ctypes.c_void_p
PSEUDOCONSOLE_PASSTHROUGH_MODE = 0x8
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
CREATE_UNICODE_ENVIRONMENT = 0x400


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


for fn, res, args in [
    ("CreatePipe", wintypes.BOOL, [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]),
    ("CreatePseudoConsole", ctypes.c_long, [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(HPCON)]),
    ("ClosePseudoConsole", None, [HPCON]),
    ("InitializeProcThreadAttributeList", wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]),
    ("UpdateProcThreadAttribute", wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]),
    ("CreateProcessW", wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)]),
    ("ReadFile", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]),
    ("WriteFile", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]),
    ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    ("WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
    ("TerminateProcess", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
    ("GetExitCodeProcess", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
]:
    f = getattr(k32, fn)
    f.restype, f.argtypes = res, args

ESC = chr(27)
Q_CELL = ESC + "[16t"
Q_AREA = ESC + "[14t"
Q_DA1 = ESC + "[c"
_QUERIES = re.compile(r"\x1b\[(16t|14t|c|0c)")


@dataclass
class FakeTerminal:
    """What the fake terminal answers: DA1 with or without sixel (attribute 4) and the cell size."""

    sixel: bool = True
    cell: tuple[int, int] | None = (10, 20)  # width, height in pixels
    answer_da1: bool = True
    answer_cell: bool = True
    cols: int = 130
    rows: int = 32

    def reply_to(self, query: str) -> str:
        if query == "16t" and self.answer_cell and self.cell:
            return f"{ESC}[6;{self.cell[1]};{self.cell[0]}t"
        if query == "14t" and self.answer_cell and self.cell:
            return f"{ESC}[4;{self.cell[1] * self.rows};{self.cell[0] * self.cols}t"
        if query in ("c", "0c") and self.answer_da1:
            return f"{ESC}[?62;4;22c" if self.sixel else f"{ESC}[?62;22c"
        return ""


@dataclass
class Result:
    raw: bytes = b""
    queries: list[tuple[float, str]] = field(default_factory=list)  # (seconds since start, query seen at the host)
    replies: list[tuple[float, str]] = field(default_factory=list)
    exit_code: int | None = None

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")


class ConPty:
    def __init__(self, argv: list[str], cols: int = 130, rows: int = 32, passthrough: bool = False,
                 terminal: FakeTerminal | None = None, env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.result = Result()
        self.terminal = terminal
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        in_r, in_w, out_r, out_w = (wintypes.HANDLE() for _ in range(4))
        k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0)
        k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0)
        self._in_w, self._out_r = in_w, out_r
        self.hpc = HPCON()
        flags = PSEUDOCONSOLE_PASSTHROUGH_MODE if passthrough else 0
        hr = k32.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, flags, ctypes.byref(self.hpc))
        if hr != 0:
            raise OSError(f"CreatePseudoConsole failed: 0x{hr & 0xFFFFFFFF:08x} (passthrough={passthrough})")
        k32.CloseHandle(in_r)
        k32.CloseHandle(out_w)
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attrs = ctypes.create_string_buffer(size.value)
        k32.InitializeProcThreadAttributeList(self._attrs, 1, 0, ctypes.byref(size))
        k32.UpdateProcThreadAttribute(self._attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self.hpc, ctypes.sizeof(HPCON), None, None)
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.addressof(self._attrs)
        pi = PROCESS_INFORMATION()
        import os
        import subprocess

        environ = dict(os.environ)
        environ.update(env or {})
        block = "".join(f"{k}={v}\0" for k, v in environ.items()) + "\0"
        envbuf = ctypes.create_unicode_buffer(block, len(block))
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        if not k32.CreateProcessW(None, cmd, None, None, False, EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                  envbuf, cwd, ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(f"CreateProcessW failed: {ctypes.get_last_error()}")
        self.pid, self._proc = pi.dwProcessId, pi.hProcess
        k32.CloseHandle(pi.hThread)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def write(self, data: str) -> None:
        raw = data.encode("utf-8")
        n = wintypes.DWORD()
        k32.WriteFile(self._in_w, raw, len(raw), ctypes.byref(n), None)

    def _read(self) -> None:
        buf = ctypes.create_string_buffer(65536)
        n = wintypes.DWORD()
        seen = 0
        while k32.ReadFile(self._out_r, buf, 65536, ctypes.byref(n), None) and n.value:
            chunk = buf.raw[: n.value]
            with self._lock:
                self.result.raw += chunk
                text = self.result.raw.decode("latin-1")
            now = time.monotonic() - self._t0
            for m in _QUERIES.finditer(text, max(0, seen - 8)):
                if m.end() <= seen:
                    continue
                self.result.queries.append((now, m.group(1)))
                if self.terminal and (reply := self.terminal.reply_to(m.group(1))):
                    self.result.replies.append((now, reply))
                    self.write(reply)
            seen = len(text)

    def alive(self) -> bool:
        return k32.WaitForSingleObject(self._proc, 0) == 258  # WAIT_TIMEOUT

    def wait(self, timeout: float) -> bool:
        return k32.WaitForSingleObject(self._proc, int(timeout * 1000)) == 0

    def close(self) -> Result:
        """End the program by PID, close the pseudo console and the pipes."""
        if self.alive():
            k32.TerminateProcess(self._proc, 1)
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(self._proc, ctypes.byref(code)):
            self.result.exit_code = code.value
        k32.ClosePseudoConsole(self.hpc)
        self._reader.join(3)
        for h in (self._in_w, self._out_r, self._proc):
            k32.CloseHandle(h)
        return self.result
