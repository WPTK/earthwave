"""Desktop notifications behind a small protocol: Windows toast, notify-send, osascript, and a test fake."""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
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
    """Drop control characters (a NUL cannot go in an environment variable; the rest cannot show in a toast)."""
    return "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")


class WindowsToast(_ThreadedNotifier):
    """One hidden PowerShell per toast, running a fixed script; slow, and it may be blocked by policy."""

    def _show(self, note: Notification) -> None:
        env = {**os.environ, "EARTHWAVE_TOAST_TITLE": _plain(note.title), "EARTHWAVE_TOAST_BODY": _plain(note.body)}
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", TOAST_SCRIPT],
            check=False,
            timeout=TOAST_TIMEOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class NotifySend(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        level = "critical" if note.urgency == "critical" else "normal"
        cmd = ["notify-send", "-u", level, "-a", "earthwave", "--", note.title, note.body]
        subprocess.run(cmd, check=False, timeout=10)


class Osascript(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        def quote(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = f"display notification {quote(note.body)} with title {quote(note.title)}"
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)


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
