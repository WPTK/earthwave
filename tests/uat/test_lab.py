"""L1, L2, L3 and the origin half of L4: the published exe against the S8b fake origin (CI runners only).

The hosts file of a disposable runner points the six feed names at a local TLS origin (``scripts/uat/s8b/origin.py``)
that serves the shipped recordings and can fail, empty, delay or hang any host on command; the exe trusts its test CA
through SSL_CERT_FILE. Nothing in the app changes. Env: EARTHWAVE_EXE, S8B_DIR (origin work dir), UAT_OUT.

Every screen the tests read goes through ``snap``; ``test_l2_*`` (last in the file) greps all of it.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pytest

from uat.known import report
from uat.target import EXE, evidence, launch, needs_kit, status_line

WORK = Path(os.environ.get("S8B_DIR", "."))
pytestmark = [
    needs_kit,
    pytest.mark.skipif(
        not (EXE and (WORK / "listening.json").is_file()),
        reason="needs EARTHWAVE_EXE and a running S8b origin (S8B_DIR)",
    ),
]

RENDERED: list[tuple[str, str]] = []
STUCK = re.compile(r"Waiting for|\bloading\b|Loading", re.I)


def info() -> dict:
    return json.loads((WORK / "listening.json").read_text())


def control(path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{info()['control']}{path}",
        data=json.dumps(body).encode() if body else None,
        method="POST" if body else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fault(mode: str, host: str = "*") -> None:
    control("/fault", {"host": host, "mode": mode})


def usgs_polls(since: int = 0) -> int:
    return sum(1 for e in control("/log")[since:] if e["host"] == "earthquake.usgs.gov" and "2.5_day" in e["target"])


def snap(term, test: str, name: str) -> str:
    text = evidence(term, test, name)
    RENDERED.append((f"{test}/{name}", text))
    return text


@pytest.fixture(autouse=True)
def healthy_origin():
    fault("ok")
    yield
    fault("ok")


def start(*args: str):
    return launch(*args, env={"SSL_CERT_FILE": info()["ca"]})


def quit_app(t) -> None:
    t.send_keys("q")
    assert t.wait_exit(10) is not None, "q did not end the process"


def stuck_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if STUCK.search(ln)]


# -- L3: a slow origin at cold start ----------------------------------------------------------------------------------


@pytest.mark.timeout(600)
@pytest.mark.parametrize("delay", [5, 20])
def test_l3_cold_start_with_a_slow_origin_updates_without_a_keypress(delay: int) -> None:
    fault(f"delay:{delay}")
    with start() as t:
        t.wait_for(lambda s: "shown of" in s.text, 60, "the dashboard itself, before any data")
        first = snap(t, f"l3-{delay}s", "at-start")
        assert first.strip(), "blank screen while the first answer is awaited"
        t.wait_for(
            lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)),
            delay * 6 + 30,
            f"quakes listed, {delay}s origin delay",
        )
        t.settle(1.0)
        done = snap(t, f"l3-{delay}s", "updated")
        assert "Waiting for USGS" not in done, "quakes are listed but the screen still says it is waiting for USGS"
        quit_app(t)


# -- L1: each source fails ----------------------------------------------------------------------------------------------

FAULT_BUDGET = {
    "500": 100,
    "badjson": 100,
    "empty": 100,
    "hang": 420,
}  # one poll's deadline is 120 s, and USGS starts with three backfills in a row


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("mode", ["500", "badjson", "empty", "hang"])
def test_l1_every_source_failing_reaches_an_error_or_empty_state_with_retry(mode: str) -> None:
    name = f"l1-{mode}"
    fault(mode)
    with start() as t:
        t.wait_for(lambda s: "shown of" in s.text, 60, "the dashboard")
        budget = FAULT_BUDGET[mode]
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            time.sleep(5)
            text = t.screen_text()
            if mode == "empty":  # on Windows the history of earlier runs may fill the list: judge the status bar
                done = "feeds ok" in status_line(text)
            else:
                done = "failed" in status_line(text).lower()
            if done and not stuck_lines(status_line(text)) and "Waiting for" not in text:
                break
        dash = snap(t, name, "dashboard")
        assert "Waiting for" not in dash and not stuck_lines(status_line(dash)), (
            f"after {budget}s of {mode} the dashboard still waits:\n" + dash
        )
        if mode == "empty":
            assert "feeds ok" in status_line(dash), dash
        else:
            assert "failed" in status_line(dash).lower(), f"{mode}: the status bar never names a failure\n{dash}"
        assert "Refresh" in dash.splitlines()[-1], "no visible way to retry (footer has no Refresh)"
        for key, label in (("t", "tsunami"), ("o", "gauges"), ("h", "history"), ("f", "filters")):
            t.send_keys(key)
            t.settle(1.0)
            screen = snap(t, name, label)
            assert screen.strip(), f"{label}: blank"
            assert not stuck_lines(screen), f"{label}: still waiting/loading under {mode}: {stuck_lines(screen)}"
            t.send_keys("escape")
            t.wait_for(lambda s: "shown of" in s.text, 10, "back on the dashboard")
        if mode != "hang":
            fault("ok")
            t.send_keys("r")
            t.wait_for(
                lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)) and "usgs" not in status_line(s.text).lower(),
                260,
                f"recovery from {mode} on its own (backoff 120 s +-10% after the first failure)",
            )
            snap(t, name, "recovered")
        quit_app(t)


# -- L4: does r re-poll USGS? ---------------------------------------------------------------------------------------


@pytest.mark.timeout(400)
def test_l4_r_repolls_usgs_measured_at_the_origin() -> None:
    with start() as t:
        t.wait_for(lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)), 90, "quakes")
        t.wait_for(lambda _s: usgs_polls() >= 1, 120, "the first steady USGS poll")
        t.settle(1.0)
        time.sleep(15)  # a person presses r some seconds after the screen filled
        n_all = len(control("/log"))
        before = usgs_polls()
        t.send_keys("r")
        time.sleep(1.0)
        toast = snap(t, "l4-r", "after-r")
        time.sleep(12)
        added = [e for e in control("/log")[n_all:] if e["host"] == "earthquake.usgs.gov"]
        shown = "Refreshing" in toast
        evidence_line = (
            f"toast Refreshing: {shown}; USGS requests within 13 s of r: {len(added)} (steady polls before: {before})"
        )
        (Path(os.environ.get("UAT_OUT", ".")) / "l4-r.txt").parent.mkdir(parents=True, exist_ok=True)
        print(evidence_line)
        report("r sends no USGS request within 13 s (toast shows: %s)" % shown, [] if added else [evidence_line])
        quit_app(t)


# -- L2: every rendered text from the runs above ----------------------------------------------------------------------

L2_WORDS = re.compile(r"Error|Exception|Traceback|http:")


def test_l2_rendered_text_has_no_raw_errors() -> None:
    hits = []
    for label, text in RENDERED:
        for ln in text.splitlines():
            if L2_WORDS.search(ln):
                hits.append(f"{label}: {ln.strip()[:200]}")
    out = Path(os.environ.get("UAT_OUT", "."))
    out.mkdir(parents=True, exist_ok=True)
    (out / "l2-hits.txt").write_text(
        f"{len(RENDERED)} screens read, {len(hits)} hits\n" + "\n".join(hits), encoding="utf-8"
    )
    print(f"L2: {len(RENDERED)} screens, {len(hits)} hits")
    for h in hits[:40]:
        print("  ", h)
    assert RENDERED, "no screens were read (the lab tests did not run before this)"
    report("rendered text contains Error, Exception, Traceback or http: (l2-hits.txt)", hits)
