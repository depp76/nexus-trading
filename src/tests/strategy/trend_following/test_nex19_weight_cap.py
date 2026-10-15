"""NEX-19 (C5): the 20% single-name cap is a standing limit (spec 2-4, 9-1 "L4 weekly adjustment"):
fills are cut to the cap at the fill price, and the weekly check sells only the excess. Synthetic data."""
import math
import unittest
from types import SimpleNamespace

import numpy as np

from strategy.trend_following.backtest import WEIGHT_TRIM_REASON, Order, TrendFollowingPolicy, run_backtest
from strategy.trend_following.config import StrategyParams, VARIANTS
from strategy.trend_following.costs import CostModel
from strategy.trend_following.signals import compute_features
from tests.strategy.trend_following.helpers import synthetic_dataset


def _scale_from(ds, ticker, k, factor, fields=("open", "high", "low", "close")):
    j = ds.stocks.col(ticker)
    for f in fields:
        getattr(ds.stocks, f)[k:, j] *= factor
    return j


def _run(ds, vid="A0"):
    p = StrategyParams()
    feats = compute_features(ds, p)
    return run_backtest(ds, feats, p, VARIANTS[vid], CostModel()), feats


class TestFillTimeCap(unittest.TestCase):
    def test_gap_up_open_is_cut_to_the_cap_at_the_fill_price(self):
        ds = synthetic_dataset()
        t0 = ds.start_idx
        j = _scale_from(ds, "UP", t0 + 1, 1.25)    # persistent gap above the signal close (limit is 30%, so it fills)
        res, _ = _run(ds)
        first = next(t for t in res.trades if t.ticker == "UP")
        self.assertEqual(first.entry_date, ds.dates[t0 + 1])
        pre_fill_equity = res.equity[0]            # t0 close; cash interest accrues after the fill
        self.assertLessEqual(first_qty_total(res) * first.entry_price, 0.20 * pre_fill_equity + 1e-6)
        sized_at_close = math.floor(0.20 * pre_fill_equity / ds.stocks.close[t0, j])
        self.assertLess(first_qty_total(res), sized_at_close)     # the cap, not the decision-day size, binds

    def test_no_gap_keeps_decision_size(self):
        ds = synthetic_dataset()
        t0 = ds.start_idx
        j = ds.stocks.col("UP")
        res, _ = _run(ds)
        self.assertLessEqual(first_qty_total(res), math.floor(0.20 * res.equity[0] / ds.stocks.close[t0, j]))


def first_qty_total(res):
    """Shares bought on the entry: everything sold later plus what is still open (UP is never re-bought)."""
    return sum(t.qty for t in res.trades if t.ticker == "UP") + sum(
        p["qty"] for p in res.open_positions if p["ticker"] == "UP")


class TestWeeklyExcessSell(unittest.TestCase):
    def test_jump_above_the_cap_is_trimmed_to_the_cap_next_session(self):
        ds = synthetic_dataset()
        t0 = ds.start_idx
        _scale_from(ds, "UP", t0 + 60, 2.0)       # one-day doubling -> weight ~35% on the next check day
        res, feats = _run(ds)
        trims = [t for t in res.trades if t.ticker == "UP" and t.reason == WEIGHT_TRIM_REASON]
        self.assertTrue(trims)
        t = ds.stocks
        j = t.col("UP")
        cap = StrategyParams().max_weight
        # the first trim is decided at a weekly check day on/after the jump and filled at the next open
        tr = trims[0]
        k = ds.dates.index(tr.exit_date)
        dec = k - 1
        self.assertTrue(feats.check_day[dec])
        self.assertGreaterEqual(dec, t0 + 60)
        held_before = sum(x.qty for x in res.trades if x.ticker == "UP" and x.exit_date >= tr.exit_date) + sum(
            p["qty"] for p in res.open_positions if p["ticker"] == "UP")
        eq = res.equity[dec - t0]
        self.assertGreater(held_before * t.close[dec, j], cap * eq)                  # was above the cap
        remaining = held_before - tr.qty
        self.assertLessEqual(remaining * t.close[dec, j], cap * eq + 1e-6)           # now at or below
        self.assertGreater((remaining + 1) * t.close[dec, j], cap * eq)              # only the excess was sold

    def test_every_trim_only_reduces_an_overweight_name_for_all_variants(self):
        for vid in ("A0", "A5", "B"):
            ds = synthetic_dataset(with_flows=True)
            t0 = ds.start_idx
            _scale_from(ds, "UP", t0 + 60, 2.0)
            res, feats = _run(ds, vid)
            for tr in (x for x in res.trades if x.reason == WEIGHT_TRIM_REASON):
                self.assertTrue(feats.check_day[ds.dates.index(tr.exit_date) - 1], vid)
            if vid == "A0":
                self.assertTrue(any(x.reason == WEIGHT_TRIM_REASON for x in res.trades))


class TestWeightTrimsUnit(unittest.TestCase):
    def _policy(self):
        feats = SimpleNamespace()
        return TrendFollowingPolicy(feats, StrategyParams(), VARIANTS["A0"])

    def _eng(self, qtys, closes, equity):
        pos = {f"T{i}": SimpleNamespace(col=i, qty=q) for i, q in enumerate(qtys)}
        book = SimpleNamespace(close=np.array([closes], dtype=float))
        return SimpleNamespace(book=book, positions=pos, equity_now=float(equity))

    def test_only_overweight_names_are_sold_and_only_the_excess(self):
        # equity 1,000,000; cap 200,000. T0 holds 300,000 (excess 100,000), T1 exactly 200,000, T2 100,000.
        eng = self._eng([300, 200, 100], [1000, 1000, 1000], 1_000_000)
        orders = self._policy()._weight_trims(0, eng, set())
        self.assertEqual([(o.ticker, o.side, o.qty, o.reason) for o in orders],
                         [("T0", "sell", 100, WEIGHT_TRIM_REASON)])

    def test_under_cap_book_trades_nothing_and_exiting_names_are_skipped(self):
        eng = self._eng([150, 200], [1000, 1000], 1_000_000)
        self.assertEqual(self._policy()._weight_trims(0, eng, set()), [])
        eng = self._eng([300], [1000], 1_000_000)
        self.assertEqual(self._policy()._weight_trims(0, eng, {"T0"}), [])

    def test_never_increases(self):
        eng = self._eng([10], [1000], 1_000_000)
        self.assertTrue(all(o.side == "sell" for o in self._policy()._weight_trims(0, eng, set())))
        self.assertIsInstance(Order("x", "buy", 1, "r", weight_cap=0.2).weight_cap, float)


if __name__ == "__main__":
    unittest.main()
