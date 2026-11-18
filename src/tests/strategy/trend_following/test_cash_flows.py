"""Spec 9-27: yearly withdrawals/deposits at the annual reset (9-2), recorded by
the engine, summarised by metrics.cash_flow_ledger and printed by the research
and robustness reports. Reporting only: the run itself must not change."""
import unittest
from datetime import date

import numpy as np

from strategy.trend_following.backtest import TRIM_REASON, Engine
from strategy.trend_following.benchmarks import BuyAndHoldPolicy, _rescale_result
from strategy.trend_following.config import StrategyParams
from strategy.trend_following.costs import CostModel
from strategy.trend_following.dataset import PriceBook
from strategy.trend_following.metrics import cash_flow_ledger, summarize_run, yearly_returns
from strategy.trend_following.research import ResearchRequest, cash_flow_table, run_research
from strategy.trend_following.robustness import RobustnessRequest, run_robustness
from strategy.trend_following.snapshot import default_snapshot_dir
from tests.strategy.trend_following.helpers import business_days, synthetic_dataset

SEED = StrategyParams().seed_krw


def _book(closes: np.ndarray, dates: list[date]) -> PriceBook:
    c = np.asarray(closes, dtype=float).reshape(-1, 1)
    return PriceBook(dates=dates, tickers=["X"], open=c.copy(), high=c * 1.001, low=c * 0.999, close=c,
                     volume=np.full_like(c, 1e7), markets={"X": "KOSPI"})


def _calendar(first: date, last: date) -> list[date]:
    days = business_days(first, 2000)
    return [d for d in days if d <= last]


def _year_path(dates: list[date], legs: dict[int, tuple[float, float]]) -> np.ndarray:
    """Linear price from `start` to `end` inside each calendar year."""
    out = np.empty(len(dates))
    for y, (p0, p1) in legs.items():
        idx = [i for i, d in enumerate(dates) if d.year == y]
        out[idx] = np.linspace(p0, p1, len(idx))
    return out


def _run(closes, dates, mult=1.0):
    eng = Engine(_book(closes, dates), np.zeros(len(dates)), StrategyParams(), CostModel().scaled(mult), 0, "T")
    return eng.run(BuyAndHoldPolicy("X"))


