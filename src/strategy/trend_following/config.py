"""strategy/trend_following/config.py — Parameters of the Trend Following
strategy (trend_following.md v06, sections 2-4, 2-5 and 6-2).

Everything a backtest can vary lives here as a frozen dataclass so a run can
be reproduced from its parameter snapshot. Defaults are the spec's proposed
values; the spec itself says they are "구상안" until the backtests confirm
them, so nothing here is tuned.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from datetime import date

SPEC_VERSION = "v06"
SPEC_FILE = "trend_following.md"

# Development window guard (NEX-11 decision, docs/nexus/README.md 5-1): data and
# evaluation dates after DEV_END are the 2025 holdout and are blocked unless a
# caller passes the explicit `allow_holdout` flag. PERIODS holds the two
# development sub-periods only; the holdout and forward years are separate
# constants (spec 6-4, 6-5).
DEV_START = date(2021, 1, 1)
DEV_END = date(2024, 12, 31)


@dataclass(frozen=True)
class StrategyParams:
    # 2-0 universe
    universe_size: int = 200                 # KOSPI top-N by market cap (current constituents; see 5)
    min_avg_trading_value: float = 1e10      # 20-day average trading value floor (KRW, 100억)
    # 2-1 L1 market regime (weekly check)
    regime_ma: int = 60
    regime_slope_lookback: int = 5
    check_weekday: int = 0                   # 0=Mon .. 4=Fri; the weekly check day (6-4 robustness)
    # 2-2 L2 stock trend (daily)
    trend_ma_short: int = 20
    trend_ma_long: int = 60
    trend_slope_lookback: int = 5
    # 2-3 L3 entry trigger (daily, next-open fill)
    breakout_window: int = 20                # Donchian 20: close > max(high) of the prior 20 sessions
    volume_window: int = 20
    volume_ratio_min: float = 1.5            # V: breakout-day volume >= 1.5 x 20-day average
    flow_long_window: int = 20               # F: 20-day foreign+institution net buy > 0
    flow_short_window: int = 5               # F: 5-day foreign+institution net buy > 0
    # 2-4 L4 ranking and sizing
    max_positions: int = 6                   # 5~8 per the spec
    risk_per_trade: float = 0.01             # position risk = 1% of equity / (2 x ATR20)
    atr_window: int = 20
    atr_multiple: float = 2.0
    max_weight: float = 0.20                 # single-name cap
    rs_window: int = 60                      # relative strength = stock 60d return - KOSPI 60d return
    high_52w_window: int = 252               # variant B: 52-week-high proximity
    # 3 exits (daily, next-open fill)
    exit_window: int = 10                    # Donchian 10: close < min(low) of the prior 10 sessions
    stop_atr_multiple: float = 2.0           # close < entry - 2 x ATR20 (ATR at entry)
    flow_exit_window: int = 10               # FX: 10-day foreign+institution net sell and close < MA20
    # 4 operating assumptions
    seed_krw: float = 10_000_000.0
    annual_reset: bool = True                # restart every year at seed; excess trimmed pro-rata at year end
    max_order_retries: int = 3               # buy orders blocked by limit-up / missing open are retried this often
    delist_after_missing: int = 10           # a held name with no price for N sessions is closed at its last close
    # 6-3 benchmarks
    bm_ma: int = 200                         # BM3: hold the KOSPI 200 ETF when close > MA200
    risk_free_fallback: float = 0.03         # annual CD91 stand-in when no rate series is available
    # 2-5 scoring mode "trend + pullback" (C-series, v04; scoring.py)
    score_ma_long: int = 50                  # MA50Div = close / MA50 - 1 (trend direction, +)
    score_ma_short: int = 20                 # MA20Div = close / MA20 - 1 (overheat gate)
    score_r_short: int = 3                   # R3 = close / close[t-3] - 1 (entry timing, -)
    score_range_window: int = 252            # Range52 = (close - low252) / (high252 - low252)
    gate_range_min: float = 0.70             # gate: Range52 >= 0.70 (top 30% of the 52-week range)
    gate_overheat_pct: float = 0.95          # gate: MA20Div percentile above this -> no new entry
    score_buy_pct: float = 0.80              # buy: total-score percentile (within gated names) >= 0.80
    score_sell_pct: float = 0.50             # weekly relative sell: percentile < 0.50 (hysteresis)
    score_vol_normalize: bool = True         # divide R3, MA20Div by ATR20 / close
    score_range_mode: str = "range"          # "range" (Range52) | "high52_prox" (close / high252, C3)
    score_week_sessions: int = 5             # Universe recommendation: sessions averaged ("last week")
    score_week_min_sessions: int = 3         # ... and how many of them a name must be scored on

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Variant:
    """One row of the 6-2 comparison matrix."""
    id: str
    regime: bool          # L1 market regime gate
    volume: bool          # V: volume confirmation on entry
    flow: bool            # F: foreign+institution net-buy confirmation on entry
    flow_exit: bool       # FX: flow-based exit
    mode: str = "breakout"   # "breakout" (A-series), "high52" (B: hold top names by 52-week-high proximity)
                             # or "score" (C-series: 2-5 trend + pullback scoring)
    score_overrides: tuple[tuple[str, object], ...] = ()   # C-series: StrategyParams fields this row changes

    @property
    def needs_flows(self) -> bool:
        return self.flow or self.flow_exit

    def apply(self, params: StrategyParams) -> StrategyParams:
        """`params` with this row's overrides (C2: no volatility normalisation, C3: high52_prox)."""
        return replace(params, **dict(self.score_overrides)) if self.score_overrides else params

    @property
    def label(self) -> str:
        if self.mode == "high52":
            return "B: L1 + 52w-high proximity ranking"
        if self.mode == "score":
            return f"{self.id}: {SCORE_LABELS.get(self.id, 'L1 + trend/pullback score')}"
        parts = ["L1" if self.regime else "-", "V" if self.volume else "-",
                 "F" if self.flow else "-", "FX" if self.flow_exit else "-"]
        return f"{self.id}: " + "/".join(parts)


