"""Known defects, as the tests record them: ``xfail`` with the issue number, written before any fix.

The test asserts what a user should get. While the defect is open it fails, which pytest reports as xfail and the run
stays green; the day the defect is fixed the test passes, strict turns that into a failure, and the marker comes off.
``scripts/uat/check_xfails.py`` fails a run whose xfail names an issue that is closed or does not exist.

``strict=False`` is for a journey written for a defect but not yet seen to fail on the platform it was written on
(the reason then says so); the day it is run there, the flag is removed.
"""

from __future__ import annotations

import pytest


def unfiled(why: str):
    """A defect observed by this suite that has no issue yet (UAT is report-only; the report lists it with its repro).
    ``check_xfails.py`` prints these so they are not forgotten; once an issue exists the marker becomes ``known``."""
    return pytest.mark.xfail(strict=True, reason=f"UNFILED: {why}")


def known(issue: int, why: str, *, strict: bool = True):
    return pytest.mark.xfail(strict=strict, reason=f"#{issue}: {why}")
