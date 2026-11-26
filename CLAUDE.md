# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A single-user PyQt6 desktop app for tracking a Korean/US equity portfolio, with four
top-level tabs: "Trading Universe" (KOSPI/KOSDAQ watchlist with live prices and indicators;
NASDAQ and S&P500 can be switched on in its market toggles, off by default; docs/ui.md 2.1. The NASDAQ toggle
("NASDAQ 150", stored key `NASDAQ 150` since 2026-11-26, `NASDAQ 100` before and migrated on load) is the 150 largest NASDAQ-listed names, ADRs included, not the Nasdaq-100 index; docs/ui.md 8.32), "Trading History"
(manually-entered trade log backed by SQLite), "Total Assets" (weekly asset snapshots vs.
KOSPI and USD) and "Strategy" (research backtests; one sub-tab per strategy package, currently
only Trend Following). Nothing places orders.

**Strategy layer: reset on 2026-09-28 and rebuilt from
`src/strategy/trend_following/trend_following.md`; the spec is now at v07 (2026-11-18: the C-series trend line
`score_ma_long` is 60, a user decision in spec 10-13). v07 is the first validation target and the basis for all
further development; v06 was an intermediate step. The v07 development-window baseline (all ten rows, one run) is
spec 10-14; no row is an adoption candidate (H not met).** The old
packages (rebalance, ma_cross, the first trend_following, shared base/metrics/costs, dialogs, five
`QThread`s, `tools/kr_trend_backtest.py`) were deleted (git `070f639` and earlier) and nothing of
them was reused. The `strategy.trend_following` package implements spec sections 2-4 and 6: cost
model 4-1, signals L1-L4 + exits, a t+1-open portfolio engine with annual reset, BM1-BM4 on the
same engine, 6-4 metrics, the 6-1 event study, the A0-A5/B matrix and its markdown report, the
2-5 "trend + pullback" scoring mode (v04; R10 dropped from the timing score in v06) with its
C1-C3 backtest policies (9-16), the 6-1-2 factor IC / quintile analysis (9-13), and the
robustness runner with the checklist-form report (6-4/6-5, 9-17). Section 9 of the spec maps
modules to spec sections and records every implementation decision that deviates from the text
(current-constituent universe, year-end trim at the close, no weekly re-weighting, no sector
cap, KODEX 200TR as BM1 data, CD91 from ECOS/CSV/constant). **Real-data results are recorded in
spec 9-19** (development window 2021-2024): the J1 verdict is that no variant is adoptable, all
ten were discarded, and the 2025 hold-out had not been opened as of that record. Since
2026-11-13 the Strategy tab's end date defaults to today and a run past `DEV_END` passes
`allow_holdout=True` and is labelled as including the hold-out (spec 9-23); running one
counts as opening it. The scoring mode also feeds the
Trading Universe tab's "전략 신호" button (was "Trend Score", C1 only; since 2026-11-16 a menu picks any
variant A0-C3 and `signal_provider.pick_signals` lists its buy candidates / exit signals with the backtest's own
signal code, spec 9-29; both it and the MA chart's "전략 점검" check-box menu (`ui/strategy_check_section.py`,
`signal_provider.check_stock` per stock, `StrategyCheckThread`) go through `src/strategy/signal_api.py`), which pins the Top 10 / Bottom 10 to the top
of its table (`SignalPicksThread`; Top rows, then Bottom rows, then the rest in the default
order, with a `Top n (score)` / `Bottom n (score)` tag in the identity cell; the checkable
button unpins on the second click; decisions in spec 9-4). Validation reruns use saved dataset
snapshots and the headless runner (`python -m strategy.trend_following.run`), not the UI.

The repo is a git repository (branch `master`). Commit or branch as usual; the old
`archive/backup_<timestamp>/` copy-before-editing convention is no longer needed.
`AutoBackupThread` still writes `archive/auto_<timestamp>/` snapshots of `portfolio.db` (via
the SQLite online backup API) and `custom_settings.json` on every start (last 7 kept);
`archive/` is gitignored.

## Running

