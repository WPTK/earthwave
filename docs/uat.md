# User acceptance testing

How earthwave is accepted before a release, what each part proves, and what it cannot. Run order and commands are in
[`development.md`](development.md#user-acceptance-tests-testsuat).

## Severity

| Level | Meaning | Milestone |
| --- | --- | --- |
| P0 | Data loss, crash on start, wrong tsunami information shown as right | current patch release, stops the release |
| P1 | A dead key, a click that does the wrong thing, a screen that contradicts another, a broken install path | current patch release |
| P2 | Missing affordance, clipped text, a test or tool gap | next minor |
| P3 | Cosmetic or testability nuisance | next minor |
| P4 | Idea | backlog |

## Exit criteria (numbers)

A release candidate passes when all of these hold:

- 0 open P0 and 0 open P1 on the milestone.
- Every binding, help key, footer label and clickable has a test (`test_inputs.py` meta-tests: 0 uncovered).
- State matrix: 0 unexpected failures; every `xfail(strict=True)` names an open issue.
- Real-input run on the exact release exe: 0 failures, SHA256 matches `SHA256SUMS`.
- `earthwave --version` and `--selftest` exit 0 on a clean install of each published asset.
- `ruff check .` and `vulture` clean; `pytest` has 0 failures (a flake is re-run alone and reported).

## README claims mapped to tests

`tests/uat/test_readme_claims.py` fails if a test named below no longer exists.

| README claim | Test |
| --- | --- |
| The first run shows live quakes in a list and on a map | `tests/uat/test_realinput.py::test_first_run_dashboard_and_clean_exit` |
| Keys in the table work (`j k Enter f s / m z t o h a r q`) | `tests/uat/test_inputs.py::test_key_press_changes_observable_state` |
| `?` opens the full key list and it only names real keys | `tests/uat/test_inputs.py::test_every_key_the_help_text_names_is_a_real_binding_with_a_case` |
| The mouse works on rows, the map, the footer and buttons | `tests/uat/test_realinput.py::test_every_footer_label_clicked_with_the_mouse` |
| At least 100x30; below that the app asks for a bigger window | `tests/uat/test_realinput.py::test_resize_to_too_small_shows_the_message_and_recovers` |
| The map appears at 120 columns or more | `tests/uat/test_matrix.py::test_matrix_combination` |
| `--offline` browses saved history without the network | `tests/uat/test_realinput.py::test_offline_flag_says_so_and_nothing_contradicts` |
| `--record` and `--replay` | `tests/uat/test_realinput.py::test_first_run_dashboard_and_clean_exit` |
| `--min-mag`, `--hours`, `--home` validate their input | `tests/uat/test_cli_negative.py::test_bad_argument_is_refused_with_one_line_and_no_traceback` |
| `--version` and `--selftest` check an install | `tests/uat/test_cli_negative.py::test_version_and_selftest_exit_zero_without_a_terminal` |
| A tsunami alert raises a banner and the tsunami screen shows it | `tests/uat/test_realinput.py::test_tsunami_screen_rows_clicked_with_the_mouse` |
| Feeds that fail are named, and recovery clears them | `tests/uat/test_walkthrough.py::test_w4_offline_and_back_says_so_both_ways` |
| Every screen is reachable by keyboard only and by mouse only | `tests/uat/test_walkthrough.py::test_w1_every_screen_by_keyboard_only` |
| The binary is the published file (checksum) | `tests/uat/test_realinput.py::test_exe_matches_published_checksum` |

Claims with no test (see the issues): the install lines for every OS (#225), Python 3.12 (#224), the map look (#226),
sixel drawing (#238), upgrade from 0.1.0 (#239).

## Baseline status (items A to P)

| Item | Status |
| --- | --- |
| A CI OS x Python matrix | gap, #234 (3 OS, Python 3.13 only) |
| B wheel, sdist, twine, package data | gap, #234 |
| C post-release smoke | gap, #233 (P1) |
| D README commands executable | built: the install lines were run by hand and reported on #225; the claims table above is checked by a test |
| E cold start | the coordinator's |
| F terminal compatibility | partial: NO_COLOR and ASCII glyph tests exist (`tests/test_no_color.py`), resize is in the real-input run; tmux, SSH, conhost gap, #238 |
| G error handling and `--diagnose` | negative CLI cases built (`test_cli_negative.py`); `--diagnose` gap, #235 |
| H hypothesis tests | gap, #236 |
| I static quality | ruff and vulture in place; mypy, pip-audit, checksum check gap, #237 |
| J what stays unverified | below |
| K severity rubric | built, above |
| L negative and boundary cases | built for the command line; UI-level boundaries are in the matrix sizes |
| M upgrade and uninstall | gap, #239 |
| N numeric exit criteria | built, above |
| O README claims to tests | built, above |
| P parser fuzzing | gap, #236 |

## Sixel and cross-OS items

| Item | Status |
| --- | --- |
| (a) pure-Python sixel decoder with golden tests | in place: `tests/sixel_decode.py`, `tests/test_sixel.py` |
| (b) DA1 detection as a pure function | in place (parse and decision table); pty fake-terminal fixture gap, #238 |
| (c) no reply lands on braille with no DCS bytes | decision table in place; real-pty timing gap, #238 |
| (d) queries before Textual starts | gap, #238 (queries are sent from the running app; the late-reply path is tested) |
| (e) environment override of the renderer | gap, #238 (config key only) |
| (f) CI matrix windows-2025, ubuntu-24.04, macos-26, macos-15 | gap, #234 |
| (g) tmux cell | gap, #238 |

## What stays unverified

The suite was built on one Windows 11 machine. Not run: macOS, real Linux terminals (WSL and the Linux release tarball
were not exercised by this suite), tmux, SSH, legacy conhost, and the pixel output of sixel in a real Windows
Terminal. The real-input run goes through ConPTY, which answers no sixel query, so it covers the braille path only.
