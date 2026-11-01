"""Add or remove the S8b feed names in the hosts file. Disposable environments only (CI runner, Windows Sandbox):
this refuses to run unless S8B_DISPOSABLE=1 is set, so it cannot be pointed at a real machine by accident.

    python hosts.py add <listening.json>      python hosts.py remove
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

MARK = "# earthwave-s8b"
PATH = Path(r"C:\Windows\System32\drivers\etc\hosts") if platform.system() == "Windows" else Path("/etc/hosts")


def main() -> None:
    if os.environ.get("S8B_DISPOSABLE") != "1":
        sys.exit("refusing: S8B_DISPOSABLE=1 is not set (hosts-file changes are for disposable environments only)")
    keep = [ln for ln in PATH.read_text().splitlines() if MARK not in ln]
    if sys.argv[1] == "add":
        info = json.loads(Path(sys.argv[2]).read_text())
        for host in info["hosts"]:
            keep.append(f"127.0.0.1 {host} {MARK}")
            if info["ipv6"]:
                keep.append(f"::1 {host} {MARK}")
    PATH.write_text("\n".join(keep) + "\n")
    if platform.system() == "Windows":
        subprocess.run(["ipconfig", "/flushdns"], check=False, capture_output=True)
    print(PATH.read_text())


main()