All source lives under `src/`. Runtime files (`.env`, `portfolio.db`, the cache/state JSON
files, `app.log`, `archive/`) live at the repo root and are resolved through `src/paths.py`
(absolute paths derived from the module location), so the app behaves the same from any
working directory:

```powershell
.\.venv\Scripts\python.exe src\main.py
```

### Verification

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests -q      # ~700 tests, no network, ~30 s
.\.venv\Scripts\ruff.exe check src                      # pyflakes rules only (ruff.toml)
```

Run pytest from the repo root or from `src/` (`tests/conftest.py` puts `src/` on `sys.path`;
the `tests/` folder is a package).
Tests patch the implementation modules (`data.cache`, `data.history`, `data.fx`, `data.collectors.yahoo`),
never names on the `data_fetcher` facade. Widgets can be built and driven headlessly with
`QT_QPA_PLATFORM=offscreen` (the tests construct tabs and dialogs that way), but nothing can be
looked at, so for UI refactors write a throwaway characterisation script that dumps widget /
matplotlib-axes state before and after and diff the two (done for `StockMaDialog` and
`TradingHistoryTab._build_ui` on 2026-09-19); for non-trivial changes to fetch logic
write a throwaway script comparing old vs. new behaviour on random inputs
(`docs/history/changelog_optimization_2026-08-11.md` shows the pattern;
`docs/history/test_plan_2026-08-29.md` is an old manual-check list kept for reference).
Dev tooling is in `requirements-dev.txt`
(`-r requirements.txt` + pytest + ruff); runtime pins are in `requirements.txt`.

## Architecture

- **`src/strategy/`**: `registry.py` (the one strategy list), `signal_api.py` (current-signal seam:
  provider protocol, result types, `SignalCache`), `common/` (strategy-agnostic `PriceBook` and the
  daily-price / flow loader) and one sub-package per strategy.
- **`src/strategy/trend_following/`** (spec `trend_following.md` v07 in the same folder,
  facade `strategy.trend_following`): `config.py` (`SPEC_VERSION`, `StrategyParams`, `VARIANTS`
  A0-A5/B and C1-C3, `V06_C_VARIANTS` C1S-C3S (the v06 intermediate 50-day line, spec 10-13; headless runner only, never in
  the UI lists; `get_variant` looks them up with the matrix rows; the 10-11 ids C1L-C3L were removed in 10-14), `COST_MULTIPLIERS`, `PERIODS`, `E2_NEIGHBORS`, benchmark ETF codes), `costs.py` (`CostModel`/`TradeCost`,
  year-keyed tax table, 2023-01-25 tick-ladder reform, `scaled()`/`for_etf()`), `signals.py`
  (date x ticker arrays for L1-L4 and the exits; `regime_state` is the weekly-held L1 state),
  `dataset.py` (`PriceBook`/`Dataset`; `build_dataset` is pure and what tests feed,
  `load_dataset` fetches through `data.*`; `markets=` other than KOSPI alone goes through
  `_load_multi_market`: KRW-ranked union of KOSPI/KOSDAQ/NASDAQ/NYSE, US prices in KRW,
  per-market index in `Dataset.index_by_market`, spec 9-25), `backtest.py` (`Engine` + `Policy`, t+1-open fills
  with sells first, cash interest, annual reset at the year's last close, per-year cost
  ledger; `TrendFollowingPolicy`), `benchmarks.py` (BM1-BM4 as policies on the same engine),
  `metrics.py` (6-4 scorecard incl. the BM1/BM3-relative block), `event_study.py` (6-1),
  `research.py` (`ResearchRequest` -> `run_research` -> `ResearchResult.to_markdown()`),
  `scoring.py` (2-5: `compute_scores` -> `ScoreBook`, `weekly_recommendation` ->
  `Recommendation`, `load_score_inputs` / `run_universe_scoring` for the Universe button; the
  loader uses `data.cache.start_date()` so a fresh Universe refresh means no network calls),
  `scoring_policy.py` (`ScoringPolicy`: the C1-C3 orders from `compute_scores`' ranks, 9-16),
  `factor_ic.py` (6-1-2: `run_factor_ic` -> `FactorICResult`, `render_markdown`; 9-13),
  `robustness.py` (`RobustnessRequest` -> `run_robustness` -> `render_robustness_markdown`:
  E1 weekdays, E2 pre-registered neighbours, E4 top-trade share, E5 trial counter; 9-17),
  `schedule.py` (`weekly_check_dates`: the weekly check days ahead of time from the KRX calendar,
  no engine/UI caller yet; 9-20), `snapshot.py` (`save_snapshot`/`load_snapshot` of a `Dataset` under `cache/snapshots/` with a
  SHA-256 manifest, for byte-identical reruns), `run.py` (headless CLI: `--build-snapshot`,
  `--snapshot`, `--factor-ic`, `--robustness`, `--variants`, ...), `signal_provider.py` (its `PROVIDER`: `pick_signals` / `check_stock`, spec 9-29), `raw_check.py` (A5 sample check
  of split-adjusted vs unadjusted prices), `variant_info.py` (`describe_variant`: each matrix
  row's rules in Korean, built from the `Variant` flags and `StrategyParams`). The
  Strategy tab runs the research matrix; its variants are picked in `TrendConditionsDialog` (a `VariantPickerDialog` plus the run conditions)
  (list + description of the highlighted row; every row tickable, flows are loaded when A3-A5 are chosen; no flows or event-study switches since 2026-11-15);
  factor IC and robustness reports come from `run.py` only.
  Strategy code imports `data.*` directly (never `data_fetcher`); `data/` never imports it.
  Tests live in `src/tests/strategy/trend_following/` on synthetic price paths
  (`helpers.synthetic_dataset`).
- **`src/main.py`** (~250 lines): `MainWindow` builds the four tabs, wires cross-tab
  signals, owns the 60-second `global_auto_timer` (the only auto-refresh timer; the "Auto
  Update" checkbox starts and stops it and everything downstream), the app stylesheet, and
  logging setup (root INFO; `app.log` gets INFO and above, the console WARNING and above).
- **`src/ui/`**: `universe_tab.py` (`UniverseTab`), `history_tab.py` (`TradingHistoryTab`),
  `assets_tab.py` (`TradingRecordTab`), `strategy_tab.py` (`StrategyTab`: a `QStackedWidget` of the
  `STRATEGY_TABS` pages (built from `strategy.registry.STRATEGIES`) switched by each page's strategy selector; merges their `collect_threads_to_stop()`), `trend_following_tab.py`
  (`TrendFollowingTab`: run toolbar (전략 selector | 시장 toggles KOSPI·KOSDAQ·NASDAQ·NYSE | 기간 | 매매전략... | 중지 백테스트, one line or two fixed lines by width; buttons at the shared toolbar width) + status line, rules and run conditions in `ui/dialogs/trend_conditions.py` `TrendConditionsDialog` (`TrendConditions`); docs/ui.md 8.19 -> result head (basis, "다음 실행에 적용", Reset (`clear_result`), Save) ->
  `TrendFollowingResearchThread` -> variant/cost segments, KPI cards, NAV + drawdown chart,
  target-check card, result tabs (comparison, all metrics, monthly, trades,
  warnings), "Save Report" to `reports/`; docs/ui.md 8.10), `strategy_widgets.py` (that
  screen's widgets: `KpiRow`, `TargetCheckCard`, `SharpeBarDelegate`, `SegmentRow`, table
  fillers, `DEFAULT_TARGET_SHARPE` (1.0; the run's target comes from the form)/`TARGET_MDD`; the trade list shared by the tab and its popup is
  `dialogs/strategy_trades.py` `StrategyTradesView`), `strategy_check_section.py` (`StrategyCheckSection`: the
  MA chart dialog's "전략 점검" block -- a check-box menu of the variants that apply to the stock's market and the
  generic `signal_api.StockCheck` drawn as verdict, score line, metric cards and condition badges; the
  Universe tab passes a `SignalContext` (its rows, its `SignalCache`, the pinned variant) through
  `StockMaDialog(signal_ctx=...)`),
  `widgets.py` (since 2026-11-15 only a re-export of: `table_items.py` `ColSpec`/`NumericItem`,
  `universe_columns.py` the Universe `COLUMNS`/`COL_*`, `universe_table.py` `StockTable`,
  `filter_popups.py` `FilterPopup`/`SignalScanPopup` (the 신호 포착 panel), `table_headers.py` `GroupedHeaderView`,
  `heat_legend.py` `HeatLegend`; new code imports from those modules), `delegates.py`
  (every custom-painted table cell: `CellDelegate` base + the Universe/History delegates),
  `ma_chart.py` (`StockMaLauncherMixin`, the MA-chart dialog launcher; `UniverseTab` is its
  only user now),
  `universe_views.py` (Trading Universe filter state and the 신호 포착 range conditions (the saved views
  and quick picks were removed on 2026-11-19; signals are saved in `custom_settings.json` `signals`), pure: `matches()` drives both the
  table's row hiding and the per-signal counts; docs/ui.md 8.9),
  `colors.py` / `theme.py` (the dark "Soft Slate" theme since 2026-11-14, docs/ui.md 8.16:
  `app_palette()` and `apply_matplotlib_theme()` are installed by `main.py` next to the QSS, so
  no widget or chart falls back to default black text; PROFIT/LOSS color rule, design tokens and the global QSS —
  buttons get their look from the `#primary`/`#danger` objectName roles, secondary labels
  from `#muted`/`#faint`; never a hardcoded hex for something `theme.py` has a rule for; the
  `QCalendarWidget` rules have no current caller and are kept for the next `QDateEdit`),
  `dialogs/` (one module per
  dialog group: `index_ma`, `stock_ma`, `trade_edit`, `trade_history`, `assets_graph`,
  `holdings_summary`, `assets_report`, `stock_report`, `variant_picker`, `trend_conditions`, `strategy_trades`, `add_ticker`, `add_record`, `signal_conditions` (신호 추가); import from `ui.dialogs`;
  `StockMaDialog` is split into mixins over shared dialog state since 2026-11-15: `stock_ma_plot`
  (panels, slots, margins, end labels, toggles), `stock_ma_hover` (crosshair/hover), `stock_ma_view`
  (pan/zoom/period/autofit), plus `stock_ma_side` (right panel), `stock_ma_consts`, `stock_ma_helpers`),
  `history_table.py` (cell factories, `fill_table_rows`, `SectionTable` + the column
  `SECTIONS` for the history grid), `history_calc.py` (pure P/L maths, no Qt:
  `compute_pl_fields`, `build_monthly_rows`, `summarize_positions`), `assets_report.py` (pure maths for the Total Assets graph's
  early/mid-month "Report": holdings and cash rolled back from Deposit + Withdrawal, next
  to KOSPI), `common.py`
  (`create_font` + the `FONT_*` point-size scale, `FONT_FAMILIES` / `FONT_FAMILY_CSS`,
  `apply_matplotlib_font`, status color constants,
  input validators, `make_button`/`BTN_H` (every push button), `register_toolbar_labels`/`toolbar_button_width`/`fix_toolbar_widths` (the one toolbar button width shared by Universe, History, Total Assets and Strategy; each tab module registers its labels), `setup_table`/`TABLE_ROW_H` (every data
  table), `make_footer_label`/`TAB_MARGINS`/`TAB_SPACING` (the tab skeleton), `FlowLayout` (wrapping
  rows; the Strategy settings form), `install_table_keys`
  (Ctrl+C/Enter on a table), `KpiStrip` (History's KPI row), `KpiCard` / `make_badge` (Strategy
  cards and status badges; all docs/ui.md 8), `atomic_save_json` /
  `safe_load_json`, `retire_thread`, `ThreadOwnerMixin`). Tabs never
  reference each other
  directly; `MainWindow` connects their signals (`status_message`, `refresh_started`,
  `auto_lightweight_tick`, `total_asset_updated`).
