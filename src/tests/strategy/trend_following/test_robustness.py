"""NEX-52: robustness runner and checklist-form report (spec 6-4 robustness, 9-17;
checklist E1, E2, E4, E5 and the A2 contamination option)."""
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import numpy as np

from strategy.trend_following import run as run_mod
from strategy.trend_following.backtest import Trade, run_backtest
from strategy.trend_following.config import (
    E2_NEIGHBORS, E2_SHARPE_JUMP, PERIODS, VARIANTS, StrategyParams,
)
from strategy.trend_following.costs import CostModel, TradeCost
from strategy.trend_following.metrics import cagr, max_drawdown, nav_returns, sharpe, summarize_run
from strategy.trend_following.research import ResearchRequest
from strategy.trend_following.robustness import (
    GROUP_A2, GROUP_BASE, GROUP_E1, GROUP_E2, NeighborRun, PerfStats, RobustnessRequest, RunCounter,
    UnregisteredNeighborError, neighbor_params, period_stats, planned_e2_runs, run_robustness, top_trade_stats,
    warmup_check,
)
from strategy.trend_following.signals import compute_features
from strategy.trend_following.snapshot import save_snapshot
from tests.strategy.trend_following.helpers import synthetic_dataset

START = date(2021, 1, 4)
SUBSET = ("A0", "A1", "A5", "B", "C1")


def _req(variants=SUBSET, mults=(1.0,), **kw):
    return RobustnessRequest(
        ResearchRequest(start=START, variant_ids=tuple(variants), cost_multipliers=mults, run_event_study=False,
                        include_flows=True), **kw)


def _trade(pnl, reason="channel"):
    return Trade("T", date(2021, 2, 1), date(2021, 3, 1), 100.0, 100.0 + pnl / 10, 10, pnl, pnl / 10.0, reason, 28,
                 TradeCost())


class TestE2Constant(unittest.TestCase):
    def test_constant_equals_the_approved_table(self):
        a = ("A0", "A1", "A2", "A3", "A4", "A5")
        self.assertEqual(E2_NEIGHBORS, (
            ("N1", "breakout_window", 15, a),
            ("N2", "breakout_window", 25, a),
            ("N3", "trend_ma_long", 120, a + ("B",)),
            ("N4", "regime_ma", 20, a[1:] + ("B",)),
            ("N5", "regime_ma", 120, a[1:] + ("B",)),
        ))
        self.assertEqual([len(r[3]) for r in E2_NEIGHBORS], [6, 6, 7, 6, 6])
        self.assertEqual(len(planned_e2_runs()), 31)
        self.assertEqual(E2_SHARPE_JUMP, 0.5)

    def test_defaults_match_the_registered_base_values(self):
        d = StrategyParams()
        self.assertEqual((d.breakout_window, d.trend_ma_long, d.regime_ma, d.check_weekday), (20, 60, 60, 0))

    def test_no_run_for_c_series(self):
        ids = {vid for *_, vid in planned_e2_runs()}
        self.assertTrue(ids.isdisjoint({"C1", "C2", "C3"}))
        self.assertEqual(planned_e2_runs(("C1", "C2", "C3")), [])

    def test_every_run_differs_from_the_base_in_one_field(self):
        base = StrategyParams()
        for nid, f, v, vid in planned_e2_runs():
            diff = {k for k, val in asdict(neighbor_params(base, f, v)).items() if val != asdict(base)[k]}
            self.assertEqual(diff, {f}, nid)
            self.assertEqual(getattr(neighbor_params(base, f, v), f), v)

    def test_unregistered_pairs_are_rejected(self):
        base = StrategyParams()
        for f, v in (("breakout_window", 55), ("breakout_window", 20), ("breakout_window", 15.0),
                     ("trend_ma_long", 20), ("regime_ma", 40), ("atr_window", 10), ("check_weekday", 1),
                     ("max_positions", 5)):
            with self.assertRaises(UnregisteredNeighborError, msg=(f, v)):
                neighbor_params(base, f, v)
        with self.assertRaises(UnregisteredNeighborError):
            warmup_check(synthetic_dataset(), base, "regime_ma", 40)

    def test_judge_boundary(self):
        def run(delta):
            return NeighborRun("N1", "breakout_window", 15, "A0", PerfStats(1.0 + delta, 0.0, 0.0), {}, 1.0)
        self.assertTrue(run(0.5).passed)
        self.assertTrue(run(-0.5).passed)
        self.assertFalse(run(0.51).passed)
        self.assertFalse(run(-0.51).passed)


