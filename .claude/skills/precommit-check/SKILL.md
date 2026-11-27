---
name: precommit-check
description: Pre-commit checklist for this repo. Looks at the changed files and checks only the matching areas (UI, data layer, strategy, DB, docs) against the CLAUDE.md rules, plus ruff/pytest. Use before committing source changes, or when the user asks for a pre-commit check (/precommit-check).
---

# Pre-commit checklist (Nexus Trading)

Goal: before a commit, verify the current changes against the repo rules in `CLAUDE.md` and
report what passed, what was fixed and what the user must check by hand. The PreToolUse hook
(`.claude/hooks/precommit_check.py`) already blocks `git commit` when ruff or pytest fail;
this skill covers the judgement items the hook cannot.

Report to the user in Korean. Do not commit as part of this skill unless the user asked.

## 1. Collect the change set

```bash
git status --short
git diff HEAD --stat
git diff HEAD            # read it; for new files read them whole
```

Classify every changed path into the areas below and run only those sections. Say which
sections were skipped and why.

## 2. Always

- [ ] `.\.venv\Scripts\ruff.exe check src` and `.\.venv\Scripts\python.exe -m pytest src\tests -q` pass
      (run the area's tests first while fixing, the full suite last). Report exact counts.
- [ ] No secrets or runtime data staged: `.env`, `kis_*.txt`, `kis_token_cache.json`,
      `portfolio.db`, `dart.db`, `cache/`, `archive/`, `reviews/`, `holdout_ledger.jsonl`.
- [ ] Comments, docstrings, log/exception messages and test code are English (exceptions: the
      verbatim external-data literals and the Korean UI/variant strings listed in CLAUDE.md).
- [ ] Docs in the same commit when needed: a `roadmap.md` row for a non-trivial change,
      `docs/ui.md` for UI changes, `CLAUDE.md` when the architecture or a convention changed.
- [ ] A changed literal that tests assert, or that `docs/ui.md` quotes, is changed there too.

## 3. UI (`src/ui/`, `src/main.py`, `src/threads/`)

- [ ] Every visible string is `tr("한국어", "English")` with both halves filled; persisted values
      (DB fields, status codes, saved names) are not translated.
- [ ] Colors from `theme.py` rules / object-name roles (`#primary`, `#danger`, `#muted`, `#faint`),
      no hardcoded hex; fonts via `create_font()` and `FONT_*`; stylesheet fonts via
      `FONT_FAMILY_CSS`, sizes in `pt`.
- [ ] Buttons via `make_button`, data tables via `setup_table`, toolbar labels registered for the
      shared toolbar width.
- [ ] No `data_fetcher` / network call in a UI-thread slot; new workers are `QThread`s in
      `src/threads/`, registered with `self._track_thread(...)`; `finished` connected to bound
      methods, per-request context echoed back in the signal (no lambdas capturing it).
- [ ] Bulk table repaints wrapped in `setUpdatesEnabled(False)` / `finally: setUpdatesEnabled(True)`.
- [ ] New tab state that must survive a language switch is in `export_state()` / `import_state()`.
- [ ] Tabs do not reference each other; cross-tab wiring goes through `MainWindow` signals.
- [ ] For a UI refactor: a throwaway offscreen (`QT_QPA_PLATFORM=offscreen`) script that dumps
      widget / axes state before and after, diffed (keep it in the scratchpad, not the repo).

## 4. Data layer (`src/data/`, `src/data_fetcher.py`)

- [ ] Import direction: `data/` never imports `data_fetcher` or `strategy`; nothing below
      `market.py` does `from data.market import`; new code sits in the lowest layer that has
      what it needs.
- [ ] A new `requests.Session` mounts `SharedTlsAdapter`; existing module caches reused.
- [ ] Inner `try` blocks catch `PAYLOAD_ERRORS` / `KIS_ERRORS` / `OSError`; `except Exception`
      only at boundaries, marked `# boundary:` / `# kept broad:` and logged with a traceback.
- [ ] Tests patch implementation modules (`data.cache`, `data.history`, ...), not the facade.
- [ ] `data_fetcher` gains a name only when a UI/thread caller needs it.
- [ ] Non-trivial fetch-logic change: a throwaway old-vs-new comparison on random inputs.
- [ ] DART: readers use `open_readonly()`; tests never reach the real `dart.db` or key.

## 5. Strategy (`src/strategy/`)

- [ ] The spec (`src/strategy/<name>/<name>.md`) is updated in the same commit; docstrings cite
      its section numbers; deviations from the spec text are recorded in section 9.
- [ ] Results that should not change are rerun from a saved snapshot
      (`python -m strategy.trend_following.run --snapshot ...`) and compared; intended changes
      are written down.
- [ ] Nothing loaded data past `DEV_END` by accident (check `holdout_ledger.jsonl` for a new
      line). Never delete that file.
- [ ] Screens get current signals only through `strategy.signal_api` / `strategy.registry`;
      shared price structures stay in `strategy/common/` (no strategy imports there).

## 6. DB (`src/trade_db.py`)

- [ ] Migrations run once and keep existing rows (test on a copy of `portfolio.db`, or rely on
      the `archive/auto_*` backup the app writes at start).
- [ ] Callers do not generate `orig_key`; batches use `upsert_trades()`.

## 7. Report

List, per section run: passed / fixed (what) / needs the user. Always end with what the user
should check by running the app (`.\.venv\Scripts\python.exe src\main.py`): the screens the
change touches, both languages for UI text, narrow and wide window widths for toolbars, and
anything that calls a real external service (Naver, Yahoo, KIS, DART, ECOS), since the tests
use synthetic data and no network.
