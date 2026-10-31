import json
import re
import subprocess
import sys

import pytest

import earthwave
from earthwave.__main__ import FIXTURES, main, selftest


def test_version_is_zerover():
    assert earthwave.__version__ == "0.0.1"
    assert earthwave.__version__.startswith("0.")


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert earthwave.__version__ in capsys.readouterr().out


def test_selftest(capsys):
    assert selftest() == 0
    assert "selftest: ok" in capsys.readouterr().out


def test_selftest_subprocess():
    out = subprocess.run([sys.executable, "-m", "earthwave", "--selftest"], capture_output=True, text=True, check=True)
    assert "selftest: ok" in out.stdout


def test_usgs_feed_shape():
    data = json.loads((FIXTURES / "usgs" / "significant_month.geojson").read_text(encoding="utf-8"))
    feat = data["features"][0]
    assert {"mag", "place", "time", "detail"} <= feat["properties"].keys()
    assert len(feat["geometry"]["coordinates"]) == 3


def test_usgs_detail_has_products():
    data = json.loads((FIXTURES / "usgs" / "detail.geojson").read_text(encoding="utf-8"))
    assert {"losspager", "shakemap"} <= data["properties"]["products"].keys()


def test_dart_rows():
    rows = [r for r in (FIXTURES / "ndbc" / "46402.dart").read_text(encoding="utf-8").splitlines() if r[:1] != "#"]
    assert rows
    assert re.match(r"^\d{4} \d\d \d\d \d\d \d\d \d\d [123] ", rows[0])


def test_selftest_fails_on_empty_fixture(tmp_path, capsys):
    import shutil

    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, root)
    (root / "jma" / "list.json").write_text("[]", encoding="utf-8")
    assert selftest(root) == 1
    assert "FAILED" in capsys.readouterr().err


def test_selftest_reports_skip(tmp_path, capsys):
    assert selftest(tmp_path / "missing") == 0
    assert "SKIPPED" in capsys.readouterr().out
