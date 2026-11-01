"""Terminal-side mouse probe for the VS Code calibration (standard library only, Windows).

Run it inside a terminal: it turns on SGR mouse reporting and appends one line per left press to LOGFILE as
``col row`` (1-based cells, as the terminal reports them). The driver clicks at known pixels and reads the log, which
gives the pixel -> cell mapping of a terminal that exposes no geometry through UI Automation. ``q`` quits.

    python probe_mouse.py LOGFILE
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes


def main() -> int:
    log = sys.argv[1]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = wintypes.HANDLE
    h_in, h_out = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
    old_in, old_out = wintypes.DWORD(), wintypes.DWORD()
    k32.GetConsoleMode(h_in, ctypes.byref(old_in))
    k32.GetConsoleMode(h_out, ctypes.byref(old_out))
    # input: VT sequences, no line editing, no echo, no Ctrl-C processing; output: VT processing on
    k32.SetConsoleMode(h_in, 0x0200 | 0x0080)
    k32.SetConsoleMode(h_out, old_out.value | 0x0004)
    sys.stdout.write("\x1b[?1000h\x1b[?1006h\r\nprobe ready: click anywhere, q quits\r\n")
    sys.stdout.flush()
    pat = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
    buf = b""
    try:
        while True:
            data = os.read(0, 256)
            if not data:
                break
            buf += data
            for m in pat.finditer(buf):
                if m.group(4) == b"M" and int(m.group(1)) == 0:  # left press
                    with open(log, "a", encoding="ascii") as fh:
                        fh.write(f"{int(m.group(2))} {int(m.group(3))}\n")
            buf = pat.sub(b"", buf)
            if b"q" in buf or b"\x03" in buf:
                break
            buf = buf[-64:]
    finally:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\r\n")
        sys.stdout.flush()
        k32.SetConsoleMode(h_in, old_in.value)
        k32.SetConsoleMode(h_out, old_out.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
