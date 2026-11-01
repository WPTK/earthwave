"""Which size does a child see under tui-test on this OS? Prints the screen of `stty size` before and after a resize."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from uat_kit import Terminal  # noqa: E402

PROG = "stty size; sleep 3; stty size; sleep 3; stty size; sleep 5"
with Terminal(["sh", "-c", PROG], 130, 32) as t:  # the kit already resizes once after start on posix
    time.sleep(1)
    print("after start:", [ln for ln in t.screen().text.splitlines() if ln.strip()])
    t.resize(120, 30)
    time.sleep(4)
    print("after resize(120, 30):", [ln for ln in t.screen().text.splitlines() if ln.strip()])
    time.sleep(4)
    print("later:", [ln for ln in t.screen().text.splitlines() if ln.strip()])
