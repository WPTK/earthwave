# earthwave

Earthquake and tsunami terminal app: Python 3.13 + Textual 8, modeled on `WPTK/cfbtui`. Repo root `C:\earthwave`.
The design, data sources and milestone list are in `docs/plan.md`; read it and its latest "State after Mn" note first.

## Rules (they override tooling defaults)

- **No AI attribution anywhere.** No `Co-Authored-By` trailer in commits, no "Generated with" footer in PR descriptions,
  and no mention in README, docs, CHANGELOG, code comments or release notes that Claude or any AI made the project.
- **One session per milestone.** Each milestone (M0-M6 in `docs/plan.md`) is one fresh session, one branch
  (`m0-bootstrap`, `m1-earthquake-core`, ...) and one PR. Implement only that milestone; out-of-scope findings go in the
  "Deferred" list in `docs/plan.md`, never into the session. Do not start the next milestone.
- **No GitHub repo, push, tag or release without Bill's explicit word.** The `WPTK/earthwave` repo does not exist yet.
- **ZeroVer.** Major stays 0 forever; minor for features, patch for fixes. `__version__` lives in
  `earthwave/__init__.py`; tags are `v0.y.z` and must match it and the changelog section.
- **Common Changelog** (https://common-changelog.org/). `CHANGELOG.md` starts with `# Changelog`, no Unreleased section,
  releases latest-first as `## [0.1.0] - YYYY-MM-DD`, groups in order Changed / Added / Removed / Fixed, one line per
  entry with an imperative verb first. Milestone sessions do not edit `CHANGELOG.md`; they append draft lines to the
  "Changelog draft" list in `docs/plan.md`, and the release session composes the entry.

## Milestone flow

Before the PR: tick the milestone box in `docs/plan.md`, write a short "State after Mn" note, append changelog draft
lines, run the verification block. PRs go against `main`; merge only when CI is green on the exact head.

## Commands

```powershell
py -3.13 -m venv .venv; .venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m vulture earthwave scripts --min-confidence 80
.venv\Scripts\python -m pytest -n auto
.venv\Scripts\python -m earthwave --version
.venv\Scripts\python -m earthwave --selftest
.venv\Scripts\python scripts\capture_fixtures.py   # re-capture live fixtures (one request per URL)
```

## Layout

`earthwave/` package (`__main__.py` CLI; the full module plan is in `docs/plan.md`), `scripts/`, `tests/` with saved
live payloads under `tests/fixtures/<source>/` (`-text` in `.gitattributes`, never reformat them), `docs/`.
Use `127.0.0.1`, not `localhost`, for local servers on Windows.