class TestLedgerUpThenDown(unittest.TestCase):
    """Year 1 up (excess banked), year 2 down (shortfall topped up), year 3 runs to the data's end."""

    @classmethod
    def setUpClass(cls):
        cls.dates = _calendar(date(2021, 1, 4), date(2023, 6, 30))
        cls.res = _run(_year_path(cls.dates, {2021: (10_000, 15_000), 2022: (15_000, 9_000), 2023: (9_000, 9_900)}),
                       cls.dates)
        cls.led = cash_flow_ledger(cls.res)

    def test_flows_by_year(self):
        y = self.led["years"]
        self.assertEqual(sorted(y), [2021, 2022, 2023])
        self.assertGreater(y[2021]["banked"], 0)
        self.assertEqual(y[2021]["topped_up"], 0)
        self.assertGreater(y[2021]["net_flow"], 0)
        self.assertEqual(y[2022]["banked"], 0)
        self.assertGreater(y[2022]["topped_up"], 0)
        self.assertLess(y[2022]["net_flow"], 0)
        self.assertFalse(y[2023]["reset"])                     # the data ends: no reset
        self.assertIsNone(y[2023]["net_flow"])
        self.assertIsNone(y[2023]["residual"])
        self.assertEqual(self.led["final_year"], 2023)

    def test_matches_engine_ledger(self):
        y = self.led["years"]
        self.assertEqual({k: v["banked"] for k, v in y.items() if v["banked"]}, self.res.banked_by_year)
        self.assertEqual({k: v["topped_up"] for k, v in y.items() if v["topped_up"]}, self.res.topup_by_year)
        self.assertEqual(self.led["banked"], sum(self.res.banked_by_year.values()))
        self.assertEqual(self.led["topped_up"], sum(self.res.topup_by_year.values()))

    def test_starts_and_residuals(self):
        y = self.led["years"]
        self.assertEqual(y[2021]["start"], SEED)
        for yr in (2021, 2022):
            self.assertAlmostEqual(y[yr]["residual"], y[yr + 1]["start"] - SEED, places=6)
            self.assertGreaterEqual(y[yr]["residual"], 0)
        self.assertEqual(y[2022]["residual"], 0)              # a top-up restores the seed exactly

    def test_final_diff_and_total_pnl(self):
        y = self.led["years"]
        self.assertAlmostEqual(self.led["final_diff"], self.res.equity[-1] - SEED, places=6)
        self.assertAlmostEqual(y[2023]["end_pre"], self.res.equity[-1], places=6)
        self.assertAlmostEqual(self.led["total_pnl"],
                               self.led["banked"] - self.led["topped_up"] + self.led["final_diff"], places=6)
        # spec 9-28: the sum of the yearly P&L is the total P&L exactly -- the
        # trim's fill-vs-close difference is in the year's result, not a gap
        yearly_pnl = sum(v["end_pre"] - v["start"] for v in y.values())
        self.assertAlmostEqual(yearly_pnl, self.led["total_pnl"], places=4)
        self.assertNotIn("fill_gap", y[2021])
        # a reset year's end value is what the account held after the trim:
        # the next year's start plus what was banked, minus what was topped up
        for yr in (2021, 2022):
            v = y[yr]
            self.assertAlmostEqual(v["end_pre"], y[yr + 1]["start"] + v["banked"] - v["topped_up"], places=4)

    def test_yearly_returns_agree(self):
        yr = yearly_returns(self.res.nav, self.res.dates)
        for k, v in self.led["years"].items():
            self.assertAlmostEqual(v["end_pre"] / v["start"] - 1.0, yr[k], places=12)

    def test_summary_carries_the_ledger_and_keeps_the_totals(self):
        m = summarize_run(self.res)
        self.assertEqual(m["cash_flows_by_year"]["years"].keys(), self.led["years"].keys())
        self.assertEqual(m["banked"], self.led["banked"])
        self.assertEqual(m["topped_up"], self.led["topped_up"])

    def test_zero_cost_run_closes_exactly_without_trim(self):
        # 0x costs and no trim needed in a falling year: the identity holds with no gap at all
        dates = _calendar(date(2021, 1, 4), date(2022, 6, 30))
        res = _run(_year_path(dates, {2021: (10_000, 8_000), 2022: (8_000, 8_800)}), dates, mult=0.0)
        led = cash_flow_ledger(res)
        self.assertAlmostEqual(sum(v["end_pre"] - v["start"] for v in led["years"].values()), led["total_pnl"],
                               places=6)


class TestTrimFillInTheYearResult(unittest.TestCase):
    """Spec 9-28: a year-end close off the tick grid makes the trim fill below
    the close the position was marked at. That difference must land in the
    year's result (NAV chain and end_pre), so the yearly P&L adds up to the
    total P&L exactly; it used to vanish between the bank and the NAV."""

    def test_off_tick_close(self):
        dates = _calendar(date(2021, 1, 4), date(2022, 6, 30))
        res = _run(_year_path(dates, {2021: (10_000, 15_003), 2022: (15_000, 15_500)}), dates)
        trims = [t for t in res.trades if t.reason == TRIM_REASON]
        self.assertTrue(trims)
        self.assertLess(trims[0].exit_price, 15_003)              # tick-rounded below the close
        led = cash_flow_ledger(res)
        y = led["years"]
        self.assertAlmostEqual(sum(v["end_pre"] - v["start"] for v in y.values()), led["total_pnl"], places=4)
        self.assertAlmostEqual(y[2021]["end_pre"], y[2022]["start"] + y[2021]["banked"] - y[2021]["topped_up"],
                               places=4)
        yr = yearly_returns(res.nav, res.dates)
        self.assertAlmostEqual(y[2021]["end_pre"] / y[2021]["start"] - 1.0, yr[2021], places=12)


