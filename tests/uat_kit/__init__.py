"""uat_kit: a self-contained, vendorable real-terminal test kit (pywinpty/ConPTY or stdlib pty, plus pyte)."""

from .terminal import Screen, Terminal, unavailable_reason

__all__ = ["Screen", "Terminal", "unavailable_reason"]
