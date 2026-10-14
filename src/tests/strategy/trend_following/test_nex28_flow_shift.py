"""NEX-28 (checklist A2): flow-based entry/exit decisions never use the same-day (unconfirmed) flow."""
import unittest
from datetime import date

import numpy as np

from strategy.trend_following import VARIANTS, StrategyParams, compute_features
from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.costs import CostModel
from tests.strategy.trend_following.helpers import synthetic_dataset


def _flow_signals(ds):
    f = compute_features(ds, StrategyParams())
    return f.flow_ok, f.flow_exit, f.flow_strength


class TestFlowShift(unittest.TestCase):
    def test_signals_ignore_flow_of_day_t(self):
        ds = synthetic_dataset(with_flows=True)
        j = ds.stocks.col("UP")
        t = ds.start_idx + 100
        before = _flow_signals(ds)
        ds.flow_fi[t, j] = -1e12          # day-t flow changes only
        after = _flow_signals(ds)
        for b, a in zip(before, after):
            self.assertTrue(np.array_equal(b[:t + 1, j], a[:t + 1, j], equal_nan=True))
        ds.flow_fi[t, j] = np.nan
        self.assertFalse(np.array_equal(before[2][:, j], _flow_signals(ds)[2][:, j], equal_nan=True))

    def test_signal_equals_one_session_shifted_raw(self):
        ds = synthetic_dataset(with_flows=True)
        j = ds.stocks.col("UP")
        t = ds.start_idx + 100
        ok, _, _ = _flow_signals(ds)
        p = StrategyParams()
        fi = ds.flow_fi[:, j]
        self.assertEqual(bool(ok[t, j]), bool(fi[t - p.flow_long_window:t].sum() > 0 and fi[t - p.flow_short_window:t].sum() > 0))

    def test_ledger_entry_and_exit_use_prior_day_flow(self):
        ds = synthetic_dataset(with_flows=True)
        p = StrategyParams()
        feats = compute_features(ds, p)
        res = run_backtest(ds, feats, p, VARIANTS["A5"], CostModel())
        for tr in res.trades:
            t_fill = ds.dates.index(tr.entry_date)
            j = ds.stocks.col(tr.ticker)
            sig = t_fill - 1
            fi = ds.flow_fi[:, j]
            self.assertTrue(fi[sig - p.flow_long_window:sig].sum() > 0, tr)       # flows through sig-1 only
            self.assertTrue(feats.flow_ok[sig, j], tr)
            if tr.reason == "flow":
                x = ds.dates.index(tr.exit_date) - 1
                self.assertTrue(fi[x - p.flow_exit_window:x].sum() < 0, tr)
        self.assertIsInstance(date.today(), date)


if __name__ == "__main__":
    unittest.main()
