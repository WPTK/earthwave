# PyInstaller spec: one-file earthwave binary. Build with `pyinstaller earthwave.spec` (see scripts/build_exe.ps1).
# Run from the repo root; output is dist/earthwave (dist/earthwave.exe on Windows).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("earthwave/data/*.json", "earthwave/data"),
    ("earthwave/ui/*.tcss", "earthwave/ui"),
]
# Textual loads widget CSS and other resources by path and imports screens and widgets lazily.
datas += collect_data_files("textual")
hiddenimports = collect_submodules("textual") + collect_submodules("earthwave") + ["sqlite3", "httpx", "platformdirs"]

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
