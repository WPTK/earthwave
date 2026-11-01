"""The screenshot script's colour guard."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from uat_screenshots import has_chroma


def test_has_chroma():
    assert has_chroma('<rect fill="#ff0000"/>')
    assert not has_chroma('<rect fill="#808080"/><rect fill="#ffffff"/>')


def test_greyscale_removes_chroma():
    from uat_screenshots import greyscale

    assert not has_chroma(greyscale('<rect fill="#ff0000"/><rect fill="#fd971f"/>'))
