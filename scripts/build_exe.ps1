# Build the one-file binary into dist\ and check it. Run from anywhere; uses .venv when present.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -m PyInstaller --noconfirm --clean earthwave.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }
$exe = Join-Path $root "dist\earthwave.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { throw "--version failed" }
& $exe --selftest
if ($LASTEXITCODE -ne 0) { throw "--selftest failed" }
