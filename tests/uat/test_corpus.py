"""L8: every real capture saved in tests/uat/corpus/ is replayed through the app's parsers with ``--replay``.

The daily live job (``uat-live-daily.yml``) saves one real capture per run as an artifact kept 90 days; a person copies
captures worth keeping into tests/uat/corpus/ (a capture is 1-2 MB), and from then on every run replays them: a feed
shape the parsers once met cannot silently stop parsing.

L10: this file never touches the network (replay only), so it is part of the default offline set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from uat import rules
from uat.target import evidence, launch, needs_kit, status_line

CORPUS = sorted((Path(__file__).parent / "corpus").glob("*.jsonl"))


@needs_kit
@pytest.mark.parametrize("capture", CORPUS, ids=[p.stem for p in CORPUS])
def test_l8_saved_capture_replays_through_the_parsers(capture: Path) -> None:
    with launch("--replay", str(capture), "--speed", "30") as t:
        t.wait_for(lambda s: bool(re.search(r"[1-9]\d* shown of", s.text)), 90, "quakes parsed from the capture")
        t.settle(2.0)
        text = evidence(t, "l8", capture.stem)
        assert not rules.violations(text, t.cols, t.rows), rules.violations(text, t.cols, t.rows)
        assert "failed" not in status_line(text).lower(), f"a feed of the capture did not parse: {status_line(text)}"
        t.send_keys("q")
        assert t.wait_exit(10) is not None
        assert "Traceback" not in t.raw_stream()


def test_l8_corpus_is_not_empty() -> None:
    assert CORPUS, "tests/uat/corpus has no capture: copy one from the uat-capture artifact of the daily job"
