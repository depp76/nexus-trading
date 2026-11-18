"""Spec 10-9 [D] / 10-13 (2026-11-18): the 60-day trend line is the C-series
default since v07, so C1L-C3L (the 10-11 ids) equal C1-C3; C1S-C3S pin the v06
50-day line; the E2 neighbours N6 / N7 apply to C1-C3 and C1L-C3L; the
cash-interest-0 reference runs and the 30-run E5 plan of 10-2. Synthetic data
only; no snapshot, no real-data backtest."""
import unittest
from dataclasses import asdict, replace
from datetime import date
from unittest.mock import patch

import numpy as np

from strategy import signal_api
from strategy.trend_following import run as run_mod
from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.config import (
    E2_NEIGHBORS, RESEARCH_VARIANTS, V06_C_VARIANTS, V07_VARIANTS, VARIANTS, StrategyParams, get_variant,
)
from strategy.trend_following.research import ResearchRequest
from strategy.trend_following.robustness import (
    GROUP_BASE, GROUP_E1, GROUP_E2, GROUP_NO_INTEREST, RobustnessRequest, run_robustness, variant_for_neighbor,
    warmup_check,
)
from strategy.trend_following.signals import compute_features
from strategy.trend_following.variant_info import describe_variant
from tests.strategy.trend_following.helpers import synthetic_dataset

START = date(2021, 1, 4)
V07 = ("C1L", "C2L", "C3L")
V06S = ("C1S", "C2S", "C3S")


def _robust(ds, variants, mults=(1.0,), **kw):
    return run_robustness(ds, RobustnessRequest(
        ResearchRequest(start=START, variant_ids=tuple(variants), cost_multipliers=mults, run_event_study=False,
                        include_flows=False), **kw))


class TestCandidates(unittest.TestCase):

    def test_v07_default_and_the_reproduction_ids(self):
        """10-13: the default is 60; C1L == C1 (v07); C1S = C1 with only the v06 50-day line."""
        base = StrategyParams()
        self.assertEqual(base.score_ma_long, 60)
        for c, cl, cs in (("C1", "C1L", "C1S"), ("C2", "C2L", "C2S"), ("C3", "C3L", "C3S")):
            cur = asdict(VARIANTS[c].apply(base))
            self.assertEqual(asdict(V07_VARIANTS[cl].apply(base)), cur, cl)
            old = asdict(V06_C_VARIANTS[cs].apply(base))
            self.assertEqual({k for k in cur if cur[k] != old[k]}, {"score_ma_long"}, cs)
            self.assertEqual(old["score_ma_long"], 50)
            for w in (V07_VARIANTS[cl], V06_C_VARIANTS[cs]):
                v = VARIANTS[c]
                self.assertEqual((v.mode, v.regime, v.volume, v.flow, v.flow_exit),
                                 (w.mode, w.regime, w.volume, w.flow, w.flow_exit))

    def test_kept_out_of_every_screen(self):
        self.assertEqual(list(VARIANTS), ["A0", "A1", "A2", "A3", "A4", "A5", "B", "C1", "C2", "C3"])
        self.assertTrue(set(V07 + V06S).isdisjoint(VARIANTS))
        self.assertTrue(set(V07 + V06S).isdisjoint(signal_api.variants()))    # Universe / MA chart menus
        self.assertEqual(ResearchRequest(start=START).variant_ids, tuple(VARIANTS))   # Strategy tab default
        self.assertEqual(list(RESEARCH_VARIANTS), list(VARIANTS) + list(V07) + list(V06S))
        with self.assertRaises(KeyError):
            get_variant("C1′")

    def test_descriptions_follow_the_window(self):
        def text(vid):
            return " ".join(line for _t, lines in describe_variant(vid).sections for line in lines)
        self.assertIn("60일선 위(MA60Div > 0)", text("C1"))             # v07 default
        self.assertIn("60일선 위(MA60Div > 0)", text("C1L"))
        self.assertIn("pct(MA60Div)", text("C3L"))
        self.assertIn("50일선 위(MA50Div > 0)", text("C1S"))            # v06 reproduction
        self.assertIn("매일: 종가가 50일선 아래", text("C2S"))


