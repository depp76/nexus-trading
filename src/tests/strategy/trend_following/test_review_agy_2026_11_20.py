"""review_agy.md (2026-11-20) items fixed without changing any backtest or research
result (trend_following.md 10-15): M10 snapshot edges and the hold-out ledger of
snapshot loads, H14 / M9 live-signal display, L3 numeric guards. Synthetic data
only (helpers.synthetic_dataset), no network; the ledger is a temporary file."""
import json
import math
import os
import random
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl

import paths
from strategy.common import inputs as inputs_mod
from strategy.trend_following import dataset as dataset_mod
from strategy.trend_following.backtest import Engine
from strategy.trend_following.config import DEV_END, StrategyParams
from strategy.trend_following.costs import CostModel
from strategy.trend_following.metrics import NAV_BASE, cagr, information_ratio
from strategy.trend_following.signal_provider import check_stock, layout_templates, pick_signals
from strategy.trend_following.signals import compute_features
from strategy.trend_following.snapshot import (
    UnsupportedSnapshotError, load_snapshot, save_snapshot,
)
from tests.strategy.trend_following.helpers import TOP, business_days, make_frame, synthetic_dataset


def _cut(book, t):
    return replace(book, dates=book.dates[:t + 1], open=book.open[:t + 1], high=book.high[:t + 1],
                   low=book.low[:t + 1], close=book.close[:t + 1], volume=book.volume[:t + 1])


def _ledger_lines():
    p = paths.HOLDOUT_LEDGER_FILE
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


