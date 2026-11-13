"""Spec 9-25: the universe drawn from several markets (KOSPI, KOSDAQ, NASDAQ,
NYSE) ranked together by KRW market cap; US prices in KRW, each name's L1 /
RS on its own market's index, US costs, an ETF-mix BM1/BM3. KOSPI alone keeps
the original single-market path."""
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
import polars as pl

from strategy.trend_following.config import StrategyParams, normalize_markets
from strategy.trend_following.costs import CostModel, tick_size
from strategy.trend_following.dataset import build_dataset
from strategy.trend_following.research import ResearchRequest, run_research
from strategy.trend_following.signals import compute_features
from tests.strategy.trend_following.helpers import business_days, make_frame, path, synthetic_dataset

FX = 1300.0


def _index_frame(dates, closes):
    return pl.DataFrame({"Date": dates, "Close": np.asarray(closes, dtype=float)})


def _two_market_dataset(kr_up=True, us_up=False, with_bm=True):
    """KOSPI + NASDAQ on a union calendar: Mondays the US is shut, Fridays Korea is shut."""
    days = business_days(date(2020, 1, 2), 700)
    kr_days = [d for d in days if d.weekday() != 4]
    us_days = [d for d in days if d.weekday() != 0]
    n_kr, n_us = len(kr_days), len(us_days)
    kr_idx = 2000.0 * np.cumprod(np.full(n_kr, 1.0005 if kr_up else 0.999))
    us_idx = 10000.0 * np.cumprod(np.full(n_us, 1.0005 if us_up else 0.999))
    frames = {
        "005930": make_frame(kr_days, path(50_000.0, [(n_kr, 0.003)])),
        "AAPL": make_frame(us_days, path(100.0, [(n_us, 0.003)])),
    }
    bm_frames = None
    if with_bm:
        bm_frames = {"278530": make_frame(kr_days, path(10_000.0, [(n_kr, 0.0005)])),
                     "QQQ": make_frame(us_days, path(300.0, [(n_us, 0.0005)]))}
    return build_dataset(
        days, None, frames, date(2021, 1, 4), {"005930": "KOSPI", "AAPL": "NASDAQ"},
        {"005930": "삼성전자", "AAPL": "Apple"},
        bm_frames=bm_frames, bm_markets={"278530": "ETF", "QQQ": "NASDAQ"},
        market_index_frames={"KOSPI": _index_frame(kr_days, kr_idx), "NASDAQ": _index_frame(us_days, us_idx)},
        fx_frame=pl.DataFrame({"Date": days, "Close": np.full(len(days), FX)}),
        info={"markets": ["KOSPI", "NASDAQ"]},
    )


class TestUsCosts(unittest.TestCase):
    def test_commission_sec_fee_no_tax_no_tick_no_limit(self):
        cm = CostModel()
        d = date(2024, 3, 4)
        buy = cm.buy_cost(d, "NASDAQ", 130_000.0, 10)
        self.assertAlmostEqual(buy.commission, 1_300_000 * cm.us_commission_rate)
        self.assertEqual(buy.tax, 0.0)
        sell = cm.sell_cost(d, "NYSE", 130_000.0, 10)
        self.assertAlmostEqual(sell.commission, 1_300_000 * (cm.us_commission_rate + cm.us_sec_fee_rate))
        self.assertEqual(sell.tax, 0.0)
        self.assertEqual(tick_size(d, "NASDAQ", 130_123.4), 0)
        self.assertEqual(cm.fill_price(d, "NASDAQ", 130_123.4, "buy"), 130_123.4)
        self.assertFalse(cm.is_limit_up(d, "NYSE", 200.0, 100.0))
        # Korean names keep the 4-1 model
        kr = cm.sell_cost(d, "KOSPI", 50_000.0, 10)
        self.assertAlmostEqual(kr.commission, 500_000 * cm.commission_rate)
        self.assertGreater(kr.tax, 0.0)


class TestMarketSelection(unittest.TestCase):
    def test_normalize_markets(self):
        self.assertEqual(normalize_markets(["nyse", "KOSPI", "x"]), ("KOSPI", "NYSE"))
        self.assertEqual(normalize_markets([]), ("KOSPI",))
        self.assertEqual(ResearchRequest(start=date(2021, 1, 4), markets=("NASDAQ", "KOSDAQ")).markets,
                         ("KOSDAQ", "NASDAQ"))
        self.assertEqual(ResearchRequest(start=date(2021, 1, 4)).markets, ("KOSPI",))


