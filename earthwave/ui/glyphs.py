"""Unicode or ASCII glyph sets; `auto` falls back to ASCII on Windows consoles that are not Windows Terminal."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Glyphs:
    unicode: bool
    dot: str  # status / PAGER dot
    tsunami: str
    bar: str  # separator in the status bar
    ellipsis: str


UNICODE = Glyphs(True, "●", "T", "│", "…")
ASCII = Glyphs(False, "*", "T", "|", "~")


def resolve_glyphs(mode: str, platform: str | None = None, environ: Mapping[str, str] | None = None) -> Glyphs:
    if mode == "unicode":
        return UNICODE
    if mode == "ascii":
        return ASCII
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    return ASCII if platform == "win32" and "WT_SESSION" not in environ else UNICODE
