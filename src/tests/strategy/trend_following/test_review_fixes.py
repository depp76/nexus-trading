"""Regression tests for review_codex.md (2026-11-13) C1-C5 (spec 9-26).

C1  BM2's first-open candidates must not read the first session's own close x volume.
C2  a pending partial sell (weight trim) must not hide a name from the exit checks.
C3  a missing ETF must not turn the benchmark into a mix of the ones that loaded.
C4  the liquidity slippage of an open fill uses the ADV known before that open.
C5  a several-market dataset is refused by the snapshot writer instead of saved wrong.
"""
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import polars as pl

from strategy.trend_following.backtest import (
    Engine, Order, Position, TrendFollowingPolicy, WEIGHT_TRIM_REASON,
)
from strategy.trend_following.benchmarks import EqualWeightPolicy, run_bm2
from strategy.trend_following.config import StrategyParams, VARIANTS
from strategy.trend_following.costs import CostModel, TradeCost
from strategy.trend_following.research import ResearchRequest, run_research
from strategy.trend_following.scoring_policy import ScoringPolicy
from strategy.trend_following.signals import compute_features
from strategy.trend_following.snapshot import (
    SnapshotIntegrityError, UnsupportedSnapshotError, load_snapshot, save_snapshot,
)
from tests.strategy.trend_following.helpers import business_days, make_frame, path, synthetic_dataset
from tests.strategy.trend_following.test_markets import FX, _index_frame, _two_market_dataset
from tests.strategy.trend_following import test_scoring_policy as tsp


# ---------------------------------------------------------------------------
# C1
# ---------------------------------------------------------------------------
def _thin_flat_dataset(t0_volume: float):
    """synthetic_dataset with FLAT illiquid on every session except the first
    evaluation day, whose volume is `t0_volume` (its open stays 20,000)."""
    ds = synthetic_dataset()
    j = ds.stocks.col("FLAT")
    ds.stocks.volume[:, j] = 1.0
    ds.stocks.volume[ds.start_idx, j] = t0_volume
    return ds


class TestBm2FirstOpen(unittest.TestCase):
    P = StrategyParams(min_avg_trading_value=1e9, annual_reset=False)

    def _first_orders(self, ds):
        feats = compute_features(ds, self.P)
        eng = Engine(ds.stocks, ds.rf_period, self.P, CostModel().scaled(0.0), ds.start_idx, "BM2")
        orders = EqualWeightPolicy(feats.liquid).initial_orders(eng, ds.start_idx)
        return feats, sorted((o.ticker, o.side, o.qty) for o in orders)

    def test_first_session_volume_does_not_change_the_first_orders(self):
        thin, busy = _thin_flat_dataset(1.0), _thin_flat_dataset(2_000_000.0)
        f_thin, o_thin = self._first_orders(thin)
        f_busy, o_busy = self._first_orders(busy)
        t0, j = thin.start_idx, thin.stocks.col("FLAT")
        self.assertFalse(f_thin.liquid[t0 - 1, j] or f_busy.liquid[t0 - 1, j])
        self.assertFalse(f_thin.liquid[t0, j])
        self.assertTrue(f_busy.liquid[t0, j])                 # the same-day figure does differ
        self.assertEqual(o_thin, o_busy)                      # ...and the orders do not
        self.assertNotIn("FLAT", [tk for tk, _, _ in o_busy])

    def test_first_session_close_does_not_change_the_first_orders(self):
        a, b = synthetic_dataset(), synthetic_dataset()
        b.stocks.close[b.start_idx, :] *= 1.2                 # only the first session's close differs
        self.assertEqual(self._first_orders(a)[1], self._first_orders(b)[1])

    def test_run_bm2_holds_the_same_names_on_day_one(self):
        nav = []
        for vol in (1.0, 2_000_000.0):
            ds = _thin_flat_dataset(vol)
            res = run_bm2(ds, compute_features(ds, self.P), self.P, CostModel().scaled(0.0))
            self.assertEqual(res.n_positions[0], 3)
            nav.append(res.nav[0])
        self.assertAlmostEqual(nav[0], nav[1])

    def test_without_a_prior_session_the_book_starts_from_the_first_close(self):
        ds = synthetic_dataset()
        feats = compute_features(ds, self.P)
        liquid = np.ones_like(feats.liquid)                  # warm-up rows have no ADV; force eligibility
        eng = Engine(ds.stocks, ds.rf_period, self.P, CostModel().scaled(0.0), 0, "BM2")
        res = eng.run(EqualWeightPolicy(liquid))
        self.assertEqual(res.n_positions[0], 0)               # nothing bought at the very first open
        self.assertEqual(res.n_positions[1], ds.stocks.N)     # decided at the first close, filled next open

    def test_year_end_reset_still_reads_its_own_close(self):
        ds = synthetic_dataset()
        P = StrategyParams(min_avg_trading_value=1e9)
        feats = compute_features(ds, P)
        eng = Engine(ds.stocks, ds.rf_period, P, CostModel().scaled(0.0), ds.start_idx, "BM2")
        pol = EqualWeightPolicy(feats.liquid)
        t = next(i for i in range(ds.start_idx, ds.stocks.T - 1) if eng.is_year_end(i))
        eng.equity_now = eng.cash
        with patch.object(pol, "_targets", wraps=pol._targets) as targets:
            pol.decide(t, eng)
        self.assertEqual(targets.call_args.args[0], t)


