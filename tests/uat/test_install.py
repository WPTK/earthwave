"""J2/J3: the install lines in the README, run literally in a clean virtual environment.

``UAT_INSTALL=1`` runs them (network: pip fetches the repository from GitHub; the test records the commit pip resolved,
because "installs from main" is only meaningful with the commit named). ``UAT_PY312=<path to a python3.12>`` adds the
Python 3.12 case. Without the variables the tests skip; the README-text tests always run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from uat.known import known

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
INSTALL = os.environ.get("UAT_INSTALL") == "1"
PY312 = os.environ.get("UAT_PY312") or None


def install_section() -> str:
    start = README.index("## Install")
    return README[start : README.index("\n## ", start + 5)]


def literal_pip_line() -> list[str]:
    """The pip command exactly as the README prints it (the `pip install git+...` inline code)."""
    m = re.search(r"`(pip install git\+https://[^`\s]+)`", install_section())
    assert m, "the README has no literal pip install line"
    return m[1].split()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw)


def clean_venv(tmp: Path, python: str | None = None) -> Path:
    env = tmp / "venv"
    if python:
        assert run([python, "-m", "venv", str(env)]).returncode == 0
    else:
        venv.create(env, with_pip=True)
    return env / ("Scripts" if sys.platform == "win32" else "bin")


@pytest.mark.skipif(not INSTALL, reason="network install: UAT_INSTALL=1")
def test_j2_readme_pip_line_installs_a_working_app(tmp_path: Path) -> None:
    bin_dir = clean_venv(tmp_path)
    line = literal_pip_line()
    got = run([str(bin_dir / "python"), "-m", *line[:1], *line[1:]])
    assert got.returncode == 0, got.stdout[-1500:] + got.stderr[-1500:]
    sha = re.search(r"commit ([0-9a-f]{40})", got.stdout + got.stderr)
    print(f"\ninstalled {' '.join(line)} resolved to commit {sha[1] if sha else 'unknown (pip did not say)'}")
    exe = bin_dir / ("earthwave.exe" if sys.platform == "win32" else "earthwave")
    for flag in ("--version", "--selftest"):
        r = run([str(exe), flag])
        assert r.returncode == 0, f"{flag}: {r.stdout}{r.stderr}"
    assert "selftest: ok" in run([str(exe), "--selftest"]).stdout


@pytest.mark.skipif(PY312 is None or not INSTALL, reason="needs UAT_INSTALL=1 and UAT_PY312=<python3.12>")
@known(224, "the README's install line fails on Python 3.12: pip refuses (requires >=3.13)")
def test_j3_readme_pip_line_on_python_312(tmp_path: Path) -> None:
    bin_dir = clean_venv(tmp_path, PY312)
    line = literal_pip_line()
    got = run([str(bin_dir / "python"), "-m", *line])
    assert got.returncode == 0, got.stderr[-800:]


@known(
    225, "the README has no one-line install per OS that works for a new user (release asset names carry the version)"
)
def test_j2b_readme_has_a_stable_install_line_per_os() -> None:
    section = install_section()
    for needle in ("releases/latest/download", "Windows", "Linux", "macOS"):
        assert needle in section, f"the Install section never says {needle!r}"
