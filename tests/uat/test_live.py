"""The daily live run: the real program against the real feeds (UAT_NETWORK=1), with the screen rules on after every
step and ``--record`` always on so a failure can be replayed offline. Never part of PR CI."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from uat import rules
from uat.target import evidence, launch, log_text, needs_kit, needs_network, ready

pytestmark = [needs_kit, needs_network]

OUT = Path(os.environ.get("UAT_OUT") or ".")


def test_live_session_keeps_the_rules_and_records(tmp_path: Path) -> None:
    rec = OUT / "live-record.jsonl"
    rec.parent.mkdir(parents=True, exist_ok=True)
    with launch("--record", str(rec)) as t:
        ready(t, timeout=90)
        for step, key in enumerate(
            ("j", "j", "enter", "escape", "s", "s", "t", "escape", "o", "escape", "m", "m", "?", "?", "f", "escape")
        ):
            t.send_keys(key)
            time.sleep(0.4)
            text = evidence(t, "live", f"{step:02d}-{key}")
            found = rules.violations(text, t.cols, t.rows)
            assert not found, f"after {key!r}: {found}\n{text}"
        t.settle(2.0)
        text = t.screen_text()
        assert re.search(r"[1-9]\d* shown of", text) or "No quakes match" in text, text
        assert "Traceback" not in log_text(t)
        t.send_keys("q")
        assert t.wait_exit(10) is not None
    assert rec.is_file() and rec.stat().st_size > 0, "--record wrote nothing"