class TestMultiMarketDataset(unittest.TestCase):
    def setUp(self):
        self.ds = _two_market_dataset()

    def test_closed_days_repeat_the_last_close_with_zero_volume(self):
        b = self.ds.stocks
        us = b.col("AAPL")
        kr = b.col("005930")
        mon = next(i for i, d in enumerate(b.dates) if d.weekday() == 0 and i > 5)
        fri = next(i for i, d in enumerate(b.dates) if d.weekday() == 4 and i > 5)
        self.assertEqual(b.volume[mon, us], 0.0)
        self.assertEqual(b.close[mon, us], b.close[mon - 1, us])
        self.assertEqual(b.open[mon, us], b.close[mon - 1, us])
        self.assertEqual(b.volume[fri, kr], 0.0)
        self.assertEqual(b.close[fri, kr], b.close[fri - 1, kr])
        self.assertTrue(np.isfinite(b.close[10:, :]).all())    # no holes for the rolling windows

    def test_us_prices_are_in_krw(self):
        b = self.ds.stocks
        us = b.col("AAPL")
        tue = next(i for i, d in enumerate(b.dates) if d.weekday() == 1)
        self.assertGreater(b.close[tue, us], 100.0 * FX * 0.99)      # $100+ times 1300
        self.assertLess(b.close[tue, b.col("005930")], 100_000.0)    # KRW names untouched
        self.assertEqual(self.ds.bm.N, 2)
        self.assertGreater(self.ds.bm.close[tue, self.ds.bm.col("QQQ")], 300.0 * FX * 0.99)

    def test_each_name_uses_its_own_index(self):
        cols = self.ds.index_cols()
        kr, us = self.ds.stocks.col("005930"), self.ds.stocks.col("AAPL")
        np.testing.assert_array_equal(cols[:, kr], self.ds.index_by_market["KOSPI"])
        np.testing.assert_array_equal(cols[:, us], self.ds.index_by_market["NASDAQ"])
        self.assertEqual(self.ds.markets, ("KOSPI", "NASDAQ"))
        self.assertIn("KOSPI+NASDAQ", self.ds.info["universe"])

    def test_regime_per_market(self):
        f = compute_features(self.ds, StrategyParams())
        kr, us = self.ds.stocks.col("005930"), self.ds.stocks.col("AAPL")
        late = slice(400, None)
        self.assertTrue(f.regime_cols[late, kr].all())       # KOSPI rising
        self.assertFalse(f.regime_cols[late, us].any())      # NASDAQ falling
        self.assertTrue(f.regime_on[late].all())             # "any market on"

    def test_research_runs_with_the_etf_mix(self):
        res = run_research(self.ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=("A1",),
                                                    cost_multipliers=(1.0,), run_event_study=True,
                                                    markets=("KOSPI", "NASDAQ")))
        ids = {r.id for r in res.runs}
        self.assertTrue({"A1", "BM1", "BM2", "BM3", "BM4"} <= ids)
        a1 = next(r for r in res.runs if r.id == "A1")
        # only the Korean name trades: NASDAQ is risk-off all along
        self.assertTrue(a1.trades)
        self.assertTrue(all(t.ticker == "005930" for t in a1.trades))
        bm1 = next(r for r in res.runs if r.id == "BM1")
        self.assertTrue(np.isfinite(bm1.nav).all())
        md = res.to_markdown()
        self.assertIn("KOSPI+NASDAQ names", md)
        self.assertIn("Markets (spec 9-25)", md)
        self.assertEqual(res.dataset_info["names"]["AAPL"], "Apple")


class TestSingleMarketUnchanged(unittest.TestCase):
    def test_kospi_features_have_one_regime(self):
        ds = synthetic_dataset()
        self.assertIsNone(ds.index_by_market)
        f = compute_features(ds, StrategyParams())
        np.testing.assert_array_equal(f.regime_cols, np.repeat(f.regime_on[:, None], ds.stocks.N, axis=1))
        np.testing.assert_array_equal(ds.index_cols()[:, 0], ds.index_close)


class TestLoadMultiMarket(unittest.TestCase):
    def test_ranks_selected_markets_together_by_krw_cap(self):
        from strategy.trend_following import dataset as dsmod
        days = business_days(date(2019, 9, 2), 900)
        idx = _index_frame(days, np.linspace(2000, 2500, len(days)))

        def hist(ticker, _start):
            return idx if ticker in ("KS11", "^IXIC") else make_frame(days, path(100.0, [(len(days), 0.001)]))

        kr_rows = [{"Code": "5930", "Name": "삼성전자", "Marcap": 400e12},
                   {"Code": "000660", "Name": "SK하이닉스", "Marcap": 100e12}]
        us_rows = [{"Code": "AAPL", "Name": "Apple", "Marcap": 3e12},      # 3,900조 KRW at 1300
                   {"Code": "SMALL", "Name": "Small", "Marcap": 1e9}]      # 1.3조 KRW
        fx = pl.DataFrame({"Date": days, "Close": np.full(len(days), FX)})
        with patch.object(dsmod, "get_historical_data", side_effect=hist), \
             patch.object(dsmod, "_fetch_kr_listing_naver", return_value=kr_rows), \
             patch.object(dsmod, "get_us_listing_ranked", return_value=us_rows), \
             patch.object(dsmod, "get_usd_krw_history", return_value=fx), \
             patch.object(dsmod, "get_cd91_series", return_value=None), \
             patch.object(dsmod, "get_investor_flows") as flows:
            ds = dsmod.load_dataset(date(2021, 1, 4), date(2022, 12, 30), universe_size=3,
                                    include_flows=True, markets=("NASDAQ", "KOSPI"))
        self.assertEqual(ds.stocks.tickers, ["AAPL", "005930", "000660"])   # SMALL is 4th by KRW cap
        self.assertEqual(ds.stocks.market_of("AAPL"), "NASDAQ")
        self.assertEqual(ds.info["markets"], ["KOSPI", "NASDAQ"])
        self.assertEqual({c.args[0] for c in flows.call_args_list}, {"005930", "000660"})  # flows: KR only
        self.assertEqual(set(ds.index_by_market), {"KOSPI", "NASDAQ"})
        self.assertEqual(set(ds.bm.tickers), {"278530", "QQQ"})

    def test_kospi_alone_takes_the_original_path(self):
        from strategy.trend_following import dataset as dsmod
        with patch.object(dsmod, "_load_multi_market") as multi, \
             patch.object(dsmod, "get_historical_data", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                dsmod.load_dataset(date(2021, 1, 4), date(2022, 12, 30), markets=("KOSPI",))
        multi.assert_not_called()


if __name__ == "__main__":
    unittest.main()
