"""The README's install instructions must agree with the package metadata and the release workflow."""

import re
import tomllib
from pathlib import Path

import earthwave

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _install_section() -> str:
    start = README.index("## Install")
    return README[start : README.index("\n## ", start + 1)]


def test_readme_names_the_python_floor_that_pyproject_requires():
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    floor = re.fullmatch(r">=(\d+\.\d+)", requires)
    assert floor, f"expected a plain '>=X.Y' requires-python, got {requires!r}"
    assert f"Python {floor.group(1)} or newer" in _install_section()


def test_ci_tests_the_python_floor():
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    floor = requires.removeprefix(">=")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'python: "{floor}"' in ci


def test_install_commands_pin_the_current_release():
    section = _install_section()
    tags = set(re.findall(r"\bv0\.\d+\.\d+\b", section))
    assert tags == {f"v{earthwave.__version__}"}, "bump the README install tags with __version__ (docs/development.md)"
    assert "releases/latest" not in section
    assert not re.search(r"earthwave\.git(?!@)", section), "an unpinned git URL builds main, not the release"


def test_readme_asset_names_match_the_release_workflow():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'base="earthwave-${GITHUB_REF_NAME}-${{ matrix.name }}"' in workflow
    v = f"v{earthwave.__version__}"
    for suffix in ("windows-x86_64.exe", "linux-x86_64.tar.gz", "macos-arm64.tar.gz"):
        assert f"earthwave-{v}-{suffix}" in _install_section()


def test_readme_mentions_every_cli_flag():
    main = (ROOT / "earthwave" / "__main__.py").read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', main))
    assert len(flags) >= 14
    missing = sorted(f for f in flags if not re.search(rf"{re.escape(f)}(?![a-z-])", README))
    assert not missing, f"README omits {missing}"
