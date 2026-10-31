# Smoke test

Run this on a PC without Python installed, using the binary from the GitHub Release. It confirms the packaged app runs
on its own. Use a terminal at least 120 columns by 30 rows (Windows Terminal is best; the app needs 100x30 minimum).

## 1. Download and verify

1. Download the binary for your system and `SHA256SUMS` from the release page.
2. Check the hash matches the line for your file:
   - Windows: `Get-FileHash .\earthwave-v0.1.0-windows-x86_64.exe -Algorithm SHA256`
   - Linux: `sha256sum -c SHA256SUMS --ignore-missing`
   - macOS: `shasum -a 256 earthwave-v0.1.0-macos-arm64`
3. The binaries are unsigned. Allow it once: SmartScreen "More info" then "Run anyway" on Windows; on macOS run
   `xattr -d com.apple.quarantine <file>` and `chmod +x <file>` (Linux needs `chmod +x` too).

## 2. Command line

- [ ] `earthwave --version` prints `earthwave 0.1.0` (the release version).
- [ ] `earthwave --selftest` prints `selftest: ok`. It reports the fixture parse as skipped; that is expected.
- [ ] `earthwave --help` lists the options.

## 3. First run (online)

- [ ] `earthwave` starts, shows the quake list filling within a few seconds, and the map beside it.
- [ ] The status bar shows `feeds ok` (or names a feed that is loading) and no feed stays on `failed`.
- [ ] Arrow keys move the cursor; `Enter` opens a quake's detail screen; `Esc` returns.
- [ ] `?` opens help; `f` opens filters and changing the magnitude or hours updates the list.
- [ ] `z` cycles the map zoom; the coastline is drawn, with a recognizable world at the default view.
- [ ] A first-run config file was created in the user config directory (shown in help or the log).
- [ ] `q` quits cleanly and the terminal is restored.

## 4. Replay (no network needed for the data)

The replay file is in the repository, not the binary; copy `tests\fixtures\replay\kamchatka_watch_to_warning.jsonl`
next to the binary.

- [ ] `earthwave --replay kamchatka_watch_to_warning.jsonl --speed 20 --notify` shows the tsunami panel and banner
  turning red at the Warning, a desktop notification and a bell, then clearing at the Cancellation.
- [ ] `t` opens the tsunami screen with the alert table and gauge strip.

## 5. History and offline

- [ ] Run `earthwave`, let it load, quit. Then disconnect the network and run `earthwave --offline`: yesterday's quakes
  are listed and `h` opens the history screen with day-by-day windows.

## 6. Pass criteria

Every box is ticked. Record the OS, terminal and binary name with any failure and open an issue.
