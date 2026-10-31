"""UAT fixtures. No markers: the suite runs with the rest of the tests (`pytest -n auto`)."""

import pytest

from uat.harness import SIZES, TOO_SMALL_SIZES


@pytest.fixture(params=SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def size(request) -> tuple[int, int]:
    return request.param


@pytest.fixture(params=TOO_SMALL_SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def small(request) -> tuple[int, int]:
    return request.param
