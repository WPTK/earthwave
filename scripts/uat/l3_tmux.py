"""Second driver for the slow-origin cold start (#272): tmux + the released binary, no tui-test kit, Linux runner only.

    python scripts/uat/l3_tmux.py --exe PATH --work S8B_DIR --out DIR [--delay 20] [--runs 3] [--budget 170]

Each run: origin fault delay:N for every host, a fresh tmux session with an empty profile, capture-pane every 2 s until
quakes are listed (or the budget ends), then the origin's own log for that run (when each request arrived, how long the
origin held it, and whether the client closed first). Writes <out>/l3-tmux.json and l3-tmux.txt. Never run this on a real
machine: it relies on the hosts-file redirect of a disposable runner.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

LISTED = re.compile(r"[1-9]\d* shown of")


def control(work: Path, path: str, body: dict | None = None):
    port = json.loads((work / "listening.json").read_text())["control"]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body else None,
        method="POST" if body else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def tmux(*a: str) -> str:
    return subprocess.run(["tmux", *a], capture_output=True, text=True, check=False).stdout


def one_run(n: int, exe: str, work: Path, delay: int, budget: float) -> dict:
    control(work, "/fault", {"host": "*", "mode": f"delay:{delay}"})
    offset = len(control(work, "/log"))
    home = Path(tempfile.mkdtemp(prefix="l3tmux"))
    sess = f"l3-{n}"
    ca = json.loads((work / "listening.json").read_text())["ca"]
    env = (
        f"HOME={home} XDG_CONFIG_HOME={home}/c XDG_DATA_HOME={home}/d XDG_STATE_HOME={home}/s "
        f"XDG_CACHE_HOME={home}/k SSL_CERT_FILE={ca} TERM=xterm-256color"
    )
    t0 = time.monotonic()
    tmux(
        "new-session",
        "-d",
        "-s",
        sess,
        "-x",
        "130",
        "-y",
        "32",
        f"env {env} {exe} --no-config --log {home}/earthwave.log",
    )
    first_dash = listed = not_answering = None
    shots: list[tuple[float, str]] = []
    text = ""
    while time.monotonic() - t0 < budget:
        text = tmux("capture-pane", "-p", "-t", sess)
        el = round(time.monotonic() - t0, 1)
        if first_dash is None and "shown of" in text:
            first_dash = el
        if not_answering is None and "not answering" in text:
            not_answering = el
        if not shots or el - shots[-1][0] >= 20:
            shots.append((el, text))
        if LISTED.search(text):
            listed = el
            break
        time.sleep(2)
    alive = subprocess.run(["tmux", "has-session", "-t", sess], capture_output=True, check=False).returncode == 0
    log = control(work, "/log")[offset:]
    tmux("send-keys", "-t", sess, "q")
    time.sleep(1)
    tmux("kill-session", "-t", sess)
    return {
        "run": n,
        "delay": delay,
        "first_dashboard_s": first_dash,
        "quakes_listed_s": listed,
        "not_answering_seen_s": not_answering,
        "alive_at_end": alive,
        "requests": [{k: e.get(k) for k in ("iso", "host", "target", "status", "held_s")} for e in log],
        "screens": [{"t": t, "text": s} for t, s in shots],
        "final_screen": text,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--delay", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--budget", type=float, default=170)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(a.work)
    results = [one_run(i + 1, a.exe, work, a.delay, a.budget) for i in range(a.runs)]
    control(work, "/fault", {"host": "*", "mode": "ok"})
    (out / "l3-tmux.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    lines = []
    for r in results:
        held = [q["held_s"] for q in r["requests"] if q["held_s"] is not None]
        closed = [q for q in r["requests"] if q["status"] == "client-closed"]
        lines.append(
            f"run {r['run']}: dashboard {r['first_dashboard_s']} s, quakes listed {r['quakes_listed_s']} s, "
            f"'not answering' {r['not_answering_seen_s']} s, requests {len(r['requests'])}, "
            f"held_s min/max {min(held, default=None)}/{max(held, default=None)}, client closed first {len(closed)}"
        )
    lines.append(f"listed in {sum(1 for r in results if r['quakes_listed_s'] is not None)} of {len(results)} runs")
    (out / "l3-tmux.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
