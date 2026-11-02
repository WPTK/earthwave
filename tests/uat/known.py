"""Known defects, as the tests record them: ``xfail`` with the issue number, written before any fix.

The test asserts what a user should get. While the defect is open it fails, which pytest reports as xfail and the run
stays green; the day the defect is fixed the test passes, strict turns that into a failure, and the marker comes off.
``scripts/uat/check_xfails.py`` fails a run whose xfail names an issue that is closed or does not exist.

``strict=False`` is for a journey written for a defect but not yet seen to fail on the platform it was written on
(the reason then says so); the day it is run there, the flag is removed.
"""

from __future__ import annotations

import pytest


def unfiled(why: str, *, strict: bool = True):
    """A defect observed by this suite that has no issue yet (UAT is report-only; the report lists it with its repro).
    ``check_xfails.py`` prints these so they are not forgotten; once an issue exists the marker becomes ``known``.
    ``strict=False`` for a check whose outcome has not been seen yet."""
    return pytest.mark.xfail(strict=strict, reason=f"UNFILED: {why}")


def report(why: str, findings) -> None:
    """Report-only check: any finding ends the test as an xfail whose reason carries the count, so the run stays green and
    the summary lists it; an exception before this line is a real failure (a marker would hide it)."""
    if findings:
        pytest.xfail(f"UNFILED: {why}: {len(findings)}")


def known(issue: int, why: str, *, strict: bool = True):
    return pytest.mark.xfail(strict=strict, reason=f"#{issue}: {why}")
