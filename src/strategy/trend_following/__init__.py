"""strategy.trend_following — Trend Following strategy (spec: trend_following.md
in this folder, currently v06).

Facade: UI and thread code import from here; the modules underneath are
  config      parameters, the 6-2 variant matrix, cost scenarios
  scoring     2-5 "trend + pullback" indicators, gate, scores and the weekly Top/Bottom list
  costs       4-1 cost model (tax table, tick sizes, commission, slippage)
  signals     layer signals L1..L4 and the exit conditions as date x ticker arrays
  dataset     aligned price / flow / rate arrays (build_dataset is pure, load_dataset fetches)
  backtest    portfolio engine (t+1 fills, interest, annual reset, ledger) + the strategy policy
  benchmarks  BM1..BM4 on the same engine
  metrics     6-4 scorecard
  event_study 6-1 breakout event study
  factor_ic   6-1-2 factor Rank IC, quintiles and the verdict table
  research    the matrix runner and the markdown report
  robustness  E1 weekdays, E2 pre-registered neighbours, E4, E5 and the checklist-form report (not imported here)
  snapshot    save / load a Dataset with a SHA-256 manifest (cache/snapshots/)
  run         headless entry point: python -m strategy.trend_following.run (not imported here)
"""
from strategy.trend_following.config import (
    SPEC_VERSION, SPEC_FILE, StrategyParams, Variant, VARIANTS, COST_MULTIPLIERS, PERIODS,
    HOLDOUT_PERIOD, FORWARD_PERIOD, IC_VERDICT_HORIZON,
    BM_TR_TICKER, BM_PRICE_FALLBACK_TICKER, DEV_START, DEV_END,
)
from strategy.trend_following.costs import CostModel, TradeCost, DEFAULT_TAX_TABLE, tick_size, snap_to_tick
from strategy.trend_following.signals import Features, compute_features, regime_state, weekly_check_days
from strategy.trend_following.dataset import Dataset, PriceBook, ResearchCancelled, HoldoutGuardError, build_dataset, load_dataset
from strategy.trend_following.snapshot import save_snapshot, load_snapshot, dataset_sha256
from strategy.trend_following.backtest import (
    BacktestResult, Trade, Engine, Policy, TrendFollowingPolicy, run_backtest,
)
from strategy.trend_following.benchmarks import run_benchmarks, run_bm1, run_bm2, run_bm3, run_bm4
from strategy.trend_following.metrics import summarize_run
from strategy.trend_following.event_study import run_event_study
from strategy.trend_following.factor_ic import FactorICResult, run_factor_ic
from strategy.trend_following.research import (
    ResearchRequest, ResearchResult, RunSummary, run_research, render_markdown,
)
from strategy.trend_following.scoring import (
    ScoreBook, ScoredName, Recommendation, compute_scores, weekly_recommendation, load_score_inputs,
    run_universe_scoring,
)

__all__ = [
    "SPEC_VERSION", "SPEC_FILE", "StrategyParams", "Variant", "VARIANTS", "COST_MULTIPLIERS", "PERIODS",
    "HOLDOUT_PERIOD", "FORWARD_PERIOD", "IC_VERDICT_HORIZON",
    "BM_TR_TICKER", "BM_PRICE_FALLBACK_TICKER",
    "CostModel", "TradeCost", "DEFAULT_TAX_TABLE", "tick_size", "snap_to_tick",
    "Features", "compute_features", "regime_state", "weekly_check_days",
    "Dataset", "PriceBook", "ResearchCancelled", "HoldoutGuardError", "build_dataset", "load_dataset",
    "save_snapshot", "load_snapshot", "dataset_sha256",
    "BacktestResult", "Trade", "Engine", "Policy", "TrendFollowingPolicy", "run_backtest",
    "run_benchmarks", "run_bm1", "run_bm2", "run_bm3", "run_bm4",
    "summarize_run", "run_event_study", "FactorICResult", "run_factor_ic",
    "ResearchRequest", "ResearchResult", "RunSummary", "run_research", "render_markdown",
    "ScoreBook", "ScoredName", "Recommendation", "compute_scores", "weekly_recommendation", "load_score_inputs",
    "run_universe_scoring",
]
