"""S7 step 1: what does the app's own probe decide in a real ConPTY, with and without a fake terminal answering?

Runs earthwave.ui.graphics.probe.resolve("auto") (the code path the app takes at startup) inside a pseudo console and
prints one line per configuration: the choice, the queries the host saw, and when the replies went out.
Usage: python probe_matrix.py   (run from the repo root with the project's python)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conpty_host import ConPty, FakeTerminal  # noqa: E402

PROG = (
    "import sys; from earthwave.ui.graphics.probe import resolve; c = resolve('auto'); "
    "print('CHOICE', c.renderer, c.cell, c.reason, flush=True)"
)
CASES = {
    "default-conpty, no reply": dict(passthrough=False, terminal=None),
    "default-conpty, fake terminal answers sixel": dict(passthrough=False, terminal=FakeTerminal()),
    "passthrough, no reply": dict(passthrough=True, terminal=None),
    "passthrough, fake terminal answers sixel": dict(passthrough=True, terminal=FakeTerminal()),
    "passthrough, fake terminal answers no-sixel DA1": dict(passthrough=True, terminal=FakeTerminal(sixel=False)),
}


def main() -> None:
    try:  # sanity: does the pseudo console carry a child's output to the host at all?
        pty = ConPty(["cmd.exe", "/c", "echo hello-from-pty"])
        pty.wait(10)
        print("sanity echo through the pty:", "hello-from-pty" in pty.close().text)
    except OSError as exc:
        print("sanity:", exc)
    for name, kw in CASES.items():
        try:
            pty = ConPty([sys.executable, "-c", PROG], **kw)
        except OSError as exc:
            print(f"{name}: {exc}")
            continue
        pty.wait(15)
        res = pty.close()
        choice = [ln for ln in res.text.splitlines() if "CHOICE" in ln]
        print(f"{name}\n  choice : {choice[-1].strip() if choice else 'none'}")
        print(f"  queries seen by host: {[(round(t, 3), q) for t, q in res.queries]}")
        print(f"  replies sent        : {[(round(t, 3), r[1:]) for t, r in res.replies]}")


if __name__ == "__main__":
    main()