class TestTwoYears(unittest.TestCase):
    def test_up_then_down_two_years(self):
        """Year 1 +, year 2 -: one bank, no top-up (year 2 is the last, never reset), negative final difference."""
        dates = _calendar(date(2021, 1, 4), date(2022, 12, 30))
        res = _run(_year_path(dates, {2021: (10_000, 13_000), 2022: (13_000, 10_000)}), dates)
        led = cash_flow_ledger(res)
        self.assertGreater(led["banked"], 0)
        self.assertEqual(led["topped_up"], 0)
        self.assertLess(led["final_diff"], 0)
        self.assertAlmostEqual(led["total_pnl"], led["banked"] + led["final_diff"], places=6)
        yr = yearly_returns(res.nav, res.dates)
        self.assertGreater(yr[2021], 0)
        self.assertLess(yr[2022], 0)
        for k, v in led["years"].items():
            self.assertAlmostEqual(v["end_pre"] / v["start"] - 1.0, yr[k], places=12)


class TestIntegerShareLeftover(unittest.TestCase):
    def test_expensive_name_leaves_a_residual(self):
        """3 shares of a 3,000,000 KRW name: a 30% year cannot be trimmed by whole
        shares, so only the cash is banked and the leftover stays in the account."""
        dates = _calendar(date(2021, 1, 4), date(2022, 6, 30))
        res = _run(_year_path(dates, {2021: (3_000_000, 3_900_000), 2022: (3_900_000, 3_900_000)}), dates)
        led = cash_flow_ledger(res)
        y = led["years"][2021]
        self.assertGreater(y["banked"], 0)
        self.assertGreater(y["residual"], 1_000_000)
        self.assertAlmostEqual(y["residual"], res.year_start_equity_by_year[2022] - SEED, places=6)
        self.assertAlmostEqual(led["years"][2022]["start"], SEED + y["residual"], places=6)


class TestRecordingDoesNotChangeTheRun(unittest.TestCase):
    def test_reset_and_nav_untouched_by_the_ledger(self):
        dates = _calendar(date(2021, 1, 4), date(2023, 6, 30))
        closes = _year_path(dates, {2021: (10_000, 15_000), 2022: (15_000, 9_000), 2023: (9_000, 9_900)})
        res = _run(closes, dates)
        res2 = _run(closes, dates)
        self.assertTrue(np.array_equal(res.nav, res2.nav))
        # the engine's ledger fields line up with what it already exposed
        self.assertEqual(set(res.year_end_equity_by_year), {2021, 2022})
        self.assertEqual(set(res.year_start_equity_by_year), {2021, 2022, 2023})
        last_2021 = max(i for i, d in enumerate(dates) if d.year == 2021)
        self.assertAlmostEqual(res.year_start_equity_by_year[2022], res.equity[last_2021], places=6)

    def test_bm2_rescale_scales_the_new_fields(self):
        dates = _calendar(date(2021, 1, 4), date(2022, 6, 30))
        res = _run(_year_path(dates, {2021: (10_000, 13_000), 2022: (13_000, 13_000)}), dates)
        start, end = dict(res.year_start_equity_by_year), dict(res.year_end_equity_by_year)
        _rescale_result(res, 10.0)
        self.assertEqual(res.year_start_equity_by_year, {k: v / 10.0 for k, v in start.items()})
        self.assertEqual(res.year_end_equity_by_year, {k: v / 10.0 for k, v in end.items()})


class TestReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)
        cls.result = run_research(cls.ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0", "B"),
                                                          cost_multipliers=(0.0, 1.0), run_event_study=False,
                                                          include_flows=True))

    def test_research_section_follows_the_yearly_returns(self):
        md = self.result.to_markdown()
        i_y = md.index("## Yearly returns (1x)")
        i_f = md.index("## Yearly cash flows (1x, KRW 만 원)")
        self.assertLess(i_y, i_f)
        section = md[i_f:md.find("\n## ", i_f + 5) if md.find("\n## ", i_f + 5) > 0 else len(md)]
        rows = [ln for ln in section.splitlines() if ln.startswith("| ")]
        ids = [r.split("|")[1].strip() for r in rows[1:]]
        self.assertEqual(ids, ["A0", "B", "BM1", "BM2", "BM3"])          # strategies + BM1-BM3, no BM4
        head = rows[0]
        for label in ("Banked", "Topped up", "y P&L", "end vs seed", "leftover"):
            self.assertIn(label, head)

    def test_cells_are_man_won_of_the_ledger(self):
        run = next(r for r in self.result.runs_at(1.0) if r.id == "A0")
        led = run.metrics["cash_flows_by_year"]
        table = cash_flow_table([run])[0]
        cells = [c.strip() for c in table.splitlines()[2].split("|")[1:-1]]
        y0 = min(led["years"])
        expected = round(led["years"][y0]["net_flow"] / 1e4)
        self.assertEqual(int(cells[1].replace(",", "")), expected)
        # ... Banked | Topped up | P&L | leftover
        self.assertEqual(int(cells[-2].replace(",", "").replace("+", "")), round(led["total_pnl"] / 1e4))
        self.assertEqual(int(cells[-4].replace(",", "")), round(led["banked"] / 1e4))

    def test_robustness_report_has_the_table(self):
        rob = run_robustness(self.ds, RobustnessRequest(
            ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",), cost_multipliers=(1.0,),
                            run_event_study=False, include_flows=True), run_e1=False, run_e2=False))
        md = rob.to_markdown()
        i_perf = md.index("## 성과표")
        i_flow = md.index("### 연도별 인출·입금 (비용 1×, 만 원")
        self.assertLess(i_perf, i_flow)
        self.assertLess(i_flow, md.index("## 보완 요청 목록"))
        self.assertIn("인출 합", md[i_flow:])
        self.assertIn("평가 종료 차액", md[i_flow:])


SNAPSHOT = default_snapshot_dir("dev_2021_2024_nex23")
# docs/yearly_flows_report_2026-11-15.md section 5 (1x, 만 원): 2021-2023 net flow, 2024 final value - seed,
# banked, topped up, total P&L, leftover after the 2023 reset.
EXPECTED_1X = {
    "A0": (272, -324, -45, 73, 272, 370, -25, 0),
    "A1": (568, -59, 102, 341, 670, 59, 953, 38),
    "A2": (225, -59, 218, 157, 443, 59, 541, 0),
    "A3": (586, -44, 10, 91, 596, 44, 642, 26),
    "A4": (460, -40, 180, -10, 641, 40, 591, 0),
    "A5": (556, -30, 142, -6, 698, 30, 661, 0),
    "B": (-241, -30, 123, 94, 123, 271, -54, 22),
    # C rows run as C1S-C3S: the v06 50-day trend line (the default is 60 since v07, spec 10-13).
    "C1S": (155, 0, 82, 127, 237, 0, 364, 0),
    "C2S": (26, 29, 2, 194, 58, 0, 252, 0),
    "C3S": (192, -35, 70, 89, 262, 35, 316, 0),
    "BM1": (28, -241, 248, -92, 276, 241, -58, 0),
    "BM2": (136, -201, 155, 50, 291, 201, 140, 1),
    "BM3": (12, 30, 52, -66, 94, 0, 28, 0),
}


@unittest.skipUnless((SNAPSHOT / "manifest.json").exists(), "snapshot dev_2021_2024_nex23 not present")
class TestSnapshotRegression(unittest.TestCase):
    def test_section_5_values(self):
        from strategy.trend_following.snapshot import load_snapshot
        ds = load_snapshot(SNAPSHOT)
        res = run_research(ds, ResearchRequest(start=ds.eval_dates[0], variant_ids=tuple(k for k in EXPECTED_1X
                                                                                         if not k.startswith("BM")),
                                               cost_multipliers=(1.0,), run_event_study=False,
                                               include_flows=ds.flow_fi is not None))
        got = {}
        for r in res.runs_at(1.0):
            if r.id not in EXPECTED_1X:
                continue
            led = r.metrics["cash_flows_by_year"]
            y = led["years"]
            got[r.id] = tuple(round(v / 1e4) for v in (
                y[2021]["net_flow"], y[2022]["net_flow"], y[2023]["net_flow"], led["final_diff"],
                led["banked"], led["topped_up"], led["total_pnl"], y[2023]["residual"]))
        self.assertEqual(got, EXPECTED_1X)


if __name__ == "__main__":
    unittest.main()
