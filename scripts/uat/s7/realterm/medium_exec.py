r"""Run a command as a Medium-integrity process (driver included) and relay its output.

    python medium_exec.py LOGFILE -- python run_matrix.py wt ...

The child is ``cmd /c "<command> > LOGFILE 2>&1"`` started with the restricted Medium token from ``medium.py`` on
``winsta0\default``. This script tails the log to its own stdout and exits with the child's exit code, so a workflow step
behaves as if it had run the command directly, only non-elevated. The child's own integrity is recorded by the driver
(``integrity`` lines in its result JSON); a child that is not Medium is a failure there.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import medium


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print(__doc__)
        return 2
    log = Path(sys.argv[1]).resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    proc = medium.spawn_medium(sys.argv[3:], hidden_console=True, stdout_path=str(log))
    pos = 0
    while True:
        done = proc.poll() is not None
        data = log.read_bytes()
        if len(data) > pos:
            sys.stdout.write(data[pos:].decode("utf-8", errors="replace"))
            sys.stdout.flush()
            pos = len(data)
        if done:
            return int(proc.returncode or 0)
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
