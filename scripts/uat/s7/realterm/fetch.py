"""Download, verify and unpack the pinned tools for the real-terminal spike.

    python fetch.py wxbrief|wt|vscode --dest DIR

Prints one JSON object on stdout (paths, version, sha256). Exits non-zero on any hash mismatch. The wxbrief exe is also
checked against the release's own SHA256SUMS. Nothing here runs a downloaded file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

PINS = json.loads((Path(__file__).with_name("pins.json")).read_text(encoding="utf-8"))


def download(url: str, dest: Path) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "wxbrief-uat-realterm"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", choices=["wxbrief", "wt", "vscode"])
    ap.add_argument("--dest", required=True, type=Path)
    ap.add_argument(
        "--arch",
        choices=["x64", "arm64"],
        default="x64",
        help="wt and vscode only; wxbrief is x86_64 (emulated on arm64)",
    )
    args = ap.parse_args()
    pin = PINS[args.tool + ("_arm64" if args.arch == "arm64" and args.tool != "wxbrief" else "")]
    args.dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {"tool": args.tool, "version": pin["version"], "url": pin["url"]}
    if args.tool == "wxbrief":
        target = args.dest / pin["name"]
        got = download(pin["url"], target)
        sums = args.dest / "SHA256SUMS"
        download(pin["sums_url"], sums)
        listed = {
            parts[1].lstrip("*"): parts[0]
            for line in sums.read_text(encoding="utf-8").splitlines()
            if len(parts := line.split()) == 2
        }
        if listed.get(pin["name"]) != got or got != pin["sha256"]:
            print(
                f"hash mismatch for {pin['name']}: got {got}, SHA256SUMS {listed.get(pin['name'])}, pin {pin['sha256']}",
                file=sys.stderr,
            )
            return 1
        out.update(exe=str(target), sha256=got, sha256sums_verified="yes")
    else:
        archive = args.dest / f"{args.tool}.zip"
        got = download(pin["url"], archive)
        if got != pin["sha256"]:
            print(f"hash mismatch for {args.tool}: got {got}, pin {pin['sha256']}", file=sys.stderr)
            return 1
        root = args.dest / args.tool
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(root)
        exe_name = "WindowsTerminal.exe" if args.tool == "wt" else "Code.exe"
        exes = sorted(root.rglob(exe_name))
        if not exes:
            print(f"{exe_name} not found after unzip", file=sys.stderr)
            return 1
        out.update(exe=str(exes[0]), root=str(exes[0].parent), sha256=got, archive=str(archive))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
