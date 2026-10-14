"""NEX-28 / NEX-30 (checklist A2): day-t flow is usable after the close of t, fills are at t+1 open or later.

Spec 5 convention is kept (no one-session shift); these tests pin the fill timing and cover the
validation-only contamination option `compute_features(..., flow_lead=n)`.
"""
import unittest

import numpy as np

from strategy.trend_following import VARIANTS, StrategyParams, compute_features
from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.costs import CostModel
from strategy.trend_following.signals import lead
from tests.strategy.trend_following.helpers import synthetic_dataset


def _flow_signals(ds, **kw):
    f = compute_features(ds, StrategyParams(), **kw)
    return f.flow_ok, f.flow_exit, f.flow_strength


class TestFlowFillTiming(unittest.TestCase):
    def test_flow_decisions_fill_at_t_plus_1_or_later(self):
        ds = synthetic_dataset(with_flows=True)
        p = StrategyParams()
        feats = compute_features(ds, p)
        res = run_backtest(ds, feats, p, VARIANTS["A5"], CostModel())
        self.assertGreater(len(res.trades), 0)
        for tr in res.trades:
            j = ds.stocks.col(tr.ticker)
            fill = ds.dates.index(tr.entry_date)
            self.assertTrue(feats.flow_ok[fill - 1, j], tr)       # decided at the close of fill-1 on its flows
            if tr.reason == "flow":
                x = ds.dates.index(tr.exit_date)
                self.assertTrue(feats.flow_exit[x - 1, j], tr)

    def test_flow_after_t_never_changes_fills_through_t(self):
        ds = synthetic_dataset(with_flows=True)
        p = StrategyParams()
        t = ds.start_idx + 150
        base = run_backtest(ds, compute_features(ds, p), p, VARIANTS["A5"], CostModel())
        ds.flow_fi[t + 1:, :] = -1e12
        alt = run_backtest(ds, compute_features(ds, p), p, VARIANTS["A5"], CostModel())
        cut = ds.dates[t]
        early = lambda r: [(x.ticker, x.entry_date) for x in r.trades if x.entry_date <= cut]
        self.assertEqual(early(base), early(alt))


class TestFlowLeadOption(unittest.TestCase):
    def test_default_is_off(self):
        ds = synthetic_dataset(with_flows=True)
        for a, b in zip(_flow_signals(ds), _flow_signals(ds, flow_lead=0)):
            self.assertTrue(np.array_equal(a, b, equal_nan=True))

    def test_lead_one_sees_next_day_flow(self):
        ds = synthetic_dataset(with_flows=True)
        j = ds.stocks.col("UP")
        t = ds.start_idx + 100
        base = _flow_signals(ds)
        lead1 = _flow_signals(ds, flow_lead=1)
        # contaminated signal at t equals the clean one at t+1 only through the raw flow it reads
        ds.flow_fi[t + 1, j] = -1e12
        changed = _flow_signals(ds, flow_lead=1)
        self.assertFalse(np.array_equal(lead1[2][t, j], changed[2][t, j], equal_nan=True))
        self.assertTrue(np.array_equal(base[2][t, j], _flow_signals(ds)[2][t, j], equal_nan=True))

    def test_lead_helper(self):
        a = np.arange(5.0).reshape(5, 1)
        out = lead(a, 2)
        self.assertEqual(out[:3, 0].tolist(), [2.0, 3.0, 4.0])
        self.assertTrue(np.isnan(out[3:, 0]).all())


if __name__ == "__main__":
    unittest.main()
