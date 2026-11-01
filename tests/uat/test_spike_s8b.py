"""S8b: the unmodified release exe, hosts file pointing its feed names at a local TLS origin (443), test CA trusted
through SSL_CERT_FILE. A quake from the fake origin on screen, then a real 503 and a connection reset naming the failing
feed in the status bar, then recovery. The hosts file and the origin are set up by the caller (CI step or Sandbox script).

Env: EARTHWAVE_EXE, S8B_DIR (the origin's work dir with listening.json), S8B_OUT (where screens are written).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest
from uat_kit import Terminal, unavailable_reason

EXE = os.environ.get("EARTHWAVE_EXE")
WORK = Path(os.environ.get("S8B_DIR", "."))
OUT = Path(os.environ.get("S8B_OUT", "s8b-out"))
pytestmark = pytest.mark.skipif(
    not (EXE and (WORK / "listening.json").is_file()) or unavailable_reason() is not None,
    reason="needs EARTHWAVE_EXE, a running origin (S8B_DIR) and tui-test",
)


def control(path: str, body: dict | None = None):
    info = json.loads((WORK / "listening.json").read_text())
    req = urllib.request.Request(f"http://127.0.0.1:{info['control']}{path}", data=json.dumps(body).encode() if body else None,
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def failed_line(screen: str) -> str:
    m = re.search(r"failed:[^\n]*", screen)
    return m.group(0) if m else ""


def snap(term: Terminal, name: str) -> str:
    text = term.screen().text
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.txt").write_text(text, encoding="utf-8")
    return text


def test_quake_then_503_then_reset_then_recovery():
    info = json.loads((WORK / "listening.json").read_text())
    env = {"SSL_CERT_FILE": info["ca"]}
    with Terminal([EXE, "--no-config"], 130, 32, env=env) as term:
        # 1. a quake that only the fake origin can have served
        term.wait_for_text("shown of", 90)
        term.hold(3)
        ok = snap(term, "1-ok")
        assert "Port-Olry" in ok or "Vanuatu" in ok, ok
        assert "usgs" not in failed_line(ok), failed_line(ok)
        served = [e for e in control("/log") if e["host"] == "earthquake.usgs.gov" and e["status"] == 200]
        assert served, "the origin never served USGS: the exe did not reach it"

        # 2. a real 503 from the usgs host
        control("/fault", {"host": "earthquake.usgs.gov", "mode": "503"})
        term.send_keys("r")
        term.wait_for(lambda s: "usgs" in failed_line(s.text), 40, "usgs named in the failed list after a 503")
        after503 = snap(term, "2-503")
        assert any(e["status"] == 503 for e in control("/log"))

        # 3. a connection reset from the usgs host
        control("/fault", {"host": "earthquake.usgs.gov", "mode": "ok"})
        term.send_keys("r")
        term.wait_for(lambda s: "usgs" not in failed_line(s.text), 40, "recovery from the 503")
        control("/fault", {"host": "earthquake.usgs.gov", "mode": "reset"})
        term.send_keys("r")
        term.wait_for(lambda s: "usgs" in failed_line(s.text), 40, "usgs named in the failed list after a reset")
        afterreset = snap(term, "3-reset")
        assert any(e["status"] == "reset" for e in control("/log"))

        # 4. recovery
        control("/fault", {"host": "earthquake.usgs.gov", "mode": "ok"})
        term.send_keys("r")
        term.wait_for(lambda s: "usgs" not in failed_line(s.text), 40, "recovery after the reset")
        recovered = snap(term, "4-recovered")
        assert "Vanuatu" in recovered
        (OUT / "failed-lines.txt").write_text("\n".join(map(failed_line, (ok, after503, afterreset, recovered))))