SCORE_LABELS = {
    "C1": "L1 + trend/pullback score (ATR-normalised, Range52)",
    "C2": "C1 without volatility normalisation",
    "C3": "C1 with Range52 -> 52w-high proximity",
}

VARIANTS: dict[str, Variant] = {
    "A0": Variant("A0", regime=False, volume=False, flow=False, flow_exit=False),
    "A1": Variant("A1", regime=True, volume=False, flow=False, flow_exit=False),
    "A2": Variant("A2", regime=True, volume=True, flow=False, flow_exit=False),
    "A3": Variant("A3", regime=True, volume=False, flow=True, flow_exit=False),
    "A4": Variant("A4", regime=True, volume=True, flow=True, flow_exit=False),
    "A5": Variant("A5", regime=True, volume=True, flow=True, flow_exit=True),
    "B": Variant("B", regime=True, volume=False, flow=False, flow_exit=False, mode="high52"),
    # 2-5 / 6-2: C-series, price data only (no V / F / FX); the rows differ only in the scoring inputs
    "C1": Variant("C1", regime=True, volume=False, flow=False, flow_exit=False, mode="score"),
    "C2": Variant("C2", regime=True, volume=False, flow=False, flow_exit=False, mode="score",
                  score_overrides=(("score_vol_normalize", False),)),
    "C3": Variant("C3", regime=True, volume=False, flow=False, flow_exit=False, mode="score",
                  score_overrides=(("score_range_mode", "high52_prox"),)),
}

# 4-1 mandatory sensitivity scenarios: no cost / base / conservative.
COST_MULTIPLIERS: tuple[float, ...] = (0.0, 1.0, 2.0)

# 6-4 / 6-1-2 development sub-periods: (label, first year, last year). Two periods
# only; 2025 is the holdout and 2026 forward observation (v05).
PERIODS: tuple[tuple[str, int, int], ...] = (("2021-22", 2021, 2022), ("2023-24", 2023, 2024))
# 6-5: evaluated once, by Validation, after spec and parameters are frozen.
HOLDOUT_PERIOD: tuple[str, int, int] = ("2025", 2025, 2025)
# Observation only; never used for a verdict.
FORWARD_PERIOD: tuple[str, int, int] = ("2026", 2026, 2026)
# 6-1-2: the Rank IC horizon (sessions) whose per-period sign decides the verdict, fixed before any result.
IC_VERDICT_HORIZON = 20

# 6-3 benchmark data. KODEX 200TR (278530) reinvests dividends, so its price is
# a total-return series with the fund's expense ratio already embedded (no
# separate daily fee deduction, 4-1 (4)); KODEX 200 (069500) is the price-only
# fallback and is flagged in the report because 6-3 forbids price indices.
BM_TR_TICKER = "278530"
BM_PRICE_FALLBACK_TICKER = "069500"
BM_ETF_SLIPPAGE = 0.0002
