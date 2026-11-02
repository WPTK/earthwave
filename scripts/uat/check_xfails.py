"""Every xfail in tests/ must name an issue that exists and is open; a closed one means the marker should have come off.

    python scripts/uat/check_xfails.py            # asks GitHub through `gh` (needs network and a token)
    python scripts/uat/check_xfails.py --list     # only lists them, offline

Finds ``known(N, ...)`` calls and ``#N:`` in ``xfail(... reason=...)`` strings, plus the ``KNOWN`` table of
tests/uat/test_matrix.py. ``UNFILED:`` markers are printed (a defect found by a suite and not yet reported) and do not
fail the check. Exit status 0 clean, 1 a stale or unknown issue, 2 gh unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = [
    re.compile(r"\bknown\(\s*(\d+)"),
    re.compile(r"xfail\([^)]*reason=f?[\"']#(\d+)"),
    re.compile(r"[\"']#(\d+): "),
]
UNFILED = re.compile(r"unfiled\(|UNFILED:")


def scan() -> tuple[dict[int, list[str]], list[str]]:
    issues: dict[int, list[str]] = {}
    unfiled: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        if path.name in {"known.py"} or "check_xfails" in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in PATTERNS:
                for m in pat.finditer(line):
                    issues.setdefault(int(m[1]), []).append(f"{rel}:{lineno}")
            if UNFILED.search(line):
                unfiled.append(f"{rel}:{lineno}")
    matrix = ROOT / "tests" / "uat" / "test_matrix.py"
    if matrix.is_file():  # KNOWN = {(state, screen, width): (issue, why)}
        for m in re.finditer(r"\):\s*\(\s*(\d{3}),", matrix.read_text(encoding="utf-8")):
            issues.setdefault(int(m[1]), []).append("tests/uat/test_matrix.py:KNOWN")
    return issues, unfiled


def state_of(number: int) -> str | None:
    got = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "state", "-q", ".state"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return got.stdout.strip() if got.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    issues, unfiled = scan()
    print(json.dumps({"issues": dict(sorted(issues.items())), "unfiled": unfiled}, indent=1))
    if args.list:
        return 0
    bad = 0
    for number in sorted(issues):
        state = state_of(number)
        if state is None:
            print(f"#{number}: cannot be read with gh (does it exist?)", file=sys.stderr)
            bad += 1
        elif state != "OPEN":
            print(f"#{number} is {state}: remove the xfail at {', '.join(issues[number])}", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