class TestRequestGuards(unittest.TestCase):
    def test_holdout_and_foreign_parameters_refused(self):
        r = ResearchRequest(start=START, allow_holdout=True)
        with self.assertRaises(ValueError):
            RobustnessRequest(r)
        with self.assertRaises(ValueError):
            RobustnessRequest(ResearchRequest(start=START, params=StrategyParams(breakout_window=15)))
        with self.assertRaises(ValueError):
            RobustnessRequest(ResearchRequest(start=START, cost_multipliers=(0.0, 2.0)))
        with self.assertRaises(ValueError):
            RobustnessRequest(ResearchRequest(start=START), flow_contamination=2)

    def test_contamination_is_off_by_default(self):
        self.assertEqual(RobustnessRequest(ResearchRequest(start=START)).flow_contamination, 0)


class TestWarmup(unittest.TestCase):
    def test_default_snapshot_warmup_covers_every_neighbour(self):
        ds = synthetic_dataset()
        for _, f, v, _ in E2_NEIGHBORS:
            chk = warmup_check(ds, StrategyParams(), f, v)
            self.assertTrue(chk["ok"], chk)
        chk = warmup_check(ds, StrategyParams(), "regime_ma", 120)
        self.assertEqual(chk["needed_sessions"], 125)
        self.assertEqual(chk["available_sessions"], ds.start_idx + 1)

    def test_indicator_is_valid_on_the_first_evaluation_session(self):
        ds = synthetic_dataset()
        p = replace(StrategyParams(), regime_ma=120, trend_ma_long=120)
        f = compute_features(ds, p)
        t0 = ds.start_idx
        self.assertTrue(np.isfinite(f.ma_short[t0]).any())
        self.assertTrue(f.trend_ok[t0].any())                 # UP: MA120 and its 5-session lag both exist at day one
        self.assertTrue(f.regime_on[t0])                      # MA120 of the index exists at day one

    def test_short_warmup_skips_the_neighbour_and_says_so(self):
        ds = synthetic_dataset(first_day=date(2020, 9, 1))     # ~88 warm-up sessions: MA120 impossible
        chk = warmup_check(ds, StrategyParams(), "trend_ma_long", 120)
        self.assertFalse(chk["ok"])
        res = run_robustness(ds, _req(("A0", "A1"), run_e1=False))
        ran = {(r.nid, r.variant) for r in res.neighbor_runs}
        self.assertNotIn(("N3", "A0"), ran)
        self.assertNotIn(("N5", "A1"), ran)
        self.assertIn(("N1", "A0"), ran)
        self.assertIn(("N4", "A1"), ran)
        self.assertEqual({s.split(" ")[0] for s in res.e2_skipped}, {"N3", "N5"})
        self.assertEqual(res.counter.groups[GROUP_E2], len(res.neighbor_runs))
        self.assertIn("부족", res.to_markdown())


class TestRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)
        cls.res = run_robustness(cls.ds, _req(), progress=None)

    def test_five_weekdays_per_variant(self):
        r = self.res
        for vid in SUBSET:
            days = sorted(x.weekday for x in r.weekday_runs if x.variant == vid)
            self.assertEqual(days, [0, 1, 2, 3, 4], vid)
        self.assertEqual(len(r.weekday_runs), 5 * len(SUBSET))

    def test_weekday_zero_is_the_base_run_and_others_use_that_weekday(self):
        r = self.res
        base = {x.id: x for x in r.research.runs_at(1.0, "strategy")}
        for vid in SUBSET:
            w0 = next(x for x in r.weekday_runs if x.variant == vid and x.weekday == 0)
            self.assertAlmostEqual(w0.whole.sharpe, base[vid].metrics["sharpe"])
        p3 = replace(StrategyParams(), check_weekday=3)
        want = summarize_run(run_backtest(self.ds, compute_features(self.ds, p3), p3, VARIANTS["A1"],
                                          CostModel().scaled(1.0)))
        got = next(x for x in r.weekday_runs if x.variant == "A1" and x.weekday == 3)
        self.assertAlmostEqual(got.whole.cagr, want["cagr"])
        self.assertAlmostEqual(got.whole.mdd, want["mdd"])

    def test_worst_weekday_summary(self):
        for s in self.res.weekday_summary():
            rows = s["rows"]
            self.assertEqual(s["worst_cagr"].whole.cagr, min(x.whole.cagr for x in rows))
            self.assertEqual(s["worst_mdd"].whole.mdd, min(x.whole.mdd for x in rows))
            self.assertAlmostEqual(s["cagr_spread"], max(x.whole.cagr for x in rows) - min(x.whole.cagr for x in rows))

    def test_e2_runs_are_exactly_the_registered_set_for_the_subset(self):
        r = self.res
        planned = planned_e2_runs(SUBSET)
        self.assertEqual(len(planned), 16)
        self.assertEqual([(x.nid, x.variant) for x in r.neighbor_runs], [(n, v) for n, _, _, v in planned])
        self.assertFalse({x.variant for x in r.neighbor_runs} & {"C1", "C2", "C3"})

    def test_delta_sharpe_uses_the_base_row_and_a_one_field_change(self):
        r = self.res
        base = {x.id: x for x in r.research.runs_at(1.0, "strategy")}
        for x in r.neighbor_runs:
            self.assertAlmostEqual(x.base_sharpe, base[x.variant].metrics["sharpe"])
            self.assertAlmostEqual(x.delta_sharpe, x.whole.sharpe - x.base_sharpe)
        n1 = next(x for x in r.neighbor_runs if x.nid == "N1" and x.variant == "A0")
        p = neighbor_params(StrategyParams(), "breakout_window", 15)
        want = summarize_run(run_backtest(self.ds, compute_features(self.ds, p), p, VARIANTS["A0"],
                                          CostModel().scaled(1.0)))
        self.assertAlmostEqual(n1.whole.sharpe, want["sharpe"])

    def test_neighbours_actually_change_behaviour(self):
        r = self.res
        base = {x.id: x for x in r.research.runs_at(1.0, "strategy")}
        moved = [x for x in r.neighbor_runs if x.whole.n_trades != base[x.variant].metrics["n_trades"]
                 or abs(x.whole.cagr - base[x.variant].metrics["cagr"]) > 1e-12]
        self.assertTrue(moved)

    def test_counter_adds_up(self):
        c = self.res.counter
        self.assertEqual(c.groups[GROUP_BASE], len(SUBSET))              # one cost multiplier
        self.assertEqual(c.groups[GROUP_E1], 4 * len(SUBSET))            # weekday 0 is the base run
        self.assertEqual(c.groups[GROUP_E2], 16)
        self.assertNotIn(GROUP_A2, c.groups)
        self.assertEqual(c.this_report, len(SUBSET) + 4 * len(SUBSET) + 16)
        self.assertEqual(c.total, c.this_report)

    def test_full_matrix_adds_31_e2_runs_to_the_counter(self):
        c = RunCounter(prior=7)
        c.add(GROUP_E2, len(planned_e2_runs()))
        self.assertEqual(c.groups[GROUP_E2], 31)
        self.assertEqual(c.total, 38)

    def test_flow_variants_skipped_without_flows(self):
        ds = synthetic_dataset(with_flows=False)
        res = run_robustness(ds, _req(("A0", "A3", "A5"), run_e1=False))
        self.assertEqual({x.variant for x in res.neighbor_runs}, {"A0"})
        self.assertEqual(res.counter.groups[GROUP_E2], len(res.neighbor_runs))

    def test_switches_turn_e1_and_e2_off(self):
        res = run_robustness(self.ds, _req(("A0",), run_e1=False, run_e2=False))
        self.assertEqual((res.weekday_runs, res.neighbor_runs), ([], []))
        self.assertEqual(res.counter.this_report, 1)
        md = res.to_markdown()
        self.assertNotIn("### E1", md)
        self.assertNotIn("### E2", md)

    def test_prior_trials_are_added_to_the_running_total(self):
        res = run_robustness(self.ds, _req(("A0",), run_e1=False, run_e2=False, prior_trials=100))
        self.assertEqual(res.counter.total, 101)
        self.assertIn("101", res.to_markdown())


class TestPeriodStats(unittest.TestCase):
    def test_single_period_over_everything_equals_the_scorecard(self):
        ds = synthetic_dataset()
        p = StrategyParams()
        res = run_backtest(ds, compute_features(ds, p), p, VARIANTS["A1"], CostModel().scaled(1.0))
        last_year = res.dates[-1].year
        got = period_stats(res.nav, res.dates, res.rf_period, (("all", 2021, last_year),))["all"]
        self.assertAlmostEqual(got.sharpe, sharpe(nav_returns(res.nav), res.rf_period))
        self.assertAlmostEqual(got.cagr, cagr(res.nav, res.dates))
        self.assertAlmostEqual(got.mdd, max_drawdown(res.nav))

    def test_periods_are_rebased_to_the_previous_period_end(self):
        dates = [date(2021, 1, 4), date(2021, 6, 1), date(2021, 12, 30), date(2022, 1, 3), date(2022, 6, 1),
                 date(2022, 12, 29), date(2023, 1, 2)]
        nav = np.array([1.0, 1.1, 1.2, 1.2, 1.5, 1.8, 1.8])
        ps = period_stats(nav, dates, np.zeros(7), (("21", 2021, 2021), ("22", 2022, 2022), ("99", 2030, 2030)))
        self.assertIsNone(ps["99"])
        self.assertAlmostEqual(ps["22"].cagr, (1.8 / 1.2) ** (365.0 / (dates[5] - dates[3]).days) - 1.0)
        self.assertAlmostEqual(ps["21"].mdd, 0.0)

    def test_report_periods_are_the_two_development_periods(self):
        self.assertEqual([p[0] for p in PERIODS], ["2021-22", "2023-24"])


