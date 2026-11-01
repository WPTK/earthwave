"""Contradiction and sanity rules that need only the text on screen, so they hold for a Pilot run, a real-input run and
the random walk alike. The state-aware rules (status count against the widget's rows, feed health against the
scheduler) stay in test_matrix.py; these are the ones a user can see without any knowledge of the app's internals.

``violations(text, cols, rows)`` returns one short sentence per broken rule; an empty list means none broke.
"""

from __future__ import annotations

import re

SCREENS = ("too_small", "help", "filters", "detail", "tsunami", "history", "observations", "dashboard", "unknown")

_LEAK = re.compile(r"\[/?(?:b|i|u|dim|bold|italic|reverse|red|green|yellow|orange|cyan|white|grey\d*|\$[\w-]+)\]|\[@click|link=")
_PLACEHOLDER = re.compile(r"\b(?:None|nan|NaN|inf)\b|\{[a-z_]+[^}]*\}|<(?:object|bound method) ")
_ROW = re.compile(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d")


def classify(text: str) -> str:
    """Which screen the text is: by the strings only that screen draws."""
    if "Terminal too small" in text:
        return "too_small"
    if "Esc or ? to close" in text:
        return "help"
    if "Minimum magnitude" in text:
        return "filters"
    if "Event page" in text and "Magnitude" in text:
        return "detail"
    if "LEVEL" in text and re.search(r"\bBack\b", text) and "shown of" not in text:
        return "tsunami"
    lines = text.splitlines()
    foot = lines[-1] if lines else ""
    if "shown of" in text:
        return "dashboard"
    if "AGE  MAG" in text and "Previous day" in foot:
        return "history"
    if re.search(r"\bBack\b", foot):
        return "observations"
    return "unknown"


def status_line(text: str) -> str:
    return next((ln for ln in text.splitlines() if " shown of " in ln), "")


def list_row_count(text: str) -> int:
    return sum(1 for ln in text.splitlines() if _ROW.match(ln[:72]))


def violations(text: str, cols: int, rows: int) -> list[str]:
    out: list[str] = []
    screen = classify(text)
    low = text.lower()
    if "Traceback (most recent call last)" in text or re.search(r"^\w+Error: ", text, re.M):
        out.append("a traceback or exception line is on screen")
    if "�" in text:
        out.append("a replacement glyph (U+FFFD) is on screen")
    if _LEAK.search(text):
        out.append(f"markup leaked into the text: {_LEAK.search(text).group(0)!r}")
    if (m := _PLACEHOLDER.search(text)) and screen != "help":
        out.append(f"placeholder text on screen: {m.group(0)!r}")
    if screen == "too_small":
        m = re.search(r"Terminal too small: (\d+)x(\d+)", text)
        if m and (int(m[1]), int(m[2])) != (cols, rows):
            out.append(f"the too-small message says {m[1]}x{m[2]} but the terminal is {cols}x{rows}")
    if screen == "dashboard":
        status = status_line(text)
        count = re.search(r"(\d+) shown of (\d+)", status)
        if count:
            shown, total = int(count[1]), int(count[2])
            drawn = list_row_count(text)
            if shown > total:
                out.append(f"status says {shown} shown of {total}")
            if drawn > shown:
                out.append(f"status says {shown} shown but {drawn} rows are drawn")
            if shown <= 4 and drawn != shown and "Terminal too small" not in text:
                out.append(f"status says {shown} shown but {drawn} rows are drawn")
        s = status.lower()
        if "feeds ok" in s and re.search(r"\b(failed|stale|loading|incomplete)\b", s):
            out.append(f"'feeds ok' beside a problem: {status.strip()!r}")
        if "offline" in s and ("feeds ok" in s or re.search(r"next \d", s)):
            out.append(f"offline beside live-feed text: {status.strip()!r}")
        if "No tsunami alerts in force" in text and "TSUNAMI" in status:
            out.append("'No tsunami alerts in force' with TSUNAMI in the status bar")
    if "waiting for usgs" in low and "feeds ok" in low:
        out.append("'waiting for USGS' beside 'feeds ok'")
    return out
