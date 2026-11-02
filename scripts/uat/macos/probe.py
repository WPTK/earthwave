"""Raw-byte probe run in the terminal before the app: logs the terminal size and the exact bytes each injected key sends.

Usage: probe.py OUTDIR   (ends when it reads a 'Z' or after 180 s). Writes OUTDIR/probe.jsonl.
Application cursor mode is switched on, as a full-screen app would, so arrow keys show their application form.
"""

import json
import os
import select
import signal
import sys
import termios
import time
import tty

out = sys.argv[1]
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
log = open(os.path.join(out, "probe.jsonl"), "a")


def w(**k):
    k["t"] = time.time()
    log.write(json.dumps(k) + "\n")
    log.flush()


def size(*_):
    s = os.get_terminal_size()
    w(ev="size", cols=s.columns, rows=s.lines)


signal.signal(signal.SIGWINCH, size)
size()
sys.stdout.write("\x1b[?1h")
sys.stdout.flush()
w(ev="ready")
end = time.time() + 180
done = False
while time.time() < end and not done:
    r, _, _ = select.select([fd], [], [], 0.2)
    if r:
        d = os.read(fd, 64)
        w(ev="bytes", hex=d.hex())
        done = b"Z" in d
sys.stdout.write("\x1b[?1l")
sys.stdout.flush()
termios.tcsetattr(fd, termios.TCSADRAIN, old)