class TestTopTrades(unittest.TestCase):
    def test_top_five_percent_of_twenty_trades_is_one(self):
        trades = [_trade(p) for p in [1000.0] + [-100.0] * 9 + [200.0] * 10]
        row = top_trade_stats("A0", trades, 10_000_000.0, 2.0)
        self.assertEqual((row.n_trades, row.k), (20, 1))
        self.assertAlmostEqual(row.total_pnl, 1000.0 - 900.0 + 2000.0)
        self.assertAlmostEqual(row.top_pnl, 1000.0)
        self.assertAlmostEqual(row.share, 1000.0 / 2100.0)
        self.assertAlmostEqual(row.ex_pnl, 1100.0)
        self.assertAlmostEqual(row.annual_pct, 2100.0 / 10_000_000.0 / 2.0)
        self.assertAlmostEqual(row.ex_annual_pct, 1100.0 / 10_000_000.0 / 2.0)

    def test_k_rounds_up_and_trims_are_not_trades(self):
        trades = [_trade(10.0 * i) for i in range(1, 22)] + [_trade(99999.0, "year_end_trim"),
                                                              _trade(99999.0, "weight_trim")]
        row = top_trade_stats("A0", trades, 1e7, 1.0)
        self.assertEqual((row.n_trades, row.k), (21, 2))                    # ceil(0.05 x 21)
        self.assertAlmostEqual(row.top_pnl, 210.0 + 200.0)

    def test_losing_total_has_no_share_and_empty_is_safe(self):
        row = top_trade_stats("A0", [_trade(-5.0), _trade(-1.0)], 1e7, 1.0)
        self.assertIsNone(row.share)
        self.assertEqual(row.k, 1)
        empty = top_trade_stats("A0", [], 1e7, 1.0)
        self.assertEqual((empty.n_trades, empty.k, empty.total_pnl), (0, 0, 0.0))

    def test_runner_reports_the_engine_trades(self):
        res = TestRunner.res
        row = next(t for t in res.top_trades if t.variant == "A0")
        base = next(x for x in res.research.runs_at(1.0, "strategy") if x.id == "A0")
        self.assertEqual(row.n_trades, base.metrics["n_trades"])
        self.assertEqual(len(res.top_trades), len(SUBSET))


