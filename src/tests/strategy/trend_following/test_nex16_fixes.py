"""NEX-16 (V1 follow-up): ETF/ETN exclusion, zero-volume sessions, universe impact
header, delisting vs. halted rows, fixed-list snapshot rebuild. Synthetic data, no network."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import polars as pl

from strategy.trend_following import run as run_mod
from strategy.trend_following.backtest import run_backtest
from strategy.trend_following.config import StrategyParams, VARIANTS
from strategy.trend_following.costs import CostModel
from strategy.trend_following.dataset import (
    is_etf_name, load_dataset, select_stock_universe, universe_impact,
)
from strategy.trend_following.signals import compute_features
from strategy.trend_following.snapshot import read_manifest, save_snapshot
from tests.strategy.trend_following.helpers import business_days, flow_frame, make_frame, path, synthetic_dataset


def _run(ds, vid="A0"):
    p = StrategyParams()
    return run_backtest(ds, compute_features(ds, p), p, VARIANTS[vid], CostModel())


def _first(res, ticker, reason=None):
    return next(t for t in res.trades if t.ticker == ticker and (reason is None or t.reason == reason))


class TestEtfExclusion(unittest.TestCase):
    def test_name_rules(self):
        for n in ("KODEX 200", "TIGER 미국S&P500", "RISE 200", "ACE KRX금현물", "TIME 미국나스닥100액티브",
                  "삼성 인덱스 레버리지 WTI원유 선물 ETN", "KODEX CD금리액티브(합성)", "PLUS 고배당주"):
            self.assertTrue(is_etf_name(n), n)
        for n in ("삼성전자", "SK하이닉스", "맥쿼리인프라", "HK이노엔", "POSCO홀딩스", "ACEPLACE", "SOLUM", "", None):
            self.assertFalse(is_etf_name(n), n)

    def test_selection_keeps_size_and_order(self):
        rows = [{"Code": f"{i:06d}", "Name": nm} for i, nm in enumerate(
            ["AAA", "KODEX 200", "BBB", "TIGER 200", "CCC", "신한 ETN", "DDD", "EEE"])]
        kept, ex = select_stock_universe(rows, 3)
        self.assertEqual([r["Name"] for r in kept], ["AAA", "BBB", "CCC"])
        self.assertEqual([r["Name"] for r in ex], ["KODEX 200", "TIGER 200"])   # only those above the cut
        kept, _ = select_stock_universe(rows, 10)
        self.assertEqual(len(kept), 5)

    def test_load_dataset_drops_etfs_and_refills_from_the_pool(self):
        n = 400
        dates = business_days(date(2022, 1, 3), n)
        closes = path(10_000.0, [(n, 0.001)])
        listing = [{"Code": c, "Name": nm} for c, nm in [
            ("000001", "AAA"), ("069500", "KODEX 200"), ("000002", "BBB"), ("122630", "KODEX 레버리지"),
            ("000003", "CCC"), ("500001", "신한 ETN"), ("000004", "DDD")]]
        asked = []

        def fake_listing(k):
            asked.append(k)
            return listing

        base = "strategy.trend_following.dataset."
        with patch(base + "get_historical_data", side_effect=lambda c, s, *a, **k: make_frame(dates, closes)), \
                patch(base + "_load_listing", side_effect=fake_listing), \
                patch(base + "get_cd91_series", return_value=pl.DataFrame({"Date": dates, "Rate": np.full(n, 3.0)})):
            ds = load_dataset(date(2022, 9, 1), universe_size=3, min_rows=10)
        self.assertEqual(ds.stocks.tickers, ["000001", "000002", "000003"])
        self.assertGreater(asked[0], 3)                       # asked for a larger pool
        self.assertEqual({r["Code"] for r in ds.info["excluded_etf"]}, {"069500", "122630"})   # the ETN ranks below the cut


class TestZeroVolumeSessions(unittest.TestCase):
    def test_traded_flag_and_candidate_exclusion(self):
        ds = synthetic_dataset()
        t0 = ds.start_idx
        j = ds.stocks.col("UP")
        ds.stocks.volume[t0, j] = 0.0
        feats = compute_features(ds, StrategyParams())
        self.assertFalse(feats.traded[t0, j])
        self.assertTrue(feats.traded[t0 + 1, j])
        # the breakout decision of the halted day is skipped: entry moves one session later
        res = _run(ds)
        self.assertEqual(_first(res, "UP").entry_date, ds.dates[t0 + 2])

    def test_entry_is_not_filled_on_a_zero_volume_open_and_retried(self):
        base = _run(synthetic_dataset())
        ds = synthetic_dataset()
        t0 = ds.start_idx
        self.assertEqual(_first(base, "UP").entry_date, ds.dates[t0 + 1])
        ds.stocks.volume[t0 + 1, ds.stocks.col("UP")] = 0.0         # open > 0, volume 0
        self.assertGreater(ds.stocks.open[t0 + 1, ds.stocks.col("UP")], 0)
        res = _run(ds)
        self.assertEqual(_first(res, "UP").entry_date, ds.dates[t0 + 2])

    def test_exit_is_not_filled_on_a_zero_volume_open_and_retried(self):
        base = _run(synthetic_dataset())
        ex = _first(base, "SPIKE", "channel")
        ds = synthetic_dataset()
        k = ds.dates.index(ex.exit_date)
        ds.stocks.volume[k, ds.stocks.col("SPIKE")] = 0.0
        res = _run(ds)
        late = _first(res, "SPIKE", "channel")
        self.assertEqual(late.exit_date, ds.dates[k + 1])


class TestDelistVsHalt(unittest.TestCase):
    def _cut(self, ds, k):
        for f in ("open", "high", "low", "close", "volume"):
            getattr(ds.stocks, f)[k:, ds.stocks.col("UP")] = np.nan

    def test_missing_bars_close_the_position_after_the_threshold(self):
        ds = synthetic_dataset()
        k = ds.start_idx + 100
        self._cut(ds, k)
        n = StrategyParams().delist_after_missing
        res = _run(ds)
        tr = _first(res, "UP", "delisted")
        self.assertEqual(tr.exit_date, ds.dates[k + n - 1])
        last_close = float(ds.stocks.close[k - 1, ds.stocks.col("UP")])
        self.assertAlmostEqual(tr.exit_price, CostModel().fill_price(tr.exit_date, "KOSPI", last_close, "sell"))
        self.assertLessEqual(tr.exit_price, last_close)               # tick rounding goes against the trader
        self.assertNotIn("UP", {p["ticker"] for p in res.open_positions})

    def test_halted_rows_with_a_price_do_not_count_as_missing(self):
        ds = synthetic_dataset()
        k = ds.start_idx + 100
        j = ds.stocks.col("UP")
        ds.stocks.volume[k:k + 25, j] = 0.0                           # price present, volume 0 for 25 sessions
        res = _run(ds)
        self.assertFalse([t for t in res.trades if t.ticker == "UP" and t.reason == "delisted"])
        held = [p for p in res.open_positions if p["ticker"] == "UP"]
        self.assertTrue(held or [t for t in res.trades if t.ticker == "UP"])


class TestUniverseImpactHeader(unittest.TestCase):
    def test_impact_counts(self):
        ds = synthetic_dataset()
        b = ds.stocks
        b.close[ds.start_idx + 200:, b.col("FLAT")] = np.nan          # data ends mid-window
        b.close[:ds.start_idx + 50, b.col("SPIKE")] = np.nan          # first bar after the start
        imp = universe_impact(b, ds.start_idx)
        self.assertEqual(imp["ended_in_window"], ["FLAT"])
        self.assertEqual(imp["listed_after_start"], ["SPIKE"])
        self.assertEqual(universe_impact(synthetic_dataset().stocks, ds.start_idx),
                         {"ended_in_window": [], "listed_after_start": []})

    def test_header_carries_the_numbers_and_the_warning(self):
        ds = synthetic_dataset()
        ds.info.update(ended_in_window=["A", "B"], listed_after_start=["C"],
                       excluded_etf=[{"Code": "069500", "Name": "KODEX 200"}] * 4, universe_built_on="2026-10-14")
        with tempfile.TemporaryDirectory() as tmp:
            man = save_snapshot(ds, Path(tmp) / "s")
            head = "\n".join(run_mod.report_header(man, "abc"))
        self.assertIn("상장폐지·데이터 종료 종목 2개", head)
        self.assertIn("신규 상장 종목 1개", head)
        self.assertIn("제외된 ETF/ETN 4개", head)
        self.assertIn("시가총액 정보가 쓰였다", head)
        self.assertIn("2026-10-14", head)
        self.assertNotIn("크기 추정 불가", head)

    def test_header_of_an_old_manifest_says_unrecorded(self):
        ds = synthetic_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            man = save_snapshot(ds, Path(tmp) / "s")
        man["info"] = {}
        head = "\n".join(run_mod.report_header(man, "abc"))
        self.assertIn("미기록", head)


class TestFixedListRebuild(unittest.TestCase):
    N = 500
    DATES = business_days(date(2022, 1, 3), N)
    CLOSES = path(10_000.0, [(N, 0.001)])

    def _patches(self, listing):
        base = "strategy.trend_following.dataset."
        d, c = self.DATES, self.CLOSES
        return (
            patch(base + "get_historical_data",
                  side_effect=lambda code, s, *a, **k: make_frame(d, c * (1.0 + int(code[-1]) / 10.0))),
            patch(base + "_load_listing", return_value=listing),
            patch(base + "get_investor_flows", side_effect=lambda code, s: flow_frame(d, c, [(self.N, 100.0)])),
            patch(base + "get_cd91_series", return_value=pl.DataFrame({"Date": d, "Rate": np.full(self.N, 3.0)})),
        )

    def test_same_list_gives_the_same_hash(self):
        listing = [{"Code": c, "Name": nm} for c, nm in [
            ("000001", "AAA"), ("069500", "KODEX 200"), ("000002", "BBB"), ("000003", "CCC"), ("000004", "DDD")]]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ps = self._patches(listing)
            for p in ps:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in ps])
            with patch.object(run_mod, "default_snapshot_dir", side_effect=lambda n: root / n):
                m1 = run_mod.build_snapshot("first", start=date(2023, 3, 1), universe_size=3)
                # the live ranking changes (new listing); the pinned list must not follow it
                with patch("strategy.trend_following.dataset._load_listing",
                           return_value=[{"Code": "000009", "Name": "ZZZ"}]) as live:
                    m2 = run_mod.build_snapshot("second", start=date(2023, 3, 1), universe_size=3,
                                                tickers_from=root / "first")
                    live.assert_not_called()
            self.assertEqual(m1["tickers"], ["000001", "000002", "000003"])
            self.assertEqual(m2["tickers"], m1["tickers"])
            self.assertEqual(m2["sha256"], m1["sha256"])
            self.assertEqual(read_manifest(root / "second")["info"]["excluded_etf"], m1["info"]["excluded_etf"])
            self.assertEqual(len(m2["info"]["excluded_etf"]), 1)
            self.assertTrue(m2["info"]["universe_fixed_from"].endswith("first"))
            self.assertEqual(m1["info"]["universe_fixed_from"], "")


if __name__ == "__main__":
    unittest.main()