# ---------------------------------------------------------------------------
# C2
# ---------------------------------------------------------------------------
N = 8


def _fake_book():
    tickers = [f"T{i}" for i in range(N)]
    return SimpleNamespace(close=np.full((1, N), 1000.0), N=N, tickers=tickers,
                           _col={tk: i for i, tk in enumerate(tickers)}, col=lambda tk: int(tk[1:]))


def _fake_feats(held_cols, exit_cols=()):
    ones = np.ones((1, N), dtype=bool)
    cand = ones.copy()
    cand[0, list(held_cols)] = False
    exit_channel = np.zeros((1, N), dtype=bool)
    exit_channel[0, list(exit_cols)] = True
    return SimpleNamespace(
        exit_channel=exit_channel, flow_exit=np.zeros((1, N), dtype=bool),
        check_day=np.array([True]), regime_on=np.array([True]), trend_ok=ones, breakout=cand, liquid=ones,
        traded=ones, vol_ok=ones, flow_ok=ones, flow_strength=np.zeros((1, N)),
        rs=np.arange(N, dtype=float)[None, :], atr=np.full((1, N), 10.0),
        high52_prox=np.arange(N, dtype=float)[None, :])


def _fake_eng(qtys, pending=()):
    pos = {f"T{i}": SimpleNamespace(col=i, qty=q, stop_price=None) for i, q in enumerate(qtys)}
    return SimpleNamespace(book=_fake_book(), positions=pos, pending=list(pending), equity_now=1_000_000.0)


