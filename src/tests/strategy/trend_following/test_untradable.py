"""NEX-31 (checklist A6): a fill that cannot happen at the next open -- open missing, buy at the
daily limit-up, sell at the daily limit-down -- is not booked that session and is retried on the
next one (spec 2-3 / 4-1 (3)). Zero-volume sessions are covered in test_nex16_fixes.py.
"""
import unittest

import numpy as np

from strategy.trend_following import VARIANTS, StrategyParams, compute_features
from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.costs import CostModel
from tests.strategy.trend_following.helpers import synthetic_dataset


def _run(ds, cm=None, vid="A0"):
    p = StrategyParams()
    return run_backtest(ds, compute_features(ds, p), p, VARIANTS[vid], cm or CostModel())


def _first(res, ticker, reason=None):
    return next(t for t in res.trades if t.ticker == ticker and (reason is None or t.reason == reason))


def _limit(ds, cm, t, j, which):
    """Price of the daily limit (0 = down, 1 = up) for session t from the previous close."""
    return cm.limit_prices(ds.dates[t], "KOSPI", float(ds.stocks.close[t - 1, j]))[which]


class TestEntryRetry(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel()
        base = synthetic_dataset()
        self.j = base.stocks.col("UP")
        self.t1 = base.start_idx + 1                        # UP's first fill session
        self.assertEqual(_first(_run(base), "UP").entry_date, base.dates[self.t1])

    def _entry(self, ds, cm=None):
        return _first(_run(ds, cm), "UP")

    def test_missing_open_defers_the_entry_one_session(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.t1, self.j] = np.nan
        tr = self._entry(ds)
        self.assertEqual(tr.entry_date, ds.dates[self.t1 + 1])
        self.assertAlmostEqual(tr.entry_price, self.cm.fill_price(tr.entry_date, "KOSPI", ds.stocks.open[self.t1 + 1, self.j], "buy"))

    def test_limit_up_open_defers_the_entry_one_session(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.t1, self.j] = _limit(ds, self.cm, self.t1, self.j, 1)
        tr = self._entry(ds)
        self.assertEqual(tr.entry_date, ds.dates[self.t1 + 1])
        self.assertAlmostEqual(tr.entry_price, self.cm.fill_price(tr.entry_date, "KOSPI", ds.stocks.open[self.t1 + 1, self.j], "buy"))

    def test_just_below_the_limit_still_fills(self):
        ds = synthetic_dataset()
        tick_below = _limit(ds, self.cm, self.t1, self.j, 1) - 1_000      # one tick-ladder step down (> 500k: 1,000)
        ds.stocks.open[self.t1, self.j] = tick_below
        self.assertFalse(self.cm.is_limit_up(ds.dates[self.t1], "KOSPI", tick_below, float(ds.stocks.close[self.t1 - 1, self.j])))
        self.assertEqual(self._entry(ds).entry_date, ds.dates[self.t1])

    def test_limit_up_fills_when_the_block_is_off(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.t1, self.j] = _limit(ds, self.cm, self.t1, self.j, 1)
        off = CostModel(block_limit_up_down=False)
        self.assertEqual(self._entry(ds, off).entry_date, ds.dates[self.t1])

    def test_retries_continue_until_the_open_is_tradable(self):
        p = StrategyParams()
        for blocked in range(1, p.max_order_retries + 1):
            ds = synthetic_dataset()
            for k in range(blocked):
                ds.stocks.open[self.t1 + k, self.j] = np.nan if k % 2 else _limit(ds, self.cm, self.t1 + k, self.j, 1)
            self.assertEqual(self._entry(ds).entry_date, ds.dates[self.t1 + blocked], blocked)


class TestExitRetry(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel()
        base = synthetic_dataset()
        self.ex = _first(_run(base), "SPIKE", "channel")
        self.k = base.dates.index(self.ex.exit_date)
        self.j = base.stocks.col("SPIKE")

    def _exit(self, ds, cm=None):
        return _first(_run(ds, cm), "SPIKE", "channel")

    def test_missing_open_defers_the_exit_one_session(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.k, self.j] = np.nan
        tr = self._exit(ds)
        self.assertEqual(tr.exit_date, ds.dates[self.k + 1])
        self.assertAlmostEqual(tr.exit_price, self.cm.fill_price(tr.exit_date, "KOSPI", ds.stocks.open[self.k + 1, self.j], "sell"))

    def test_limit_down_open_defers_the_exit_one_session(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.k, self.j] = _limit(ds, self.cm, self.k, self.j, 0)
        tr = self._exit(ds)
        self.assertEqual(tr.exit_date, ds.dates[self.k + 1])
        self.assertAlmostEqual(tr.exit_price, self.cm.fill_price(tr.exit_date, "KOSPI", ds.stocks.open[self.k + 1, self.j], "sell"))

    def test_limit_down_fills_when_the_block_is_off(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.k, self.j] = _limit(ds, self.cm, self.k, self.j, 0)
        self.assertEqual(self._exit(ds, CostModel(block_limit_up_down=False)).exit_date, ds.dates[self.k])

    def test_a_sell_is_retried_without_a_limit(self):
        ds = synthetic_dataset()
        n = StrategyParams().max_order_retries + 2                      # longer than a buy would be retried
        for i in range(n):
            ds.stocks.open[self.k + i, self.j] = np.nan if i % 2 else _limit(ds, self.cm, self.k + i, self.j, 0)
        self.assertEqual(self._exit(ds).exit_date, ds.dates[self.k + n])

    def test_a_deferred_sell_is_booked_once(self):
        ds = synthetic_dataset()
        ds.stocks.open[self.k, self.j] = np.nan
        res = _run(ds)
        self.assertEqual(len([t for t in res.trades if t.ticker == "SPIKE" and t.reason == "channel"]), 1)


class TestUntradableDoesNotLeakIntoOtherFills(unittest.TestCase):
    def test_other_names_fills_are_unchanged(self):
        base = _run(synthetic_dataset())
        ds = synthetic_dataset()
        ds.stocks.open[ds.start_idx + 1, ds.stocks.col("UP")] = np.nan
        alt = _run(ds)
        key = lambda r: [(t.ticker, t.entry_date, t.exit_date) for t in r.trades if t.ticker == "SPIKE"]
        self.assertEqual(key(base), key(alt))


if __name__ == "__main__":
    unittest.main()
