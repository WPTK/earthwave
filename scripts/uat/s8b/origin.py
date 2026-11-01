"""S8b: a local TLS origin for every feed host of the exe, served from the replay recordings, with per-host faults.

A hosts-file entry (made only in a disposable environment: a CI runner or Windows Sandbox) points the feed names at
127.0.0.1; this origin listens on port 443 with a leaf certificate for all of them, signed by a throw-away CA that the
exe trusts through SSL_CERT_FILE. Reuses the CA helper from the wxbrief faultproxy (ca.py); the origin is new because
earthwave's payloads are not AWC lists (USGS is a GeoJSON object) and its feeds sit on six hosts.

    python origin.py --dir <work dir> [--port 443] [--control 9443]

Control (plain HTTP on 127.0.0.1): POST /fault {"host": "earthquake.usgs.gov", "mode": "503" | "reset" | "ok"},
GET /log. Writes <dir>/ca.pem and <dir>/listening.json once it is up.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import ssl
import struct
import time
from datetime import UTC, datetime
from pathlib import Path

from ca import TestCA

HOSTS = [
    "earthquake.usgs.gov",
    "www.tsunami.gov",
    "api.weather.gov",
    "www.jma.go.jp",
    "api.tidesandcurrents.noaa.gov",
    "www.ndbc.noaa.gov",
]
REPLAY = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "replay"
DROP = {"content-encoding", "content-length", "transfer-encoding", "connection"}


def load_recordings() -> dict[str, dict]:
    """url -> the first recorded exchange, from every replay file."""
    out: dict[str, dict] = {}
    for path in sorted(REPLAY.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out.setdefault(rec["url"], rec)
    return out


def freshen(body: bytes) -> bytes:
    """Move a USGS summary so its newest quake is 20 minutes old now (the recordings are days old)."""
    try:
        data = json.loads(body)
        feats = data["features"]
        newest = max(f["properties"]["time"] for f in feats)
    except (ValueError, KeyError, TypeError):
        return body
    shift = int(time.time() * 1000) - 20 * 60 * 1000 - newest
    for f in feats:
        for key in ("time", "updated"):
            if f["properties"].get(key):
                f["properties"][key] += shift
    return json.dumps(data, separators=(",", ":")).encode()


class Origin:
    def __init__(self) -> None:
        self.rec = load_recordings()
        self.faults: dict[str, str] = {}
        self.log: list[dict] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ssl.SSLError):
                    return
                lines = head.decode("latin-1").split("\r\n")
                method, target = lines[0].split(" ")[:2]
                headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
                host = headers.get("host", "").split(":")[0].lower()
                mode = self.faults.get(host, "ok")
                entry = {"iso": datetime.now(UTC).isoformat(timespec="milliseconds"), "host": host, "target": target,
                         "mode": mode, "status": None, "ua": headers.get("user-agent", "")}
                self.log.append(entry)
                if mode == "reset":
                    entry["status"] = "reset"
                    sock = writer.get_extra_info("socket")
                    with contextlib.suppress(Exception):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    writer.transport.abort()
                    return
                if mode == "503":
                    status, body, ctype = 503, b'{"error":"injected"}', "application/json"
                else:
                    rec = self.rec.get(f"https://{host}{target}")
                    if rec is None:
                        status, body, ctype = 404, b"no recording", "text/plain"
                    else:
                        status = rec["status"]
                        body = rec["body"].encode("utf-8") if "body" in rec else b""
                        ctype = rec.get("headers", {}).get("content-type", "application/json")
                        if "geojson" in target and "/summary/" in target:
                            body = freshen(body)
                entry["status"] = status
                writer.write(
                    f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    f"Last-Modified: {datetime.now(UTC):%a, %d %b %Y %H:%M:%S GMT}\r\n\r\n".encode() + body
                )
                await writer.drain()
                if headers.get("connection", "").lower() == "close":
                    return
        except (ConnectionError, ssl.SSLError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target = lines[0].split(" ")[:2]
            n = next((int(ln.split(":")[1]) for ln in lines if ln.lower().startswith("content-length")), 0)
            data = json.loads(await reader.readexactly(n)) if n else {}
            if method == "POST" and target == "/fault":
                if data["mode"] == "ok":
                    self.faults.pop(data["host"], None)
                else:
                    self.faults[data["host"]] = data["mode"]
                out = {"ok": True, "faults": self.faults}
            elif method == "GET" and target == "/log":
                out = self.log
            else:
                out = {"error": "unknown"}
            body = json.dumps(out).encode()
            writer.write(b"HTTP/1.1 200 X\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--control", type=int, default=9443)
    a = ap.parse_args()
    ca = TestCA(a.dir, HOSTS)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(ca.leaf_pem, ca.leaf_key)
    origin = Origin()
    servers = [await asyncio.start_server(origin.serve, "127.0.0.1", a.port, ssl=ctx)]
    ipv6 = False
    try:
        servers.append(await asyncio.start_server(origin.serve, "::1", a.port, ssl=ctx))
        ipv6 = True
    except OSError:
        pass
    servers.append(await asyncio.start_server(origin.control, "127.0.0.1", a.control))
    (a.dir / "listening.json").write_text(json.dumps({"port": a.port, "control": a.control, "ipv6": ipv6,
                                                      "hosts": HOSTS, "ca": str(ca.ca_pem)}))
    print("origin up", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
