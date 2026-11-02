"""Click through first-launch modal dialogs (local-network prompt, update prompt) by Accessibility, CI VM only.

Walks every process that owns an on-screen window and clicks buttons named Allow / OK / Don't Check etc.
Prints what it clicked.
"""

import subprocess
import sys

import Quartz

WANT = ["Allow", "Check Automatically", "OK", "Open", "Continue", "Not Now", "Don’t Ask Again"]
SKIP = {"Window Server", "Dock", "Control Center", "SystemUIServer", "Terminal", "Finder"}
NL = chr(10)


def owners():
    out = set()
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
        o = w.get("kCGWindowOwnerName")
        if o and o not in SKIP:
            out.add(o)
    return sorted(out)


def try_click(owner):
    names = ", ".join('"' + n + '"' for n in WANT)
    script = NL.join(
        [
            'tell application "System Events"',
            'tell process "' + owner + '"',
            "set hit to \"\"",
            "repeat with w in windows",
            "try",
            "repeat with e in (entire contents of w)",
            "try",
            "if (class of e is button) and ((name of e) is in {" + names + "}) then",
            "set hit to name of e",
            "click e",
            "exit repeat",
            "end if",
            "end try",
            "end repeat",
            "end try",
            "if hit is not \"\" then exit repeat",
            "end repeat",
            "return hit",
            "end tell",
            "end tell",
        ]
    )
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=25)
        return p.stdout.strip(), p.stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        return "", "timeout"


clicked = 0
for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 2):
    for o in owners():
        hit, err = try_click(o)
        print("owner", o, "->", hit or "-", err)
        if hit:
            clicked += 1
print("dismissed", clicked)