- **`src/threads/`**: every network call the UI triggers runs in a `QThread` subclass here
  (`AllDataFetchThread`, `UniverseLightweightFetchThread`, `PositionPriceFetchThread`,
  `RealtimePriceThread`, `StockMaThread`, `IndexMaThread`, `SingleStockFetchThread`,
  `TickerValidateThread`, `AssetMetricsPreloadThread`, `AssetReportThread`, `KisAccountThread` (Trading History's KIS 조회: deposit, holdings, executed orders),
  `GeminiStockReportThread`, `AutoBackupThread`; `strategy_threads.py` holds
  `TrendFollowingResearchThread`, which loads the dataset and runs the research matrix, and
  `SignalPicksThread` / `StrategyCheckThread`, the `signal_api` universe run and one-stock check).
  Never call `data_fetcher` functions from a
  slot on the UI thread; add a thread class instead. Connect `finished` signals to bound
  methods, not closures, so Qt queues them onto the UI thread; when a slot needs per-request
  context (which ticker, which market, which change mode), have the thread echo it back
  in the signal (`SingleStockFetchThread.finished(result, error, ticker)`,
  `StockMaThread.finished(..., market, change_mode)`) instead of capturing it in a lambda.
- **`src/data/`**: all external data access, layered bottom-up with no import cycles
  (module-level or lazy): `cache.py` (HTTP sessions, all mounted with `SharedTlsAdapter` — one SSLContext
  with the CA bundle loaded once per process instead of once per connection, which cost ~0.1 s per new
  connection under the GIL; a new `requests.Session` in `data/` should mount it too; `LazyModule` /
  `fdr`, the FinanceDataReader import deferred to first use; `_HIST_CACHE` LRU with
  `_HIST_CACHE_STATS` and the per-key `_HIST_CACHE_FETCHED_AT` stamps that
  `_hist_df_is_stale(df, fetched_at)` checks against `_HIST_CACHE_STALE_TTL` (30 min) so a
  frame without today's bar is not re-fetched on every weekday lookup, `_YF_BULK_CACHE`,
  `start_date()`, `is_kr_code()`, `is_us_market()`, `safe_float`) ->
  `frames.py` (`_to_polars`) -> `collectors/naver.py`, `kis.py`, `krx.py` -> `flows.py`
  (`get_investor_flows`: per-stock daily foreigner/institution/retail net shares from the
  Naver frgn page, cached per ticker under `cache/investor_flows/`), `rates.py`
  (`get_cd91_series`: ECOS with `ECOS_API_KEY`, else `cd91.csv`, else empty), `krx_calendar.py`
  (`get_krx_sessions`: open days from the KIS holiday API, cached in `cache/krx_calendar.json`),
  `raw_prices.py` (KIS daily bars, raw or adjusted, never past the development window) and
  `price_crosscheck.py` (report-only KIS vs Naver comparison, `python -m data.price_crosscheck`)
  -> `listing.py`
  (`get_stock_listing`, day-scoped single-flight cache), `history.py` (`get_historical_data`
  routing KR codes to Naver, bonds to cached series, else yfinance/yahooquery/FDR), `fx.py`
  (`get_usd_krw_rate`) -> `indicators.py` (`_compute_indicators`, `fetch_historical_changes`)
  -> `collectors/yahoo.py` (`yf_quote_batch` is the one Yahoo quote entry point, US bulk
  universe) -> `market.py` (per-market universe builds, single-stock lookup, index/MA
  series; re-exports the lower names for older callers). Keep new code in the lowest
  layer that has what it needs, never add a `from data.market import` below market.py, and
  never import a strategy package from here. Uses polars internally and converts to pandas
  only at library boundaries; reuse the module-level caches rather than adding parallel ones.
- **`src/data/dart/`** (since 2026-11-26, `docs/dart_db_plan_2026-11-26.md` stages 1-3): OpenDART
  periodic reports and quarterly revenue / operating income / net income in `dart.db` (SQLite, WAL,
  repo root). `client.py` (key from `DART_API_KEY`, daily call counter `api_usage` stopping at 19,000,
  back-off, <= 5 calls/s, raw replies cached under `cache/dart/`), `corp.py`, `filings.py` (list.json ->
  `filing`, fiscal year/quarter from the report name, corrections linked), `financials.py` (fin_raw ->
  fin_std: CFS before OFS, IS before CIS, Q4 = annual - Q3 cumulative), `store.py` (schema, views
  `v_fin_latest`/`v_fin_growth`), `query.py` (`get_financials(code, as_of=)`: only filings received by
  then -- the one read entry point for screens and strategies), `lock.py` (`cache/dart/sync.lock`),
  `sync.py` (`run_sync(mode, trigger, ...)`: the only collection logic, no Qt; records `sync_run`).
  `tools/dart_sync.py` (`--init/--daily/--backfill [--tickers]/--auto`) only calls `run_sync`. Not in
  the `data_fetcher` facade. The app runs it in the background (stage 3A, docs/ui.md 8.33):
  `src/ui/dart_sync_thread.py` (`DartSyncThread` -- the plan names this path, so this QThread lives in
  `ui/`, not `threads/` -- and `DartAutoSync`, the start rule and the header label) armed by
  `MainWindow._arm_dart_sync`; tests never reach the real dart.db or key (conftest).
- **`src/data_fetcher.py`**: the single re-export facade over `data/` (data access only, no
  strategy symbols) so UI and thread code import from one place; `data/__init__.py` itself
  re-exports nothing. Nothing in `data/` imports the facade back; keep it that way. It
  lists only the names callers outside `data/` use (about 30; trimmed from 79 on
  2026-09-19), so add a name there when a new UI/thread caller needs it rather than
  importing `data.*` directly.
- **`src/trade_db.py`**: SQLite persistence (`portfolio.db`, WAL mode): the `trades` table and,
  since 2026-11-18, `kis_imports` (KIS orders already imported by Trading History's KIS 조회 —
  `apply_kis_import()` saves trades and these marks in one transaction; `ui/kis_sync.py` plans the import),
  since 2026-09-19, `asset_records` (the Total Assets tab's weekly snapshots —
  `load_asset_records()` / `save_asset_records()`, a full replace per save). Prefer
  `upsert_trades()` for trade batches. Do not generate `orig_key` values in callers:
  `upsert_trade()` without a key claims a collision-free one inside the INSERT and writes it
  back into the record. `init_db()` runs before any tab is built (`MainWindow`) because
  `TradingRecordTab` reads the DB in its constructor. `backup_to()` is the online-backup
  entry point `AutoBackupThread` uses.
- **`tools/register_secret.py`**: standalone CLI for pushing secrets to Google Cloud Secret
  Manager; unrelated to the app runtime. **`docs/history/`**: dated one-off documents.
- **`src/gemini_helper.py`**: Gemini calls for the per-stock AI report feature
  (`GOOGLE_API_KEY` in `.env`); the AI filter (2026-09-19) and portfolio diagnosis
  (2026-09-19) features were removed. Prompts were rewritten from
  Korean to English in the 2026-09-18 source-code-wide English-only pass; the stock-report
  prompt explicitly asks for a Korean-language reply (the app's end user is Korean-speaking),
  so keep that line if the prompt is edited.

### External dependencies / credentials

- `.env` (repo root, loaded via `paths.ENV_FILE`): `KRX_AUTH_KEY`, `GOOGLE_API_KEY`, and
  optionally `GEMINI_MODEL` and `ECOS_API_KEY` (Bank of Korea ECOS, for the CD 91-day rate
  the Trend Following backtest uses as BM4; without it a `cd91.csv` at the root or a 3%
  constant is used).
- KIS (한국투자증권) Open API keys are **not** in this repo: `data/collectors/kis.py` reads
  `kis_appkey.txt`, `kis_secretkey.txt` and `kis_account.txt` (10 digits: 8-digit CANO plus
  2-digit product code) from `KIS_KEY_PATH` (default `D:\Source Code\Trading MCP`). KIS calls
  fail without that folder. The issued access token is cached in plain text in
  `kis_token_cache.json` (gitignored).
- The KRX VKOSPI endpoint is disabled in code (`_VKOSPI_API_DISABLED`, persistent 403 since
  2026-08-28); `vkospi_cache.json` serves the history until KRX lifts the block.

### Local state / cache files (gitignored)

`portfolio.db` (source of truth for trades and asset snapshots), `custom_settings.json`,
`universe_cache.json`, `vkospi_cache.json`, `kis_token_cache.json`, `app.log`, `archive/`,
`holdout_ledger.jsonl` (one line per dataset load past `DEV_END` -- Strategy tab, `load_dataset` or `load_snapshot`,
written by the load layer since 2026-11-20: the hold-out opening record,
trend_following.md 10-7 -- not a cache, never delete it),
plus the legacy `trading_record.json` (read once by
`_migrate_asset_records_json`; left on disk afterwards — the import records completion in
`portfolio.db`'s `migrations` table so deleted rows are never re-imported), and
the rebuildable caches under `cache/` (`investor_flows/<ticker>.json`, `cd91.json`,
`krx_calendar.json`, and
`jp10y.json`, the last MOF JP 10Y series, served when the download fails), and `dart.db`
(DART filings and financials collected over days within the API's daily limit -- rebuildable only
slowly, so treat it as data, not a cache) with its raw replies and lock under `cache/dart/`.
All of these paths come from `src/paths.py`. They are runtime data,
not fixtures. Trading History principal/deposit/withdrawal live in `QSettings`
(scope "PortfolioManagement"/"PortfolioManagement"; migrated once from the old
"MyCompany"/"PortfolioManager" scope).

## Conventions

- All comments, docstrings, log/exception messages, and test code are written in English
  (user direction, 2026-08-29 for new/edited code; extended 2026-09-18 to a one-time sweep
  converting the remaining Korean comments/strings across `src/` to English). The exceptions
  are literal values that must match real external data verbatim, which would silently break
  if translated: Naver/KRX API field names and Korean-formatted numbers
  (`data/collectors/naver.py`, `data/collectors/krx.py`), and the `'맑은 고딕 Semilight'`
  font-family fallback name (`ui/common.py`). `gemini_helper.py`'s prompts were included in
  the sweep — see the note on that file above for the response-language consequence.
  A second exception (user direction, 2026-11-13): the strategy-variant descriptions the
  Strategy tab's picker shows (`strategy/trend_following/variant_info.py` strings, the
  `ui/dialogs/variant_picker.py` list labels) are Korean, and the tests assert those
  literals; their identifiers, comments and docstrings stay English. The same applies to
  the one-line explanations under the Strategy KPI cards and the Universe score cards
  (`ui/strategy_widgets.py` `KPI_CARDS`, the C-row metric captions in `signal_provider.py`) and the E2/A5 tooltip,
  worded as in `docs/ui_redesign_2026-11.md` (2026-11-13).
  Third and broadest (user direction, 2026-11-13): **visible UI labels are Korean** for
  buttons, toolbar labels and placeholders, context menus, dialog buttons/titles/field labels
  and message-box texts (docs/ui.md 8.11). Kept English: the four top-level tab names,
  message-box titles (they are the tab names, 8.5), metric abbreviations (CAGR, MDD, Sharpe,
  tPER, MA20 Div, RSI ...), the narrow numeric column headers, IDs (A0-C3, BM1-BM4) and
  market names. The MA chart dialogs (`StockMaDialog` incl. its Trend Score section,
  `IndexMaDialog`) followed on 2026-11-15, and on the same day table/group headers, KPI strip labels,
  status/footer/progress messages (worker threads and the strategy package's progress lines
  included), empty-table hints and chart labels. Still English by design: the research
  warnings/markdown reports and CSV/Excel export headers. Qt's own dialogs get Korean from the bundled `qtbase_ko`
  translator `main.py` installs (Korean mode only).
- **UI language (user direction, 2026-11-15; docs/ui.md 8.20):** Korean (default) or English,
  picked with the header's flag buttons (`ui/language_switch.py`, saved in `QSettings`
  `ui/language`; a switch rebuilds the main window in place without restarting:
  `main.switch_language` re-imports the `ui`/`threads` packages and carries each tab's
  `export_state()`/`import_state()` over — a new tab state that matters belongs there). Every visible string that has a Korean form is
  written `tr("한국어", "English")` (`src/i18n.py`, no Qt-widget/ui/data imports, so
  `variant_info.py` uses it too); new Korean labels must come with their English partner.
  `main.py` calls `i18n.load_language()` before importing any `ui` module, so module-level
  `tr()` constants are fine (the run-time switch re-imports those modules); tests never load the saved setting and run in Korean. Never
  translate persisted values (saved view names, status codes, DB fields) — only their display.
- UI 변경은 `docs/ui.md` 의 전역 규칙을 따른다.
- One font family app-wide, Malgun Gothic Semilight (user direction, 2026-09-19): every
  widget goes through `create_font()` (sizes from the `FONT_*` scale in `ui/common.py`),
  numeric cells included — there is no separate monospace/tabular face — and matplotlib
  charts get the same family from `apply_matplotlib_font()` at startup. Inline stylesheets
  splice `FONT_FAMILY_CSS` instead of repeating the family list, and size in `pt`, not `px`.
- Exceptions (code review 2026-11-15 #6): an inner `try` around one fetch/parse catches what
  that body can raise from outside — `data.cache.PAYLOAD_ERRORS` (requests errors, ValueError,
  KeyError, IndexError, TypeError/AttributeError from a reply of the wrong shape),
  `collectors.kis.KIS_ERRORS`, `OSError` for files — not `Exception`, so a bug in our own code is
  not reported as "no data". `except Exception` stays only at boundaries (a QThread `run()`, a
  per-item loop, a function whose contract is "empty on any failure", a third-party call that
  raises arbitrary types), marked `# boundary:` / `# kept broad:` and logged with a traceback.
- Bulk table repaints are wrapped in `setUpdatesEnabled(False)` / `finally:
  setUpdatesEnabled(True)`.
- Every tab mixes in `ui.common.ThreadOwnerMixin` and registers each worker it starts with
  `self._track_thread(thread, "<attr>")` (the optional attr retires the previous thread
  stored there via `retire_thread` and stores the new one); the mixin's
  `collect_threads_to_stop()` is what `MainWindow.closeEvent` calls, so a thread that is
  not tracked will not be stopped at shutdown.
- KR-vs-US position labels go through `is_us_market()` (one set, shared by the price
  thread and the Trading History summary).
- KR-vs-US ticker routing uses `is_kr_code()`; the daily-history lookback start is
  `start_date()` (a function, not an import-time constant).
- **Strategy rule (user direction, 2026-09-17; kept for the future re-introduction):** every
  strategy is its own sub-package `src/strategy/<name>/` and its design/spec document is
  saved as `<name>.md` inside that same folder. A sub-package has a `config.py`
  (parameters), `signals.py`, `backtest.py` and an `__init__.py` facade; tests go in
  `tests/strategy/<name>/`; docstrings and commit messages cite the md section numbers, and
  the md is updated in the same commit as the code. Strategy code imports from `data.*`;
  callers import strategy symbols from `strategy.<name>` directly, never via
  `data_fetcher`, and `data/` never imports `strategy`. Every strategy UI runs its work in
  a `QThread` under `src/threads/` and is signal generation / research only. There are no
  strategy docs at the repo root. Since 2026-11-16 a strategy is listed once, in
  `src/strategy/registry.py` (`STRATEGIES`: its Strategy tab page and its signal module); the
  signal module exposes a `PROVIDER` implementing `strategy.signal_api.SignalProvider` (variants,
  title, markets, needs_universe, labels, load, pick, check, templates), which puts it in the Universe
  "전략 신호" menu and the MA chart "전략 점검" menu. Screens never import a strategy package for
  current signals; shared price structures and loaders live in `src/strategy/common/`
  (`prices.py` `PriceBook`, `inputs.py` `load_daily_inputs`), which imports no strategy package.
- `roadmap.md` is the running log of what was done and why (current-state summary, open long-term
  items, a priority matrix, and a dated change history). Add a row there for non-trivial changes.
  Finished sections (2, 3, 6, 7 — the "roadmap 2-4" / "6-1a" / "7-2c" that code comments cite) and the
  2026-08~10 rows live in `docs/history/roadmap_2026-08_10.md`; superseded `docs/ui.md` sections (5,
  6.1-6.2, 7) in `docs/history/ui_archive_2026-09_10.md`, with a one-line pointer left at each number.
