"""Negative and boundary cases at the command line (UAT item L): bad input is refused with exit code 2 and one
plain line, never a traceback, and nothing is started. Runs the source in a subprocess (no terminal needed)."""

from __future__ import annotations

import subprocess
import sys

import pytest

BAD = [
    (["--home", "abc"], "--home expects LAT,LON"),
    (["--home", "99,200"], "out of range"),
    (["--min-mag", "-5"], "between -2 and 10"),
    (["--min-mag", "11"], "between -2 and 10"),
    (["--min-mag", "abc"], "invalid float value"),
    (["--hours", "0"], "between 1 and 840"),
    (["--hours", "841"], "between 1 and 840"),
    (["--glyphs", "nope"], "invalid choice"),
    (["--bogus"], "unrecognized arguments"),
    (["--offline", "--replay", "x.jsonl"], "cannot be combined"),
]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "earthwave", "--no-config", *args],
        capture_output=True, text=True, timeout=30, encoding="utf8", errors="replace", stdin=subprocess.DEVNULL,
    )  # fmt: skip


@pytest.mark.parametrize("args,message", BAD, ids=[" ".join(a) for a, _ in BAD])
def test_bad_argument_is_refused_with_one_line_and_no_traceback(args, message):
    r = run(*args)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr and message in r.stderr, r.stderr


def test_version_and_selftest_exit_zero_without_a_terminal():
    assert run("--version").returncode == 0
    assert "selftest: ok" in run("--selftest").stdout
