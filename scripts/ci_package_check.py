"""Check the built wheel and sdist, install the wheel in a clean venv, upgrade 0.1.0 state, uninstall (CI package job).

    python -m build && python scripts/ci_package_check.py dist

Covers: package data in both archives (#234), a state directory written by 0.1.0 read by this build, and that
`pip uninstall` leaves the config and data directories alone (#239). Nothing here touches the real user directories:
EARTHWAVE_CONFIG_DIR and EARTHWAVE_DATA_DIR point at a scratch directory.
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPGRADE = ROOT / "tests" / "fixtures" / "upgrade" / "v0.1.0"
PACKAGE_DATA = [
    p.relative_to(ROOT).as_posix() for pat in ("ui/*.tcss", "data/*.json") for p in (ROOT / "earthwave").glob(pat)
]


def fail(message: str) -> None:
    print(f"package check FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if done.returncode:
        fail(f"{' '.join(cmd)} exited {done.returncode}\n{done.stdout}{done.stderr}")
    return done.stdout


def check_archives(dist: Path) -> Path:
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected one wheel and one sdist in {dist}, found {[p.name for p in wheels + sdists]}")
    if not PACKAGE_DATA:
        fail("no package data found in the source tree; the globs in pyproject.toml are stale")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    with tarfile.open(sdists[0]) as sdist:
        sdist_names = {n.split("/", 1)[1] for n in sdist.getnames() if "/" in n}
    for item in [*PACKAGE_DATA, "earthwave/__main__.py", "earthwave/paths.py"]:
        if item not in names:
            fail(f"{wheels[0].name} lacks {item}")
        if item not in sdist_names:
            fail(f"{sdists[0].name} lacks {item}")
    if "pyproject.toml" not in sdist_names:
        fail(f"{sdists[0].name} lacks pyproject.toml")
    print(f"archives ok: {wheels[0].name} and {sdists[0].name} carry {len(PACKAGE_DATA)} package data files")
    return wheels[0]


def main() -> None:
    wheel = check_archives(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
    with tempfile.TemporaryDirectory(prefix="ew-pkg-") as tmp:
        root = Path(tmp)
        cfg, data = root / "cfg", root / "data"
        cfg.mkdir()
        data.mkdir()
        shutil.copy(UPGRADE / "config.toml", cfg / "config.toml")
        shutil.copy(UPGRADE / "history.sqlite", data / "history.sqlite")
        before = {p.name: p.read_bytes() for p in (cfg / "config.toml", data / "history.sqlite")}

        venv.create(root / "venv", with_pip=True)
        bin_dir = root / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
        python = str(bin_dir / "python")
        env = {**os.environ, "EARTHWAVE_CONFIG_DIR": str(cfg), "EARTHWAVE_DATA_DIR": str(data)}
        run([python, "-m", "pip", "install", "--quiet", str(wheel)])
        run([python, "-m", "earthwave", "--version"], env)
        print(run([python, "-m", "earthwave", "--diagnose", "--offline"], env))

        # The upgrade: this build reads what 0.1.0 wrote, and the history still holds its two quakes.
        probe = (
            "from datetime import UTC, datetime;"
            "from earthwave.config import load_config;"
            "from earthwave.state.history import History, default_path;"
            "from earthwave.clock import SystemClock;"
            "assert load_config().view.min_magnitude == 4.0;"
            "h = History(default_path(), 36500, SystemClock()); h.open();"
            "ids = {q.id for q in h.load_since(datetime(2026, 1, 1, tzinfo=UTC))[0]};"
            "assert ids == {'us0001', 'us0002'}, ids; h.close_now(); print('upgrade from 0.1.0 ok')"
        )
        print(run([python, "-c", probe], env).strip())

        run([python, "-m", "pip", "uninstall", "--yes", "--quiet", "earthwave"])
        if (bin_dir / ("earthwave.exe" if sys.platform == "win32" else "earthwave")).exists():
            fail("the earthwave script is still there after pip uninstall")
        for path in (cfg / "config.toml", data / "history.sqlite"):
            if not path.is_file():
                fail(f"pip uninstall removed {path.name}; the docs say user files are kept")
        # opening the history in WAL mode may rewrite the file, so compare the config only byte for byte
        if (cfg / "config.toml").read_bytes() != before["config.toml"]:
            fail("config.toml changed during upgrade and uninstall")
        print("uninstall ok: the program is gone, config.toml and history.sqlite are kept")


if __name__ == "__main__":
    main()
