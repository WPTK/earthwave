"""Every test id in the README-claims table of docs/uat.md must exist (UAT item O)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_every_claim_in_docs_uat_names_a_test_that_exists():
    ids = re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", (ROOT / "docs" / "uat.md").read_text(encoding="utf8"))
    assert len(ids) >= 12, "the claims table has gone missing"
    for path, name in ids:
        source = (ROOT / path).read_text(encoding="utf8")
        assert re.search(rf"def {name}\b", source), f"{path}::{name} does not exist"
