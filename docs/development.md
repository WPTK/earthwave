# Development

earthwave needs Python 3.13. The design and milestone history are in [`plan.md`](plan.md); project rules are in
[`CLAUDE.md`](../CLAUDE.md).

## Setup

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

On Linux and macOS use `python3.13 -m venv .venv` and `.venv/bin/python`. Use `127.0.0.1`, not `localhost`, for local
servers on Windows.

## Checks

CI runs these on Windows, Linux and macOS; run them before pushing.

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m vulture earthwave scripts --min-confidence 80
.venv\Scripts\python -m pytest -n auto
.venv\Scripts\python -m earthwave --version
.venv\Scripts\python -m earthwave --selftest
```

Snapshot tests (`pytest-textual-snapshot`) compare rendered screens in `tests/__snapshots__`. After an intentional UI
change, review the diff and run `pytest --snapshot-update`.

Tests never touch the network. Saved live payloads live under `tests/fixtures/<source>/` (marked `-text` in
`.gitattributes`; never reformat them), and `tests/fixtures/replay/*.jsonl` are recorded HTTP sessions. To re-capture
the live fixtures, run `scripts\capture_fixtures.py` (one request per URL).

## Running with a recording

```powershell
.venv\Scripts\python -m earthwave --debug
.venv\Scripts\python -m earthwave --replay tests\fixtures\replay\kamchatka_watch_to_warning.jsonl --speed 20 --notify
.venv\Scripts\python -m earthwave --record session.jsonl
```

`--replay` serves a recording instead of the network and advances a replay clock, so a Watch to Warning to Cancellation
session plays back in seconds. `--debug` writes a DEBUG log to the default log file.

## Building the binary

`earthwave.spec` builds a one-file binary with PyInstaller (a dev extra). It bundles `earthwave/data/*.json` and
`earthwave/ui/*.tcss`, Textual's own data files, and hidden imports for Textual's lazily loaded modules, `httpx`,
`platformdirs` and `sqlite3`. Python itself is bundled; the user needs nothing installed.

```powershell
scripts\build_exe.ps1            # builds dist\earthwave.exe, then runs --version and --selftest on it
```

On Linux and macOS, run `python -m PyInstaller --noconfirm --clean earthwave.spec`; the result is `dist/earthwave`.
A PyInstaller binary is built for the OS and CPU it was built on. The binary has no `tests/fixtures` folder, so its
`--selftest` skips the fixture parse and still renders a headless frame.

### Signing

The binaries are not code-signed or notarized. PyInstaller output cannot be notarized as built (that needs an Apple
Developer ID and a signed, hardened-runtime bundle), and Windows Authenticode needs a paid certificate. Expect
SmartScreen on Windows ("More info", "Run anyway") and Gatekeeper on macOS (`xattr -d com.apple.quarantine earthwave`,
or right-click Open). Verify a download against the `SHA256SUMS` file attached to the release.

## Releasing

Only on the maintainer's word.

1. In a release PR, set `__version__` in `earthwave/__init__.py` (ZeroVer: `0.y.z`, minor for features, patch for
   fixes) and add a `## [0.y.z] - YYYY-MM-DD` section to `CHANGELOG.md` in
   [Common Changelog](https://common-changelog.org/) form, composed from the "Changelog draft" in `docs/plan.md`.
2. Merge it, then tag the merge commit and push the tag:

   ```powershell
   git tag v0.y.z
   git push origin v0.y.z
   ```

3. `.github/workflows/release.yml` runs. It fails at once if the tag is not `v0.y.z`, does not equal `__version__`,
   has no dated changelog section, or is not on `main`. Otherwise it builds Windows (`.exe`), Linux (x86-64, `.tar.gz`) and macOS (arm64, `.tar.gz`) binaries, runs
   `--version` and `--selftest` on each, and creates the GitHub Release with the binaries, `SHA256SUMS` and the
   changelog section as notes.
4. Run through [`smoke-test.md`](smoke-test.md) on a machine without Python.

If a run publishes a bad release, remove the release and its tag together, fix on a branch and tag again:

```powershell
gh release delete v0.y.z --cleanup-tag --yes
```

If the workflow fails before publishing, only the tag needs deleting (`git push origin :refs/tags/v0.y.z`).