class TestPendingTrimDoesNotBlockExits(unittest.TestCase):
    def test_exit_signal_replaces_the_pending_trim_with_one_full_sell(self):
        p = StrategyParams()
        trim = Order("T0", "sell", 2, WEIGHT_TRIM_REASON)
        eng = _fake_eng([10, 10, 10], pending=[trim])
        orders = TrendFollowingPolicy(_fake_feats({0, 1, 2}, exit_cols={0}), p, VARIANTS["A0"]).decide(0, eng)
        t0 = [(o.side, o.qty, o.reason) for o in orders if o.ticker == "T0"]
        self.assertEqual(t0, [("sell", None, "channel")])
        self.assertNotIn(trim, eng.pending)                   # replaced, not queued behind

    def test_same_orders_as_without_the_pending_trim(self):
        p = StrategyParams()
        feats = _fake_feats({0, 1, 2}, exit_cols={0})
        with_trim = TrendFollowingPolicy(feats, p, VARIANTS["A0"]).decide(
            0, _fake_eng([10, 10, 10], pending=[Order("T0", "sell", 2, WEIGHT_TRIM_REASON)]))
        without = TrendFollowingPolicy(feats, p, VARIANTS["A0"]).decide(0, _fake_eng([10, 10, 10]))
        key = lambda orders: sorted((o.ticker, o.side, o.qty, o.reason) for o in orders)   # noqa: E731
        self.assertEqual(key(with_trim), key(without))

    def test_a_pending_full_exit_is_not_duplicated(self):
        p = StrategyParams()
        eng = _fake_eng([10, 10], pending=[Order("T0", "sell", None, "stop")])
        orders = TrendFollowingPolicy(_fake_feats({0, 1}, exit_cols={0}), p, VARIANTS["A0"]).decide(0, eng)
        self.assertEqual([o for o in orders if o.ticker == "T0"], [])
        self.assertEqual(len(eng.pending), 1)

    def test_a_name_with_a_pending_trim_still_takes_a_slot_and_gets_no_second_trim(self):
        p = StrategyParams()
        eng = _fake_eng([300, 100, 100], pending=[Order("T0", "sell", 100, WEIGHT_TRIM_REASON)])
        orders = TrendFollowingPolicy(_fake_feats({0, 1, 2}), p, VARIANTS["A0"]).decide(0, eng)
        self.assertEqual([o for o in orders if o.ticker == "T0"], [])          # no stacked trim
        self.assertEqual(len([o for o in orders if o.side == "buy"]), p.max_positions - 3)

    def test_b_rank_out_replaces_the_pending_trim(self):
        p = StrategyParams()
        trim = Order("T0", "sell", 100, WEIGHT_TRIM_REASON)
        eng = _fake_eng([300, 100], pending=[trim])           # T0 has the lowest proximity -> ranked out
        orders = TrendFollowingPolicy(_fake_feats(set()), p, VARIANTS["B"]).decide(0, eng)
        self.assertEqual([(o.side, o.qty, o.reason) for o in orders if o.ticker == "T0"],
                         [("sell", None, "rank_out")])
        self.assertNotIn(trim, eng.pending)

    def test_c_series_exit_replaces_the_pending_trim(self):
        book = tsp._book()
        p = StrategyParams(max_positions=3)
        j = book.col("AAA")
        sb = tsp._scores(book, ma50={(tsp.CHECK, j): -0.01})
        eng = tsp._engine(book, p)
        tsp._hold(eng, book, "AAA", tsp.CHECK)
        trim = Order("AAA", "sell", 10, WEIGHT_TRIM_REASON)
        eng.pending = [trim]
        orders = ScoringPolicy(tsp._feats(book), p, VARIANTS["C1"], sb).decide(tsp.CHECK, eng)
        self.assertEqual([(o.qty, o.reason) for o in orders if o.ticker == "AAA"],
                         [(None, tsp.REASON_MA50)])
        self.assertNotIn(trim, eng.pending)

    def test_deferred_trim_then_stop_sells_the_whole_position_when_trading_resumes(self):
        """Trim queued -> halted (volume 0) -> stop fires -> next tradable open: all 100 shares go, as one trade."""
        ds = synthetic_dataset()
        p = StrategyParams(annual_reset=False)
        feats = compute_features(ds, p)
        b = ds.stocks
        j, t = b.col("FLAT"), ds.start_idx + 5
        eng = Engine(b, ds.rf_period, p, CostModel(), ds.start_idx, "x")
        px = float(b.close[t - 1, j])
        eng.positions["FLAT"] = Position(ticker="FLAT", col=j, qty=100, entry_price=px, entry_idx=t - 1,
                                         stop_price=px * 1.5, entry_costs=TradeCost(), last_close=px)
        eng.pending = [Order("FLAT", "sell", 20, WEIGHT_TRIM_REASON)]
        b.volume[t, j] = 0.0                                  # halted: the trim cannot fill
        eng._fill(t)
        self.assertEqual(eng.positions["FLAT"].qty, 100)
        self.assertEqual(len(eng.pending), 1)
        eng.equity_now = eng.cash + eng.positions_value()
        orders = TrendFollowingPolicy(feats, p, VARIANTS["A0"]).decide(t, eng)   # close < stop -> "stop"
        eng.pending.extend(orders)
        self.assertEqual([(o.qty, o.reason) for o in eng.pending if o.ticker == "FLAT"], [(None, "stop")])
        eng._fill(t + 1)
        self.assertNotIn("FLAT", eng.positions)
        self.assertEqual([(tr.qty, tr.reason) for tr in eng.trades], [(100, "stop")])


