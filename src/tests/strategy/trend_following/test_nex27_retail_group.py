"""NEX-27: 6-1 retail-only group uses the measured flow_retail (FI <= 0 & retail > 0)."""
import unittest

import numpy as np

from strategy.trend_following.config import StrategyParams
from strategy.trend_following.event_study import RETAIL_GROUP, run_event_study
from strategy.trend_following.research import ResearchRequest, run_research
from strategy.trend_following.signals import compute_features
from tests.strategy.trend_following.helpers import synthetic_dataset
from datetime import date


def _group_n(fi, retail, mutate=None):
    """Event count of the retail group for ticker UP with constant daily FI / retail amounts."""
    ds = synthetic_dataset(with_flows=True)
    j = ds.stocks.col("UP")
    ds.flow_fi[:, j] = fi
    ds.flow_retail[:, j] = retail
    if mutate:
        mutate(ds, j)
    p = StrategyParams()
    rows = {r["group"]: r for r in run_event_study(ds, compute_features(ds, p), p)}
    return rows[RETAIL_GROUP]["n"]


class TestRetailGroup(unittest.TestCase):
    def test_fi_sell_and_retail_sell_excluded(self):
        # FI < 0 but retail < 0 (the old proxy would have counted it)
        self.assertEqual(_group_n(-1.0, -1.0), 0)

    def test_fi_nonpositive_and_retail_buy_included(self):
        self.assertGreater(_group_n(-1.0, 1.0), 100)
        self.assertGreater(_group_n(0.0, 1.0), 100)

    def test_fi_buy_excluded(self):
        self.assertEqual(_group_n(1.0, 1.0), 0)

    def test_missing_retail_excluded_not_zero_filled(self):
        def blank(ds, j):
            ds.flow_retail[:, j] = np.nan
        self.assertEqual(_group_n(-1.0, 1.0, blank), 0)

    def test_event_day_uses_flows_through_t_only(self):
        # Flows after the event day t (known only later) never change t's classification;
        # the fill is t+1 open, as for the signals (spec 5: t-day flows -> t+1 open).
        base = _group_n(-1.0, 1.0)

        def late(ds, j):
            ds.flow_retail[ds.start_idx + 200:, j] = -1e9
        changed = _group_n(-1.0, 1.0, late)
        self.assertLess(changed, base)
        ds = synthetic_dataset(with_flows=True)
        j = ds.stocks.col("UP")
        ds.flow_fi[:, j] = -1.0
        ds.flow_retail[:, j] = 1.0
        p = StrategyParams()
        t = ds.start_idx + 100
        before = compute_features(ds, p)
        ds.flow_retail[t + 1:, j] = -1.0
        after = compute_features(ds, p)
        self.assertTrue(np.array_equal(before.breakout[:t + 1], after.breakout[:t + 1]))
        from strategy.trend_following.signals import rolling
        r5 = rolling(ds.flow_retail, StrategyParams().flow_short_window, "sum")
        self.assertGreater(r5[t, j], 0)

    def test_no_proxy_group_name_and_no_proxy_warning(self):
        ds = synthetic_dataset(with_flows=True)
        p = StrategyParams()
        names = [r["group"] for r in run_event_study(ds, compute_features(ds, p), p)]
        self.assertIn(RETAIL_GROUP, names)
        self.assertFalse([n for n in names if "proxy" in n or "FI < 0" in n])
        res = run_research(ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",),
                                               cost_multipliers=(1.0,), include_flows=True,
                                               run_backtests=False))
        text = res.to_markdown()
        for bad in ("역산", "개인 컬럼 없음", "retail = -(FI)", "proxy"):
            self.assertNotIn(bad, text)
        self.assertIn("sub-investors", text)


if __name__ == "__main__":
    unittest.main()