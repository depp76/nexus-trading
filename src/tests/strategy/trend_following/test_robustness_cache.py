"""review_agy.md 2026-11-20 M11 (trend_following.md 10-15): the robustness runner shares
one feature bundle and one score-book cache across the base matrix, E1, the score-only
E2 neighbours and the cash-interest-0 reruns, and factor_ic ranks each factor once for
its correlation matrices. Results must be exactly those of the former separate
computations: every run here is recomputed the old way (fresh compute_features /
compute_scores per run) and compared with ==, not approximately."""
import unittest
from dataclasses import fields, replace
from datetime import date

import numpy as np

from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.config import StrategyParams
from strategy.trend_following.factor_ic import FACTORS, row_rank_corr, run_factor_ic
from strategy.trend_following.metrics import summarize_run
from strategy.trend_following.research import ResearchRequest
from strategy.trend_following.robustness import (
    SCORE_ONLY_PREFIXES, RobustnessRequest, neighbor_params, period_stats, run_robustness, stats_from_metrics,
    variant_for_neighbor,
)
from strategy.trend_following.scoring import compute_scores
from strategy.trend_following.signals import compute_features, with_check_weekday
from tests.strategy.trend_following.helpers import synthetic_dataset

START = date(2021, 1, 4)
VIDS = ("A0", "A1", "A5", "B", "C1", "C2", "C3")


def _assert_features_equal(tc, a, b):
    for f in fields(a):
        x, y = getattr(a, f.name), getattr(b, f.name)
        if isinstance(x, np.ndarray):
            np.testing.assert_array_equal(x, y, err_msg=f.name)
        else:
            tc.assertEqual(x, y, f.name)


def _old_run(ds, params, vid, cm, free_field=None, cash_interest=True):
    """One backtest computed the pre-2026-11-20 way: its own features and score book."""
    v = variant_for_neighbor(vid, free_field)
    feats = compute_features(ds, params)
    scores = compute_scores(ds.stocks, v.apply(params)) if v.mode == "score" else None
    return run_backtest(ds, feats, params, v, cm, scores=scores, cash_interest=cash_interest)


class TestFeatureSharing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def test_weekday_rebuild_equals_a_fresh_computation(self):
        p = StrategyParams()
        base = compute_features(self.ds, p)
        for w in range(5):
            pw = replace(p, check_weekday=w)
            _assert_features_equal(self, with_check_weekday(base, self.ds, pw), compute_features(self.ds, pw))

    def test_features_ignore_every_score_only_field(self):
        p = StrategyParams()
        base = compute_features(self.ds, p)
        moved = {}
        for f in fields(StrategyParams):
            if not f.name.startswith(SCORE_ONLY_PREFIXES):
                continue
            v = getattr(p, f.name)
            moved[f.name] = (not v if isinstance(v, bool) else v * 2 + 1 if isinstance(v, (int, float))
                             else "high52_prox" if f.name == "score_range_mode" else v)
        self.assertIn("score_ma_long", moved)
        _assert_features_equal(self, compute_features(self.ds, replace(p, **moved)), base)


class TestRobustnessUnchanged(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)
        cls.req = RobustnessRequest(
            ResearchRequest(start=START, variant_ids=VIDS, cost_multipliers=(1.0,), run_event_study=False,
                            include_flows=True), cash_interest_reference=True)
        cls.res = run_robustness(cls.ds, cls.req)
        cls.p = cls.req.research.params
        cls.cm1 = cls.req.research.cost_model.scaled(1.0)

    def _stats(self, res):
        return stats_from_metrics(summarize_run(res)), period_stats(res.nav, res.dates, res.rf_period)

    def test_base_matrix(self):
        for run in self.res.research.runs_at(1.0, "strategy"):
            old = _old_run(self.ds, self.p, run.id, self.cm1)
            np.testing.assert_array_equal(run.nav, old.nav, err_msg=run.id)

    def test_e1_weekday_runs(self):
        checked = 0
        for r in self.res.weekday_runs:
            if r.weekday == self.p.check_weekday:
                continue
            whole, periods = self._stats(_old_run(self.ds, replace(self.p, check_weekday=r.weekday), r.variant,
                                                  self.cm1))
            self.assertEqual((r.whole, r.periods), (whole, periods), (r.variant, r.weekday))
            checked += 1
        self.assertEqual(checked, 4 * len(VIDS))

    def test_e2_neighbour_runs(self):
        self.assertTrue({"N6", "N7"} <= {r.nid for r in self.res.neighbor_runs})
        for r in self.res.neighbor_runs:
            pn = neighbor_params(self.p, r.field, r.value)
            whole, periods = self._stats(_old_run(self.ds, pn, r.variant, self.cm1, free_field=r.field))
            self.assertEqual((r.whole, r.periods), (whole, periods), (r.nid, r.variant))

    def test_cash_interest_zero_runs(self):
        self.assertEqual(len(self.res.no_interest), len(VIDS))
        for r in self.res.no_interest:
            m = summarize_run(_old_run(self.ds, self.p, r.variant, self.cm1, cash_interest=False))
            self.assertEqual(r.no_interest, stats_from_metrics(m), r.variant)


class TestFactorIcPreRank(unittest.TestCase):
    def test_correlation_matrices_equal_the_pairwise_computation(self):
        ds = synthetic_dataset()
        res = run_factor_ic(ds)
        sb = compute_scores(ds.stocks, StrategyParams())
        raw = {"r3": sb.r3, "r10": sb.r10, "range52": sb.range52, "ma20_div": sb.ma20_div, "ma50_div": sb.ma50_div}
        common = sb.liquid.copy()
        for f in FACTORS:
            common &= np.isfinite(raw[f]) & np.isfinite(sb.pct[f])
        in_eval = np.arange(ds.stocks.T) >= ds.start_idx
        for mat, src in ((res.corr_before, raw), (res.corr_after, sb.pct)):
            for a in range(len(FACTORS)):
                for b in range(a + 1, len(FACTORS)):
                    daily = row_rank_corr(src[FACTORS[a]], src[FACTORS[b]], common)[in_eval]
                    good = daily[np.isfinite(daily)]
                    want = float(good.mean()) if good.size else float("nan")
                    np.testing.assert_array_equal(mat[a, b], want)


if __name__ == "__main__":
    unittest.main()