# ---------------------------------------------------------------------------
# C3
# ---------------------------------------------------------------------------
class TestMissingBenchmarkEtf(unittest.TestCase):
    def _load(self, missing=()):
        from strategy.trend_following import dataset as dsmod
        days = business_days(date(2019, 9, 2), 900)
        idx = _index_frame(days, np.linspace(2000, 2500, len(days)))

        def hist(ticker, _start):
            if ticker in missing:
                return None
            return idx if ticker in ("KS11", "^IXIC") else make_frame(days, path(100.0, [(len(days), 0.001)]))

        kr_rows = [{"Code": "005930", "Name": "삼성전자", "Marcap": 400e12}]
        us_rows = [{"Code": "AAPL", "Name": "Apple", "Marcap": 3e12}]
        fx = pl.DataFrame({"Date": days, "Close": np.full(len(days), FX)})
        with patch.object(dsmod, "get_historical_data", side_effect=hist), \
             patch.object(dsmod, "_fetch_kr_listing_naver", return_value=kr_rows), \
             patch.object(dsmod, "get_us_listing_ranked", return_value=us_rows), \
             patch.object(dsmod, "get_usd_krw_history", return_value=fx), \
             patch.object(dsmod, "get_cd91_series", return_value=None):
            return dsmod.load_dataset(date(2021, 1, 4), date(2022, 12, 30), universe_size=2,
                                      markets=("KOSPI", "NASDAQ"))

    def _research(self, ds):
        return run_research(ds, ResearchRequest(start=date(2021, 1, 4), end=date(2022, 12, 30), variant_ids=("A0",),
                                                cost_multipliers=(1.0,), run_event_study=False,
                                                markets=("KOSPI", "NASDAQ")))

    def test_all_etfs_loaded(self):
        ds = self._load()
        self.assertEqual(set(ds.bm.tickers), {"278530", "QQQ"})
        self.assertEqual(ds.info["bm_missing"], [])
        self.assertEqual(ds.info["bm_requested"], ["278530", "QQQ"])
        self.assertTrue({"BM1", "BM3"} <= {r.id for r in self._research(ds).runs})

    def test_one_missing_etf_leaves_no_benchmark_and_says_so(self):
        ds = self._load(missing=("QQQ",))
        self.assertIsNone(ds.bm)                                   # not a 100% KODEX 200TR "mix"
        self.assertEqual(ds.info["bm_missing"], ["NASDAQ: QQQ"])
        res = self._research(ds)
        ids = {r.id for r in res.runs}
        self.assertFalse({"BM1", "BM3"} & ids)
        self.assertTrue({"A0", "BM2", "BM4"} <= ids)
        self.assertTrue(any("NASDAQ: QQQ" in w and "N/A" in w for w in res.warnings), res.warnings)
        a0 = next(r for r in res.runs if r.id == "A0")
        self.assertIsNone(a0.metrics.get("bm1_excess_cagr"))

    def test_every_etf_missing(self):
        ds = self._load(missing=("QQQ", "278530", "069500"))
        self.assertIsNone(ds.bm)
        self.assertEqual(ds.info["bm_missing"], ["KOSPI: 278530", "NASDAQ: QQQ"])

    def test_kospi_price_only_fallback_is_kept(self):
        ds = self._load(missing=("278530",))
        self.assertEqual(set(ds.bm.tickers), {"069500", "QQQ"})
        self.assertEqual(ds.info["bm_missing"], [])
        self.assertFalse(ds.bm_is_total_return)                    # flagged as price-only, as before


