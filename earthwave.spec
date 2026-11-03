# PyInstaller spec: one-file earthwave binary. Build with `pyinstaller earthwave.spec` (see scripts/build_exe.ps1).
# Run from the repo root; output is dist/earthwave (dist/earthwave.exe on Windows).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

datas = [
    ("earthwave/data/*.json", "earthwave/data"),
    ("earthwave/ui/*.tcss", "earthwave/ui"),
    # What --selftest parses; without these it prints SKIPPED in the binary (#259).
    ("tests/fixtures/usgs/*.geojson", "tests/fixtures/usgs"),
    ("tests/fixtures/tsunamigov/*", "tests/fixtures/tsunamigov"),
    ("tests/fixtures/ndbc/activestations.xml", "tests/fixtures/ndbc"),
    ("tests/fixtures/ndbc/46402.dart", "tests/fixtures/ndbc"),
    ("tests/fixtures/coops/water_level_9414290.json", "tests/fixtures/coops"),
    ("tests/fixtures/jma/*.json", "tests/fixtures/jma"),
]
# Textual loads widget CSS and other resources by path and imports screens and widgets lazily.
datas += collect_data_files("textual")
# --diagnose reads these versions from the installed metadata; the BSD, Apache and MIT licences of the HTTP-layer
# libraries, Textual and Rich require their notices to travel with the binary, and a dist-info directory carries its
# licence files.
for dist in ("httpx", "hishel", "stamina", "anysqlite", "tenacity", "msgpack", "textual", "rich"):
    datas += copy_metadata(dist)
hiddenimports = collect_submodules("textual") + collect_submodules("earthwave") + collect_submodules("anyio") + collect_submodules("hishel") + collect_submodules("anysqlite") + collect_submodules("stamina") + collect_submodules("tenacity") + collect_submodules("rich._unicode_data") + ["sqlite3", "httpx", "platformdirs", "msgpack"]

a = Analysis(
    ["earthwave/__main__.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mypy", "ruff", "vulture"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="earthwave",
    console=True,
    upx=False,
)
