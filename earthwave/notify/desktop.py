"""Desktop notifications behind a small protocol: Windows toast, notify-send, osascript, and a test fake."""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from earthwave.state.rules import Notification

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, note: Notification) -> None:
        """Show one notification. Implementations swallow and log their own failures."""
        ...


class RecordingNotifier:
    """Collects notifications instead of showing them (tests, and a stand-in under replay)."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, note: Notification) -> None:
        self.sent.append(note)


class _ThreadedNotifier:
    """Runs a blocking call off the event loop; any failure is logged, never raised."""

    async def send(self, note: Notification) -> None:
        try:
            await asyncio.to_thread(self._show, note)
        except Exception as exc:
            log.warning("desktop notification failed: %r", exc)

    def _show(self, note: Notification) -> None:
        raise NotImplementedError


# Fixed script: the feed text never reaches it as code. Title and body travel in environment variables and are
# assigned to XML text nodes (InnerText escapes them), so `$(...)`, backticks, quotes and `"@` stay plain text.
TOAST_SCRIPT = """[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast duration="long"><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$text = $xml.GetElementsByTagName('text')
$text.Item(0).InnerText = $env:EARTHWAVE_TOAST_TITLE
$text.Item(1).InnerText = $env:EARTHWAVE_TOAST_BODY
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('earthwave').Show($toast)
"""  # noqa: E501
TOAST_TIMEOUT = 10  # seconds before a stuck PowerShell is killed


def _plain(text: str) -> str:
    """Spaces of any kind become one plain space; other control characters go (a NUL cannot be in an environment)."""
    return "".join(" " if ch.isspace() and ch != "\n" else ch for ch in text if ch.isprintable() or ch.isspace())


def powershell_path() -> str:
    """The system's own PowerShell, not whatever `powershell.exe` the search path finds first."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


class WindowsToast(_ThreadedNotifier):
    """One PowerShell per toast, running a fixed script; slow, and it may be blocked by policy."""

    def _show(self, note: Notification) -> None:
        env = {**os.environ, "EARTHWAVE_TOAST_TITLE": _plain(note.title), "EARTHWAVE_TOAST_BODY": _plain(note.body)}
        result = subprocess.run(
            [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT],
            check=False,
            timeout=TOAST_TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console of its own; ours stays visible
        )
        if result.returncode:
            log.warning("toast PowerShell exited %s: %s", result.returncode, (result.stderr or "").strip()[:300])


def _run_quietly(cmd: list[str]) -> None:
    """Run a notifier with its output captured: a child writing to our terminal would corrupt the TUI screen."""
    result = subprocess.run(
        cmd,
        check=False,
        timeout=10,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        log.warning("%s exited %s: %s", cmd[0], result.returncode, (result.stderr or "").strip()[:300])


class NotifySend(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        level = "critical" if note.urgency == "critical" else "normal"
        cmd = ["notify-send", "-u", level, "-a", "earthwave", "--", note.title, note.body]
        _run_quietly(cmd)


class Osascript(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        def quote(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = f"display notification {quote(note.body)} with title {quote(note.title)}"
        _run_quietly(["osascript", "-e", script])


def default_notifier(platform: str | None = None) -> Notifier | None:
    """The platform's notifier, or None when the platform has no usable one."""
    platform = platform or sys.platform
    if platform == "win32":
        return WindowsToast()
    if platform == "darwin":
        return Osascript()
    if shutil.which("notify-send"):
        return NotifySend()
    log.warning("no desktop notifier: notify-send is not installed, so there will be no toasts")
    return None