# ---------------------------------------------------------------------------
# C4
# ---------------------------------------------------------------------------
class TestOpenFillAdv(unittest.TestCase):
    def _engine(self, adv):
        ds = synthetic_dataset()
        cm = CostModel(slippage_liquidity_coeff=0.1, round_to_tick=False)
        eng = Engine(ds.stocks, ds.rf_period, StrategyParams(annual_reset=False), cm, ds.start_idx, "x", adv=adv)
        return ds, eng

    def test_open_buy_cost_and_quantity_ignore_the_fill_days_adv(self):
        ds = synthetic_dataset()
        t, j = ds.start_idx + 3, ds.stocks.col("FLAT")
        results = []
        for adv_today in (20_000.0, 2_000_000_000.0):
            adv = np.full(ds.stocks.close.shape, 1e9)
            adv[t, j] = adv_today
            _, eng = self._engine(adv)
            px = float(eng.book.open[t, j])
            afford = eng.affordable_qty(t, "KOSPI", px, j)
            eng._execute_buy(t, "FLAT", 1, px, "entry", None)
            results.append((afford, eng.positions["FLAT"].entry_costs.slippage))
        self.assertEqual(results[0], results[1])
        cm = CostModel(slippage_liquidity_coeff=0.1)
        px = float(ds.stocks.open[t, j])
        self.assertAlmostEqual(results[0][1], px * (cm.slippage_rate + 0.1 * px / 1e9))    # adv[t-1]

    def test_open_sell_uses_yesterdays_adv_and_a_close_time_fill_todays(self):
        ds = synthetic_dataset()
        t, j = ds.start_idx + 3, ds.stocks.col("FLAT")
        adv = np.full(ds.stocks.close.shape, 1e9)
        adv[t, j] = 1e6
        px = 20_000.0
        costs = {}
        for at_open in (True, False):
            _, eng = self._engine(adv)
            pos = Position(ticker="FLAT", col=j, qty=10, entry_price=px, entry_idx=t - 1, stop_price=None,
                           entry_costs=TradeCost(), last_close=px)
            eng.positions["FLAT"] = pos
            eng._execute_sell(t, pos, 10, px, "x", at_open=at_open)
            costs[at_open] = eng.trades[-1].cost.slippage
        base = CostModel().slippage_rate
        self.assertAlmostEqual(costs[True], 200_000 * (base + 0.1 * 200_000 / 1e9))
        self.assertAlmostEqual(costs[False], 200_000 * (base + 0.1 * 200_000 / 1e6))

    def test_pending_orders_fill_with_the_open_time_adv(self):
        ds = synthetic_dataset()
        t, j = ds.start_idx + 3, ds.stocks.col("FLAT")
        slips = []
        for adv_today in (1e6, 1e12):
            adv = np.full(ds.stocks.close.shape, 1e9)
            adv[t, j] = adv_today
            _, eng = self._engine(adv)
            eng.pending = [Order("FLAT", "buy", 5, "entry")]
            eng._fill(t)
            slips.append(eng.positions["FLAT"].entry_costs.slippage)
        self.assertEqual(slips[0], slips[1])

    def test_first_row_has_no_liquidity_term(self):
        ds = synthetic_dataset()
        adv = np.full(ds.stocks.close.shape, 1.0)             # absurdly thin: would explode the cost if used
        cm = CostModel(slippage_liquidity_coeff=0.1, round_to_tick=False)
        eng = Engine(ds.stocks, ds.rf_period, StrategyParams(), cm, 0, "x", adv=adv)
        self.assertIsNone(eng._adv_open(0, 0))
        eng._execute_buy(0, "FLAT", 1, 20_000.0, "entry", None)
        self.assertAlmostEqual(eng.positions["FLAT"].entry_costs.slippage, 20_000.0 * cm.slippage_rate)

    def test_default_coefficient_is_unaffected(self):
        ds = synthetic_dataset()
        adv = np.full(ds.stocks.close.shape, 1e9)
        eng = Engine(ds.stocks, ds.rf_period, StrategyParams(), CostModel(), ds.start_idx, "x", adv=adv)
        t = ds.start_idx + 3
        eng._execute_buy(t, "FLAT", 1, 20_000.0, "entry", None)
        self.assertAlmostEqual(eng.positions["FLAT"].entry_costs.slippage, 20_000.0 * CostModel().slippage_rate)


# ---------------------------------------------------------------------------
# C5
# ---------------------------------------------------------------------------
class TestSnapshotRefusesWhatItCannotStore(unittest.TestCase):
    def test_several_market_dataset_is_refused_before_anything_is_written(self):
        ds = _two_market_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "snap")
            with self.assertRaises(UnsupportedSnapshotError) as ctx:
                save_snapshot(ds, target)
            self.assertFalse(os.path.exists(target))
        msg = str(ctx.exception)
        self.assertIn("per-market indices", msg)
        self.assertIn("2 benchmark ETFs", msg)
        self.assertIn("NASDAQ", msg)

    def test_us_benchmark_alone_is_refused(self):
        ds = synthetic_dataset()
        ds.bm.markets[ds.bm.tickers[0]] = "NASDAQ"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(UnsupportedSnapshotError):
            save_snapshot(ds, os.path.join(tmp, "snap"))

    def test_kospi_dataset_still_round_trips(self):
        ds = synthetic_dataset(with_flows=True)
        with tempfile.TemporaryDirectory() as tmp:
            man = save_snapshot(ds, os.path.join(tmp, "snap"))
            back = load_snapshot(os.path.join(tmp, "snap"))
        np.testing.assert_array_equal(back.stocks.close, ds.stocks.close)
        self.assertEqual(back.bm.tickers, ds.bm.tickers)
        self.assertEqual(man["tickers"], ds.stocks.tickers)

    def test_load_rejects_arrays_wider_than_the_manifest(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "snap")
            save_snapshot(ds, folder)
            f = os.path.join(folder, "bm_close.parquet")
            one = pl.read_parquet(f)
            one.with_columns(one.get_column(one.columns[0]).alias("EXTRA")).write_parquet(f)
            with self.assertRaises(SnapshotIntegrityError):
                load_snapshot(folder, verify=False)


if __name__ == "__main__":
    unittest.main()
