"""NEX-21 regressions: trim-day slot count (N1), rank_out of a trimmed name in B (N2), and no same-session close
in the fill-time weight cap (N3). Synthetic / fake objects, no network."""
import unittest
from types import SimpleNamespace

import numpy as np

from strategy.trend_following.backtest import WEIGHT_TRIM_REASON, Engine, TrendFollowingPolicy
from strategy.trend_following.config import VARIANTS, StrategyParams
from strategy.trend_following.costs import CostModel
from tests.strategy.trend_following.helpers import synthetic_dataset

N = 8


def _book():
    tickers = [f"T{i}" for i in range(N)]
    return SimpleNamespace(close=np.full((1, N), 1000.0), N=N, tickers=tickers,
                           _col={tk: i for i, tk in enumerate(tickers)}, col=lambda tk: int(tk[1:]))


def _feats(held_cols):
    ones = np.ones((1, N), dtype=bool)
    cand = ones.copy()
    cand[0, list(held_cols)] = False
    return SimpleNamespace(
        exit_channel=np.zeros((1, N), dtype=bool), flow_exit=np.zeros((1, N), dtype=bool),
        check_day=np.array([True]), regime_on=np.array([True]), trend_ok=ones, breakout=cand, liquid=ones,
        traded=ones, vol_ok=ones, flow_ok=ones, flow_strength=np.zeros((1, N)),
        rs=np.arange(N, dtype=float)[None, :], atr=np.full((1, N), 10.0),
        high52_prox=np.arange(N, dtype=float)[None, :])


def _eng(qtys, cols=None):
    cols = list(range(len(qtys))) if cols is None else cols
    pos = {f"T{i}": SimpleNamespace(col=i, qty=q, stop_price=None) for i, q in zip(cols, qtys)}
    return SimpleNamespace(book=_book(), positions=pos, pending=[], equity_now=1_000_000.0)


class TestTrimDaySlots(unittest.TestCase):
    def test_entries_fill_only_the_free_slots_when_a_name_is_trimmed(self):
        p = StrategyParams()
        eng = _eng([300, 100, 100])                 # T0 holds 30% of equity -> trimmed, still held
        orders = TrendFollowingPolicy(_feats({0, 1, 2}), p, VARIANTS["A0"]).decide(0, eng)
        self.assertEqual([o.ticker for o in orders if o.reason == WEIGHT_TRIM_REASON], ["T0"])
        self.assertEqual(len([o for o in orders if o.side == "buy"]), p.max_positions - 3)

    def test_same_buy_count_without_the_trim(self):
        p = StrategyParams()
        trimmed = TrendFollowingPolicy(_feats({0, 1, 2}), p, VARIANTS["A0"]).decide(0, _eng([300, 100, 100]))
        plain = TrendFollowingPolicy(_feats({0, 1, 2}), p, VARIANTS["A0"]).decide(0, _eng([150, 100, 100]))
        self.assertEqual(len([o for o in trimmed if o.side == "buy"]), len([o for o in plain if o.side == "buy"]))


class TestRankOutOfTrimmedName(unittest.TestCase):
    def test_b_sells_a_trimmed_name_whole_when_it_leaves_the_top(self):
        p = StrategyParams()
        eng = _eng([300, 100])                      # T0 (lowest proximity) is overweight and ranked out
        orders = TrendFollowingPolicy(_feats(set()), p, VARIANTS["B"]).decide(0, eng)
        t0 = [o for o in orders if o.ticker == "T0"]
        self.assertEqual([(o.side, o.qty, o.reason) for o in t0], [("sell", None, "rank_out")])

    def test_b_still_trims_a_name_that_stays_in_the_top(self):
        p = StrategyParams()
        eng = _eng([100, 300], cols=[1, 7])         # T7 has the highest proximity -> stays, trimmed
        orders = TrendFollowingPolicy(_feats(set()), p, VARIANTS["B"]).decide(0, eng)
        self.assertEqual([(o.side, o.reason) for o in orders if o.ticker == "T7"], [("sell", WEIGHT_TRIM_REASON)])


class TestFillCapIgnoresSameSessionClose(unittest.TestCase):
    def test_new_position_is_valued_at_the_fill_price_until_marked(self):
        ds = synthetic_dataset()
        t = ds.start_idx + 1
        eng = Engine(ds.stocks, ds.rf_period, StrategyParams(), CostModel(), ds.start_idx, "x")
        j = ds.stocks.col("UP")
        ds.stocks.close[t, j] = ds.stocks.close[t, j] * 3.0       # a large move into the session close
        price = float(ds.stocks.open[t, j])
        eng._execute_buy(t, "UP", 10, price, "entry", None)
        self.assertEqual(eng.positions["UP"].last_close, price)
        eng._mark(t)
        self.assertEqual(eng.positions["UP"].last_close, float(ds.stocks.close[t, j]))


if __name__ == "__main__":
    unittest.main()

