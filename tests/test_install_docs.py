"""The README's install instructions must agree with the package metadata and the release workflow."""

import re
import tomllib
from pathlib import Path

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


def test_install_section_leads_with_pypi():
    section = _install_section()
    first = "pipx install earthwave\n"
    assert first in section
    assert section.index(first) < section.index("git+https")
    assert "pipx upgrade earthwave" in section
    assert "pipx uninstall earthwave" in section


def test_source_install_names_no_release_and_uses_the_tag_placeholder():
    section = _install_section()
    assert not re.findall(r"\bv\d+\.\d+\.\d+\b", section), "the install section names no fixed release tag"
    assert "`TAG`" in section
    urls = re.findall(r"earthwave\.git\S*", section)
    assert urls and all(u == "earthwave.git@TAG" for u in urls), "every source install URL carries the @TAG placeholder"


def test_readme_asset_names_match_the_release_workflow():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'base="earthwave-${GITHUB_REF_NAME}-${{ matrix.name }}"' in workflow
    for suffix in ("windows-x86_64.exe", "linux-x86_64.tar.gz", "macos-arm64.tar.gz"):
        assert f"earthwave-TAG-{suffix}" in _install_section()


def test_readme_mentions_every_cli_flag():
    main = (ROOT / "earthwave" / "__main__.py").read_text(encoding="utf-8")
    flags = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', main))
    assert len(flags) >= 14
    missing = sorted(f for f in flags if not re.search(rf"{re.escape(f)}(?![a-z-])", README))
    assert not missing, f"README omits {missing}"
