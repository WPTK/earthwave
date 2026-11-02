"""L4: every key, flag and config path the README promises, compared with --help, the footer and the help screen, and
the keys driven. Report-only: mismatches are listed in ``$UAT_OUT/l4-readme.txt`` and printed; the extraction is in
``scripts/uat/readme_cmds.py``. (`r` re-polling USGS is measured at the origin in test_lab.py.)"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "uat"))
import readme_cmds as rc

from uat.known import report
from uat.target import BASE, REPO, USGS, evidence, launch, needs_kit, needs_state, program, ready

TEXT = (REPO / "README.md").read_text(encoding="utf-8")
REPORT: list[str] = []


def write_report() -> None:
    out = Path(os.environ.get("UAT_OUT", "."))
    out.mkdir(parents=True, exist_ok=True)
    (out / "l4-readme.txt").write_text("\n".join(REPORT), encoding="utf-8")
    print("\n".join(REPORT))


def test_l4_readme_flags_match_help() -> None:
    helped = subprocess.run([*program(), "--help"], capture_output=True, text=True, timeout=60, check=False).stdout
    problems = rc.compare_flags(TEXT, helped)
    REPORT.append(f"flags: README {sorted(rc.readme_flags(TEXT))}")
    REPORT.append(f"flags: --help {sorted(rc.help_flags(helped))}")
    REPORT.extend(f"flag mismatch: {p}" for p in problems)
    write_report()
    # the README names the flags it uses; every flag in the README must exist
    assert not [p for p in problems if p.startswith("README mentions")], problems


@needs_kit
def test_l4_readme_keys_match_help_screen_and_footer() -> None:
    with launch("--replay", str(USGS), *BASE) as t:
        ready(t)
        footer = t.screen().lines[-1]
        t.send_keys("?")
        t.wait_for_text("Esc or ? to close", 10)
        help_text = evidence(t, "l4", "help")
        t.send_keys("escape")
        t.wait_for_text("shown of", 10)
        t.send_keys("q")
    readme = rc.readme_keys(TEXT)
    helped = rc.help_screen_keys(help_text)
    foot = rc.footer_keys(footer)
    norm = {"Enter": "Enter", "arrows": "arrows"}
    problems = [f"README key {k!r} is not on the help screen" for k in sorted(readme) if norm.get(k, k) not in helped]
    problems += [f"footer key {k!r} is not on the help screen" for k in sorted(foot) if k not in helped and k != "?"]
    REPORT.append(f"keys: README {sorted(readme)}; help screen {sorted(helped)}; footer {sorted(foot)}")
    REPORT.extend(f"key mismatch: {p}" for p in problems)
    write_report()
    assert readme and helped and foot, "extraction found nothing: " + str((readme, helped, foot))
    report("README, footer and help screen disagree about keys (l4-readme.txt)", problems)


@needs_kit
def test_l4_each_readme_key_changes_the_screen() -> None:
    silent = []
    keys = ["j", "k", "down", "up", "enter", "f", "s", "slash", "m", "z", "t", "o", "h", "a", "r"]
    for key in keys:
        with launch("--replay", str(USGS), *BASE) as t:
            ready(t)
            t.send_keys("j")  # not the first row, so k and up move too
            t.settle()
            before = [[(c.char, c.fg, c.bg, c.inverse) for c in row] for row in t.cells()]
            t.send_keys(key)
            t.settle(0.8)
            after = [[(c.char, c.fg, c.bg, c.inverse) for c in row] for row in t.cells()]
            if before == after:
                silent.append(key)
    REPORT.append(f"keys with no visible change from a settled dashboard: {silent}")
    write_report()
    report("README keys with no visible effect (l4-readme.txt)", silent)


@needs_kit
@needs_state
def test_l4_first_run_writes_the_config_where_the_readme_says(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    from earthwave.config import default_config_path

    path = default_config_path()
    path.unlink(missing_ok=True)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_DATA_HOME": str(tmp_path / "xdg-data")}
    with launch("--replay", str(USGS), *BASE, env=env, no_config=False) as t:
        ready(t)
        t.send_keys("q")
        t.wait_exit(10)
    REPORT.append(f"config path {path}: exists={path.is_file()}")
    write_report()
    report(
        "first run writes no commented config where the README says",
        [] if path.is_file() and path.read_text(encoding="utf-8").lstrip().startswith("#") else [str(path)],
    )
