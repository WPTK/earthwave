"""Extract every command, flag, key and config path the README promises, and compare them with what the program says.

Static half of L4 (the driving half is tests/uat/test_readme_cmds.py). Pure functions; nothing here starts the app.

    python scripts/uat/readme_cmds.py              # README against `python -m earthwave --help`
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
KEY_ROW = re.compile(r"^\|\s*(`[^|]+?)\s*\|\s*(.+?)\s*\|\s*$", re.M)


def fenced_commands(text: str) -> list[str]:
    """Every line of a fenced block that starts with the program name (comments after # are cut)."""
    out = []
    for block in FENCE.findall(text):
        for line in block.splitlines():
            line = line.split("#")[0].strip()
            if line.startswith("earthwave"):
                out.append(line)
    return out


def readme_flags(text: str) -> set[str]:
    return {f for line in fenced_commands(text) for f in FLAG.findall(line)} | set(FLAG.findall(prose(text)))


def prose(text: str) -> str:
    """README text outside fences, where flags are mentioned in backticks."""
    return FENCE.sub("", text)


def help_flags(help_text: str) -> set[str]:
    return set(FLAG.findall(help_text))


def readme_keys(text: str) -> set[str]:
    """Keys of the README keys table, as the single characters or names (j k, arrows, Enter, /, ...)."""
    keys: set[str] = set()
    for cell, _action in KEY_ROW.findall(text):
        if cell.lower().startswith("`key"):
            continue
        for tok in re.findall(r"`([^`]+)`", cell):
            keys.add(tok)
        if "arrows" in cell:
            keys.add("arrows")
    return keys


def help_screen_keys(screen: str) -> set[str]:
    """Keys on the help screen's Keys block: the token (or tokens joined by '/') before two or more spaces."""
    keys: set[str] = set()
    lines = screen.splitlines()
    start = next((i for i, ln in enumerate(lines) if "Keys" in ln), None)
    if start is None:
        return keys
    x0 = max(lines[start].index("Keys") - 2, 0)  # the help box is drawn over the dashboard: cut at its left edge
    block = []
    for ln in lines[start + 1 :]:
        if "Colors" in ln:
            break
        block.append(ln[x0 : x0 + 60].replace("│", " "))
    for line in block:
        cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        if len(cells) < 2 or line.startswith("Detail"):
            continue
        for cell in cells[0::2]:  # key, description, key, description
            for part in re.split(r"[/,]\s*", cell):
                part = part.strip()
                if part == "arrows":
                    keys.add("arrows")
                elif part and len(part) <= 5:
                    keys.add(part)
    return keys


def footer_keys(footer: str) -> set[str]:
    """Footer cells are 'k Label' pairs."""
    return set(re.findall(r"(?:^|\s)(\S) [A-Z][a-z]+", footer))


def compare_flags(text: str, help_text: str) -> list[str]:
    readme, helped = readme_flags(text), help_flags(help_text)
    skip = {"--help", "--version"}
    out = [f"README mentions {f}, which --help does not list" for f in sorted(readme - helped)]
    out += [f"--help lists {f}, which the README never mentions" for f in sorted(helped - readme - skip)]
    return out


def main() -> int:
    text = README.read_text(encoding="utf-8")
    helped = subprocess.run(
        [sys.executable, "-m", "earthwave", "--help"], capture_output=True, text=True, timeout=60, check=False
    ).stdout
    problems = compare_flags(text, helped)
    print(f"{len(fenced_commands(text))} README commands, {len(readme_keys(text))} keys, {len(problems)} mismatches")
    for p in problems:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
