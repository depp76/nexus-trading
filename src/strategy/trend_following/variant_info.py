"""strategy/trend_following/variant_info.py — Plain-language description of each
6-2 matrix row (A0-A5, B, C1-C3) for the Strategy tab's variant picker.

Built from the `Variant` flags and the `StrategyParams` values rather than
written out per row, so a change to `config.py` changes the text with it.
The rules quoted are trend_following.md 2-1 (L1), 2-2 (L2), 2-3 (L3 + V/F),
2-4 (L4), 2-5 (C-series scoring), 3 (exits) and 6-2 (the matrix).
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.trend_following.config import VARIANTS, StrategyParams, Variant


@dataclass(frozen=True)
class VariantDescription:
    id: str
    title: str                                  # short list label, e.g. "Breakout + regime + volume"
    summary: str                                # what question this row answers in the matrix
    sections: tuple[tuple[str, tuple[str, ...]], ...]   # (heading, bullet lines)
    needs_flows: bool


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def _regime(p: StrategyParams) -> str:
    return (f"L1 market regime (weekly): new entries only while KOSPI closes above its {p.regime_ma}-day MA "
            f"and that MA is higher than {p.regime_slope_lookback} sessions ago; in risk-off weeks held names "
            f"are managed by the exit rules only.")


def _trend(p: StrategyParams) -> str:
    return (f"L2 stock trend (daily): close > MA{p.trend_ma_long}, MA{p.trend_ma_long} rising over "
            f"{p.trend_slope_lookback} sessions, and MA{p.trend_ma_short} > MA{p.trend_ma_long}.")


def _liquidity(p: StrategyParams) -> str:
    return f"Liquidity floor: 20-day average trading value of at least {p.min_avg_trading_value / 1e8:,.0f}억 KRW."


def _sizing(p: StrategyParams) -> str:
    return (f"Sizing: {_pct(p.risk_per_trade)} of equity at risk / ({p.atr_multiple:g} x ATR{p.atr_window}), "
            f"capped at {_pct(p.max_weight)} per name (also trimmed weekly), whole shares, "
            f"up to {p.max_positions} names.")


def _stop(p: StrategyParams) -> str:
    return f"Initial stop: close < entry - {p.stop_atr_multiple:g} x ATR{p.atr_window}."


def _title(v: Variant) -> str:
    if v.mode == "high52":
        return "52-week-high proximity holding"
    if v.mode == "score":
        return {"C1": "Trend + pullback score",
                "C2": "Score, raw (no ATR% scaling)",
                "C3": "Score, 52w-high proximity"}.get(v.id, "Trend + pullback score")
    if not v.regime:
        return "Breakout only"
    extras = [name for flag, name in ((v.volume, "volume"), (v.flow, "flows"), (v.flow_exit, "flow exit")) if flag]
    return "Breakout + regime" + (" + " + " + ".join(extras) if extras else "")


def _summary(v: Variant) -> str:
    return {
        "A0": "Baseline of the A-series: the Donchian breakout with no market filter, so every other row is "
              "measured against it.",
        "A1": "Does the weekly KOSPI regime filter help? Same as A0 plus L1.",
        "A2": "Does a volume surge on the breakout day help? A1 plus the V filter.",
        "A3": "Does foreign + institution buying help? A1 plus the F filter.",
        "A4": "Volume and flow confirmation together: A1 plus V and F.",
        "A5": "The full A-series: A4 plus the flow-based exit FX.",
        "B": "Instead of buying breakouts, hold the names closest to their 52-week high.",
        "C1": "The 2-5 scoring mode: buy names in a strong trend that have pulled back over the last few days.",
        "C2": "C1 without dividing the short-term indicators by ATR%, to see if volatility normalisation matters.",
        "C3": "C1 with the trend position measured as close / 52-week high instead of Range52.",
    }.get(v.id, v.label)


def describe_variant(vid: str, params: StrategyParams | None = None) -> VariantDescription:
    """The description of matrix row `vid` with the numbers of `params`
    (the StrategyParams defaults when None)."""
    v = VARIANTS[vid]
    p = params or StrategyParams()
    if v.mode == "score":
        p = v.apply(p)
    sections: list[tuple[str, tuple[str, ...]]] = []

    universe = [_liquidity(p)]
    if v.regime:
        universe.append(_regime(p))
    sections.append(("Market and universe", tuple(universe)))

    if v.mode == "breakout":
        entry = [_trend(p),
                 f"L3 trigger (daily): close above the highest high of the prior {p.breakout_window} sessions "
                 f"(Donchian {p.breakout_window}); filled at the next session's open."]
        if v.volume:
            entry.append(f"V: breakout-day volume >= {p.volume_ratio_min:g} x its {p.volume_window}-day average.")
        if v.flow:
            entry.append(f"F: foreign + institution net buying over the last {p.flow_long_window} and "
                         f"{p.flow_short_window} sessions both > 0.")
        entry.append(f"L4 ranking when candidates exceed open slots: rank of 20-day flow strength + rank of "
                     f"{p.rs_window}-day return relative to KOSPI (relative strength only without flow data).")
        entry.append(_sizing(p))
        sections.append(("Entry", tuple(entry)))
        exits = [f"Trend break: close < the lowest low of the prior {p.exit_window} sessions (Donchian {p.exit_window}).",
                 _stop(p)]
        if v.flow_exit:
            exits.append(f"FX: foreign + institution net selling over {p.flow_exit_window} sessions "
                         f"and close < MA{p.trend_ma_short}.")
        sections.append(("Exit (checked daily, next-open fill)", tuple(exits)))
    elif v.mode == "high52":
        sections.append(("Holding rule (weekly check day)", (
            _trend(p),
            f"Rank the names passing L2 and the liquidity floor by close / {p.high_52w_window}-day high and hold "
            f"the top {p.max_positions} at equal weight (<= {_pct(p.max_weight)} each).",
            "Names that drop out of the top ranks are sold at the next open.",
        )))
        sections.append(("Exit (checked daily, next-open fill)", (
            f"Trend break: close < the lowest low of the prior {p.exit_window} sessions (Donchian {p.exit_window}).",
            _stop(p),
        )))
    else:
        range_ind = (f"close / {p.score_range_window}-day high" if p.score_range_mode == "high52_prox"
                     else f"Range52 = position of the close in its {p.score_range_window}-day low-high range")
        norm = ("R3 and MA20Div are divided by ATR20 / close (volatility normalised)." if p.score_vol_normalize
                else "R3 and MA20Div are used as raw returns (no volatility normalisation).")
        sections.append(("Gate (all must pass)", (
            f"Close above MA{p.score_ma_long} (MA50Div > 0).",
            f"Trend position >= {p.gate_range_min:g}, using {range_ind}.",
            f"Not overheated: MA20Div below the {p.gate_overheat_pct * 100:g}th percentile of liquid names.",
        )))
        sections.append(("Score", (
            f"Trend score = pct(MA50Div) + pct(trend position); timing score = -pct(R{p.score_r_short}), "
            f"so a recent dip scores higher. Weights 1 : 1 : 1.",
            norm,
        )))
        sections.append(("Buy (weekly check day, next-open fill)", (
            f"Risk-on week, gate passed and total-score percentile >= {p.score_buy_pct:g}; "
            f"best scores first into the open slots.",
            _sizing(p),
        )))
        sections.append(("Sell", (
            f"Weekly: score percentile < {p.score_sell_pct:g} (a lower bar than the buy, to avoid churning).",
            f"Daily: close below MA{p.score_ma_long}, or the initial stop "
            f"(close < entry - {p.stop_atr_multiple:g} x ATR{p.atr_window}).",
            "No Donchian 10 exit, and an overheated holding is kept (overheat only blocks new entries).",
        )))

    data = ["Needs investor-flow data (tick Investor flows); skipped without it."] if v.needs_flows else \
        ["Price data only; runs without investor flows."]
    sections.append(("Data", tuple(data)))
    return VariantDescription(v.id, _title(v), _summary(v), tuple(sections), v.needs_flows)
