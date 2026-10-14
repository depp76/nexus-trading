"""strategy/trend_following/scoring_policy.py — C-series (C1~C3) backtest
policy: trend_following.md 2-5 (4)(5) on the shared engine (9-16).

The indicators, gate, scores and ranks all come from ``scoring.compute_scores``
(nothing is recomputed here); this module only turns them into orders:

  buy   weekly check day, L1 Risk-on, gate passed, total rank >= score_buy_pct;
        as many names as there are free slots, highest total first; size from
        2-4 (1% risk / (2 x ATR20), 20% cap, whole shares), filled at t+1 open
  sell  relative, weekly check day: gate passed and total rank < score_sell_pct
        absolute, every session: MA50Div < 0, or close < entry - 2 x ATR20
  not   Donchian-10 exit (``exit_channel``) is never read; an overheated holding
        is never sold for being overheated

A holding that fails the gate for a reason other than MA50Div < 0 (Range52 below
the floor, overheated, no longer liquid) has no total rank, so neither weekly
rule applies to it: it stays until MA50Div < 0 or the stop (9-16 decision 1).
"""
from __future__ import annotations

import numpy as np

from strategy.trend_following.backtest import Engine, Order, TrendFollowingPolicy
from strategy.trend_following.config import StrategyParams, Variant
from strategy.trend_following.scoring import ScoreBook
from strategy.trend_following.signals import Features

REASON_MA50 = "ma50_exit"        # absolute sell: close below the 50-day average (2-5 (5) absolute 1)
REASON_STOP = "stop"             # absolute sell: initial ATR stop (2-5 (5) absolute 2, same as 3-2)
REASON_RANK = "score_rank_out"   # relative sell: gated but total rank below score_sell_pct


class ScoringPolicy(TrendFollowingPolicy):
    """Reuses the A-series sizing (`_size`) and the standing 20% weight trim; only the decisions differ."""

    def __init__(self, feats: Features, params: StrategyParams, variant: Variant, scores: ScoreBook):
        super().__init__(feats, params, variant)
        if scores.T != len(feats.check_day):
            raise ValueError("score book and features cover different sessions")
        self.sb = scores

    def decide(self, t: int, eng: Engine) -> list[Order]:
        f, p, sb, b = self.f, self.p, self.sb, eng.book
        orders: list[Order] = []
        weekly = bool(f.check_day[t])

        exiting = {o.ticker for o in eng.pending if o.side == "sell"}
        for tk, pos in eng.positions.items():
            if tk in exiting:
                continue
            j = pos.col
            c = b.close[t, j]
            if not np.isfinite(c):
                continue
            reason = None
            if sb.ma50_div[t, j] < 0:
                reason = REASON_MA50
            elif pos.stop_price is not None and c < pos.stop_price:
                reason = REASON_STOP
            elif weekly and sb.gate[t, j] and sb.total_rank[t, j] < p.score_sell_pct:
                reason = REASON_RANK
            if reason:
                orders.append(Order(tk, "sell", None, reason))
                exiting.add(tk)

        if weekly:
            orders.extend(self._weight_trims(t, eng, exiting))     # names already being sold are skipped there

        if not weekly or not f.regime_on[t]:
            return orders                       # buys only on a Risk-on check day

        pending_buys = {o.ticker for o in eng.pending if o.side == "buy"}
        staying = len([tk for tk in eng.positions if tk not in exiting])
        slots = p.max_positions - staying - len(pending_buys)
        if slots <= 0:
            return orders

        held = np.zeros(b.N, dtype=bool)
        for pos in eng.positions.values():
            held[pos.col] = True
        for tk in pending_buys:
            if tk in b._col:
                held[b.col(tk)] = True
        rank = sb.total_rank[t]
        with np.errstate(invalid="ignore"):
            mask = sb.gate[t] & (rank >= p.score_buy_pct) & f.traded[t] & ~held
        cands = np.flatnonzero(mask)
        if cands.size == 0:
            return orders
        cands = cands[np.argsort(-sb.total[t, cands], kind="stable")][:slots]
        equity = eng.equity_now
        for j in cands:
            qty = self._size(t, j, equity, b.close[t, j])
            if qty > 0:
                orders.append(Order(b.tickers[j], "buy", qty, "entry", stop_offset=p.stop_atr_multiple * f.atr[t, j],
                                    weight_cap=p.max_weight))
        return orders