class TestNeighboursAndInterest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=False)

    def test_n6_n7_really_use_50_and_80(self):
        """The trap of 10-6 item 5: without variant_for_neighbor the variant's own override would
        put 60 back. N6 (50) must equal a C1S run (the v06 line), N7 a C1 run at 80."""
        res = _robust(self.ds, ("C1L",), run_e1=False)
        runs = {r.nid: r for r in res.neighbor_runs}
        self.assertEqual(set(runs), {"N6", "N7"})
        p, cm = StrategyParams(), ResearchRequest(start=START).cost_model.scaled(1.0)
        feats = compute_features(self.ds, p)
        from strategy.trend_following.metrics import summarize_run
        c50 = summarize_run(run_backtest(self.ds, feats, p, V06_C_VARIANTS["C1S"], cm))
        c80 = summarize_run(run_backtest(self.ds, feats, replace(p, score_ma_long=80), VARIANTS["C1"], cm))
        self.assertAlmostEqual(runs["N6"].whole.sharpe, c50["sharpe"], places=12)
        self.assertAlmostEqual(runs["N7"].whole.sharpe, c80["sharpe"], places=12)
        base = res.research.runs_at(1.0, "strategy")[0]
        self.assertEqual(base.id, "C1L")
        self.assertAlmostEqual(runs["N6"].base_sharpe, base.metrics["sharpe"], places=12)
        self.assertEqual(variant_for_neighbor("C1L", "score_ma_long").apply(replace(p, score_ma_long=80)).score_ma_long,
                         80)
        self.assertEqual(variant_for_neighbor("C1L", None).apply(p).score_ma_long, 60)
        self.assertIs(variant_for_neighbor("A1", "regime_ma"), VARIANTS["A1"])
        self.assertIs(variant_for_neighbor("C1", "score_ma_long"), VARIANTS["C1"])   # no override to drop

    def test_c1_neighbours_match_c1l(self):
        """Since v07 C1 and C1L are the same row, so their N6 / N7 runs are identical."""
        a = {r.nid: r.whole.sharpe for r in _robust(self.ds, ("C1",), run_e1=False).neighbor_runs}
        b = {r.nid: r.whole.sharpe for r in _robust(self.ds, ("C1L",), run_e1=False).neighbor_runs}
        self.assertEqual(set(a), {"N6", "N7"})
        for nid in a:
            self.assertAlmostEqual(a[nid], b[nid], places=12)

    def test_warmup_needs_the_window(self):
        chk = warmup_check(self.ds, StrategyParams(), "score_ma_long", 80)
        self.assertEqual(chk["needed_sessions"], 80)
        self.assertTrue(chk["ok"])

    def test_no_cash_interest_only_stops_the_accrual(self):
        ds = synthetic_dataset(index_up=False, with_flows=False)        # L1 off: C1 never buys
        p, cm = StrategyParams(), ResearchRequest(start=START).cost_model.scaled(1.0)
        feats = compute_features(ds, p)
        on = run_backtest(ds, feats, p, VARIANTS["C1"], cm)
        off = run_backtest(ds, feats, p, VARIANTS["C1"], cm, cash_interest=False)
        self.assertEqual((len(on.trades), len(off.trades)), (0, 0))
        self.assertGreater(on.nav[-1], on.nav[0])
        np.testing.assert_allclose(off.nav, off.nav[0])                    # all cash, no interest: flat
        np.testing.assert_array_equal(on.rf_period, off.rf_period)        # the metrics still deduct rf

    def test_the_registered_plan_is_30_runs(self):
        """10-2: base 9 + E1 12 + E2 6 + cash-interest-0 3."""
        res = _robust(self.ds, V07, mults=(0.0, 1.0, 2.0), cash_interest_reference=True)
        g = res.counter.groups
        self.assertEqual((g[GROUP_BASE], g[GROUP_E1], g[GROUP_E2], g[GROUP_NO_INTEREST]), (9, 12, 6, 3))
        self.assertEqual(res.counter.this_report, 30)
        self.assertEqual([r.variant for r in res.no_interest], list(V07))
        for r in res.no_interest:
            self.assertLessEqual(r.no_interest.cagr, r.with_interest.cagr + 1e-12)
        md = res.to_markdown()
        self.assertIn("### 현금 이자 0 (참고, 스펙 10-4)", md)
        # the neighbour table shows the candidates' own base (60), not the global default (50)
        self.assertIn("| N6 | score_ma_long | 60 → 50 | C1, C2, C3, C1L, C2L, C3L |", md)
        self.assertIn("| N7 | score_ma_long | 60 → 80 | C1, C2, C3, C1L, C2L, C3L |", md)
        self.assertIn("| N3 | trend_ma_long | 60 → 120 |", md)
        self.assertNotIn(GROUP_NO_INTEREST, _robust(self.ds, ("C1L",), run_e1=False, run_e2=False).counter.groups)

    def test_e2_rows_cover_c1_c3_and_the_v07_ids(self):
        rows = {nid: vids for nid, _f, _v, vids in E2_NEIGHBORS}
        self.assertEqual(rows["N6"], ("C1", "C2", "C3") + V07)
        self.assertEqual(rows["N7"], ("C1", "C2", "C3") + V07)
        self.assertFalse(any(set(vids) & set(V06S) for vids in rows.values()))   # C1S-C3S: reproduction only


class TestCli(unittest.TestCase):

    def test_unknown_variant_fails_before_loading(self):
        with patch.object(run_mod, "run_robustness_from_snapshot") as rr, self.assertRaises(KeyError):
            run_mod.main(["--snapshot", "x", "--robustness", "--variants", "C1L,ZZ"])
        rr.assert_not_called()

    def test_cash_interest_flag_reaches_the_runner(self):
        with patch.object(run_mod, "run_robustness_from_snapshot", return_value="r.md") as rr:
            run_mod.main(["--snapshot", "x", "--robustness", "--variants", "C1L,C2L,C3L", "--cash-interest-ref"])
        self.assertEqual(rr.call_args.args[2], V07)
        self.assertTrue(rr.call_args.kwargs["cash_interest_reference"])


if __name__ == "__main__":
    unittest.main()