# ---------------------------------------------------------------------------
# M10: snapshot edges and the ledger of snapshot loads
# ---------------------------------------------------------------------------
class TestSnapshotEdges(unittest.TestCase):
    def test_one_flow_array_only_is_refused_before_writing(self):
        ds = synthetic_dataset(with_flows=True)
        ds.flow_retail = None
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "snap"
            with self.assertRaises(UnsupportedSnapshotError):
                save_snapshot(ds, target)
            self.assertEqual(os.listdir(d), [])

    def test_empty_dataset_is_refused(self):
        ds = synthetic_dataset()
        empty = replace(ds, stocks=_cut(ds.stocks, -1), index_close=ds.index_close[:0],
                        rf_annual=ds.rf_annual[:0], rf_period=ds.rf_period[:0], start_idx=0, bm=None)
        self.assertEqual(empty.dates, [])
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(UnsupportedSnapshotError):
                save_snapshot(empty, Path(d) / "snap")

    def test_holdout_snapshot_load_is_recorded_save_is_not(self):
        ds = synthetic_dataset(first_day=date(2023, 6, 1), start=date(2024, 6, 3), n_days=480)
        self.assertGreater(ds.dates[-1], DEV_END)
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "snap"
            save_snapshot(ds, target, allow_holdout=True)          # its read-back is not an opening
            self.assertEqual(_ledger_lines(), [])
            load_snapshot(target, allow_holdout=True)
            lines = _ledger_lines()
        self.assertEqual([(x["source"], x["end"]) for x in lines], [("load_snapshot", ds.dates[-1].isoformat())])

    def test_dev_window_snapshot_load_writes_nothing(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as d:
            save_snapshot(ds, Path(d) / "snap")
            load_snapshot(Path(d) / "snap")
        self.assertEqual(_ledger_lines(), [])


# ---------------------------------------------------------------------------
# H14 / M9: live signals (Universe "전략 신호", MA chart "전략 점검") only
# ---------------------------------------------------------------------------
class TestLiveSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def test_illiquid_exit_is_listed_like_check_stock_shows_it(self):
        t = TOP + 3                                            # SPIKE breaks its 10-day low
        book = _cut(self.ds.stocks, t)
        j = book.col("SPIKE")
        vol = book.volume.copy()
        vol[:, j] = 1.0                                        # traded, far below the 100억 floor
        book = replace(book, volume=vol)
        idx = self.ds.index_close[:t + 1]
        f = compute_features(SimpleDs(book, idx), StrategyParams())
        self.assertFalse(f.liquid[t, j])
        self.assertTrue(f.exit_channel[t, j])
        picks = pick_signals("A0", book, idx)
        self.assertIn("SPIKE", [s.ticker for s in picks.bottom])
        self.assertEqual(check_stock("A0", "SPIKE", book, idx).verdict, "exit")

    def test_missing_index_history_is_unknown_not_off(self):
        book = self.ds.stocks
        nan_idx = np.full(book.T, np.nan)
        self.assertIsNone(pick_signals("A1", book, nan_idx).regime_on)
        rows = check_stock("A1", "UP", book, nan_idx).entry
        self.assertIsNone(rows[0].ok)                          # the L1 row: unknown, not a red badge
        short = self.ds.index_close.copy()
        short[:-30] = np.nan                                   # 30 sessions: no MA60 yet
        self.assertIsNone(pick_signals("A1", book, short).regime_on)
        # with the full index the state is judged as before
        f = compute_features(self.ds, StrategyParams())
        self.assertEqual(pick_signals("A1", book, self.ds.index_close).regime_on, bool(f.regime_on[-1]))

    def test_labels_follow_the_parameters(self):
        rows = check_stock("A0", "UP", self.ds.stocks, self.ds.index_close).entry
        self.assertTrue(any("100억" in r.label for r in rows))
        p = StrategyParams(min_avg_trading_value=5e9)
        rows = check_stock("A0", "UP", self.ds.stocks, self.ds.index_close, params=p).entry
        self.assertTrue(any("50억" in r.label for r in rows))
        c3 = layout_templates("C3")[0]
        self.assertEqual([m.label for m in c3.metrics], ["MA60Div", "High52", "R3"])
        self.assertIn("High52 ≥ 0.70", [r.label for r in c3.entry])
        self.assertEqual([m.label for m in layout_templates("C1")[0].metrics], ["MA60Div", "Range52", "R3"])

    def test_flows_use_the_requested_start(self):
        dates = business_days(date(2024, 1, 2), 80)
        frame = make_frame(dates, np.full(len(dates), 10_000.0))
        seen = []

        def flows(tk, start):
            seen.append(start)
            return pl.DataFrame()

        with patch.object(inputs_mod, "get_historical_data", return_value=frame), \
                patch("data.flows.get_investor_flows", side_effect=flows):
            inputs_mod.load_daily_inputs([{"ticker": "005930", "name": "x", "market": "KOSPI"}],
                                         need_flows=True, start="2024-01-02")
        self.assertEqual(seen, ["2024-01-02"])


class SimpleDs:
    def __init__(self, book, idx):
        self.stocks, self.index_close, self.flow_fi, self.index_by_market = book, idx, None, None


# ---------------------------------------------------------------------------
# L3: guards that change nothing on valid inputs
# ---------------------------------------------------------------------------
class TestNumericGuards(unittest.TestCase):
    def test_cagr_of_a_wiped_out_nav(self):
        d = [date(2021, 1, 4), date(2022, 1, 4)]
        self.assertEqual(cagr(np.array([NAV_BASE, 0.0]), d), -1.0)
        self.assertEqual(cagr(np.array([NAV_BASE, -5.0]), d), -1.0)
        nav = np.array([NAV_BASE, NAV_BASE * 1.21])
        self.assertEqual(cagr(nav, d), float((nav[-1] / NAV_BASE) ** (365.0 / 365) - 1.0))

    def test_information_ratio_float_noise_floor(self):
        a = np.array([0.01, -0.02, 0.005, 0.0])
        self.assertEqual(information_ratio(a, a + 1e-17), 0.0)
        b = np.array([0.0, 0.01, -0.01, 0.02])
        d = a - b
        self.assertEqual(information_ratio(a, b), float(np.mean(d) / np.std(d, ddof=1) * math.sqrt(252)))

    def test_limit_prices_without_a_tick_grid(self):
        cm = CostModel()
        down, up = cm.limit_prices(date(2024, 3, 4), "NASDAQ", 100.0)
        self.assertAlmostEqual(down, 70.0)
        self.assertAlmostEqual(up, 130.0)
        self.assertEqual(cm.limit_prices(date(2022, 3, 4), "KOSPI", 10_000), (7_000.0, 13_000.0))

    def test_kospi_calendar_sorted_and_unique(self):
        dates = business_days(date(2024, 1, 2), 10)
        shuffled = [dates[3], dates[0], dates[1], dates[1], dates[2]] + dates[4:]
        frame = pl.DataFrame({"Date": shuffled, "Close": [float(dates.index(d)) for d in shuffled]})
        with patch.object(dataset_mod, "get_historical_data", return_value=frame):
            cal, close = dataset_mod._load_kospi_calendar(dates[0], dates[-1], None, None)
        self.assertEqual(cal, dates)
        self.assertEqual(list(close), [float(i) for i in range(10)])

    def test_affordable_qty_equals_the_one_share_loop(self):
        """The bisection returns exactly what the former `q -= 1` loop returned."""
        ds = synthetic_dataset()
        rnd = random.Random(20261120)
        for _ in range(300):
            cm = CostModel(slippage_liquidity_coeff=rnd.choice([0.0, 0.05, 0.5, 5.0]),
                           min_commission=rnd.choice([0.0, 100.0, 5_000.0]),
                           cost_multiplier=rnd.choice([0.0, 1.0, 2.0]), round_to_tick=False)
            adv = np.full(ds.stocks.close.shape, rnd.choice([1e6, 1e8, 1e10]))
            eng = Engine(ds.stocks, ds.rf_period, StrategyParams(annual_reset=False), cm, ds.start_idx, "x", adv=adv)
            eng.cash = rnd.uniform(0.0, 5e6)
            t = ds.start_idx + rnd.randrange(1, 50)
            j = rnd.randrange(ds.stocks.N)
            market = rnd.choice(["KOSPI", "NASDAQ"])
            price = rnd.choice([rnd.uniform(500.0, 5_000.0), rnd.uniform(5_000.0, 500_000.0)])
            self.assertEqual(eng.affordable_qty(t, market, price, j), _old_affordable(eng, t, market, price, j))


def _old_affordable(eng, t, market, price, col):
    """backtest.Engine.affordable_qty before 2026-11-20, verbatim."""
    if price <= 0 or eng.cash <= 0:
        return 0
    cm = eng.cm
    per_share = price * (1.0 + (cm.commission_rate_for(market) + cm.slippage_rate) * cm.cost_multiplier)
    q = int(math.floor(eng.cash / per_share))
    d = eng.book.dates[t]
    adv = eng._adv_open(t, col)
    while q > 0 and price * q + cm.buy_cost(d, market, price, q, adv).total > eng.cash + 1e-9:
        q -= 1
    return q


if __name__ == "__main__":
    unittest.main()