class TestContamination(unittest.TestCase):
    def test_flag_shifts_the_flow_by_exactly_one_session(self):
        ds = synthetic_dataset(with_flows=True)
        p = StrategyParams()
        clean = compute_features(ds, p)
        lead = compute_features(ds, p, flow_lead=1)
        T = ds.stocks.T
        warm = 30           # the first rolling windows differ by one row at the series start only
        self.assertTrue(np.array_equal(lead.flow_ok[warm:T - 1], clean.flow_ok[warm + 1:]))
        self.assertFalse(np.array_equal(lead.flow_ok, clean.flow_ok))
        two = compute_features(ds, p, flow_lead=2)
        self.assertTrue(np.array_equal(two.flow_ok[warm:T - 2], clean.flow_ok[warm + 2:]))

    def test_runner_uses_the_shifted_flow_only_when_asked(self):
        ds = synthetic_dataset(with_flows=True)
        off = run_robustness(ds, _req(("A0", "A3", "A5"), run_e1=False, run_e2=False))
        self.assertEqual(off.contamination, [])
        self.assertNotIn("A2 참고", off.to_markdown())
        on = run_robustness(ds, _req(("A0", "A3", "A5"), run_e1=False, run_e2=False, flow_contamination=1))
        self.assertEqual([c.variant for c in on.contamination], ["A3", "A5"])         # flow variants only
        self.assertEqual(on.counter.groups[GROUP_A2], 2)
        p = StrategyParams()
        want = summarize_run(run_backtest(ds, compute_features(ds, p, flow_lead=1), p, VARIANTS["A3"],
                                          CostModel().scaled(1.0)))
        got = next(c for c in on.contamination if c.variant == "A3")
        self.assertAlmostEqual(got.contaminated.cagr, want["cagr"])
        self.assertEqual(got.contaminated.n_trades, want["n_trades"])
        base = next(x for x in on.research.runs_at(1.0, "strategy") if x.id == "A3")
        self.assertAlmostEqual(got.clean.cagr, base.metrics["cagr"])                  # clean side is the real run
        md = on.to_markdown()
        self.assertIn("A2 참고", md)
        self.assertIn("결론에 쓰지 않음", md)

    def test_contamination_does_not_touch_the_e2_or_conclusion(self):
        ds = synthetic_dataset(with_flows=True)
        a = run_robustness(ds, _req(("A1", "A3"), run_e1=False))
        b = run_robustness(ds, _req(("A1", "A3"), run_e1=False, flow_contamination=1))
        self.assertEqual([(x.nid, x.variant, x.whole) for x in a.neighbor_runs],
                         [(x.nid, x.variant, x.whole) for x in b.neighbor_runs])
        self.assertEqual(a.e2_verdicts(), b.e2_verdicts())


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)
        cls.res = run_robustness(cls.ds, _req(("A0", "A1", "B"), mults=(0.0, 1.0, 2.0)))
        cls.md = cls.res.to_markdown(["- 대상 커밋: `abc`"])

    def test_sections_follow_the_checklist_form(self):
        md = self.md
        order = ["# [D] NEX-52", "- 대상 커밋: `abc`", "- 실행 일시", "## 결론",
                 "## 편향·한계 경고", "## 체크리스트 결과표 (A~H)", "### E1", "### E2", "### E3", "### E4", "### E5",
                 "## 성과표", "### 개발 구간별", "## 보완 요청 목록"]
        pos = [md.index(s) for s in order]
        self.assertEqual(pos, sorted(pos))

    def test_without_a_provenance_header_the_report_names_spec_and_window(self):
        md = self.res.to_markdown()
        self.assertIn("- 스펙 버전: v06", md)
        self.assertIn("- 데이터 기간: 2021-01-04", md)

    def test_conclusion_never_declares_a_pass(self):
        head = self.md.split("## 편향·한계 경고")[0]
        self.assertIn("판정 불가", head)
        self.assertIn("E2 실패 변형", head)
        self.assertNotIn("검증 통과", head)

    def test_tables_carry_the_required_columns(self):
        md = self.md
        for s in ("최악 CAGR 요일", "최악 MDD 요일", "ΔSharpe", "2021-22 Sharpe", "2023-24 MDD", "상위 기여도",
                  "상위 제외 손익", "누적 합계", "다중검정", "웜업 확인"):
            self.assertIn(s, md)
        for nid in ("N1", "N2", "N3", "N4", "N5"):
            self.assertIn(f"| {nid} |", md)

    def test_checklist_table_lists_every_group_and_the_dev_end_check(self):
        md = self.md
        for item in ("| A1 |", "| C3 |", "| D4 |", "| E1 |", "| E2 |", "| E5 |", "| E6 |", "| H |"):
            self.assertIn(item, md)
        self.assertIn("2024-12-31", md)

    def test_data_end_is_inside_the_development_window(self):
        self.assertLessEqual(self.res.data_last_date, date(2024, 12, 31))


class TestHeadless(unittest.TestCase):
    def test_cli_writes_one_report(self):
        ds = synthetic_dataset(with_flows=True)
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap"
            man = save_snapshot(ds, snap)
            out = Path(tmp) / "out"
            code = run_mod.main(["--snapshot", str(snap), "--out", str(out), "--variants", "A0,A1,A3",
                                 "--robustness", "--a2-contaminate", "--prior-trials", "5"])
            self.assertEqual(code, 0)
            reports = list(out.glob("robustness_*.md"))
            self.assertEqual(len(reports), 1)
            md = reports[0].read_text(encoding="utf-8")
        self.assertIn(man["sha256"], md)
        self.assertIn("### E1", md)
        self.assertIn("### E2", md)
        self.assertIn("A2 참고", md)
        self.assertIn("생존편향", md)

    def test_default_cli_run_has_no_contamination(self):
        ds = synthetic_dataset(with_flows=True)
        with tempfile.TemporaryDirectory() as tmp:
            save_snapshot(ds, Path(tmp) / "s")
            path = run_mod.run_robustness_from_snapshot(Path(tmp) / "s", Path(tmp) / "o", ("A0",), commit="abc123")
            md = path.read_text(encoding="utf-8")
        self.assertIn("`abc123`", md)
        self.assertNotIn("A2 참고", md)


if __name__ == "__main__":
    unittest.main()
