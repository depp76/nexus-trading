"""NEX-19 (A5): pure parts of the unadjusted-price fetch (data.raw_prices) and of the
adjusted-vs-raw comparison (raw_check). No network."""
import unittest
from datetime import date

import numpy as np

from data.raw_prices import RAW_DEV_END, parse_kis_daily_rows, rows_to_frame
from strategy.trend_following.raw_check import adjustment_events, compare_ticker, pick_sample, to_markdown
from tests.strategy.trend_following.helpers import business_days


def _item(d, o, h, lo, c, v, val=0):
    return {"stck_bsop_date": d, "stck_oprc": str(o), "stck_hgpr": str(h), "stck_lwpr": str(lo),
            "stck_clpr": str(c), "acml_vol": str(v), "acml_tr_pbmn": str(val)}


class TestParseKisRows(unittest.TestCase):
    def test_sorted_deduped_and_holdout_rows_dropped(self):
        items = [_item("20250102", 10, 11, 9, 10, 1), _item("20241231", 100, 110, 90, 105, 5, 500),
                 _item("20241230", 100, 110, 90, 100, 4), _item("20241231", 101, 110, 90, 106, 6)]
        rows = parse_kis_daily_rows(items)
        self.assertEqual([r["Date"] for r in rows], [date(2024, 12, 30), date(2024, 12, 31)])
        self.assertEqual(rows[1]["Close"], 106)                     # last duplicate wins
        self.assertTrue(all(r["Date"] <= RAW_DEV_END for r in rows))

    def test_bad_and_blank_rows_are_skipped(self):
        items = [_item("20240102", 0, 0, 0, 0, 0), {"stck_bsop_date": "x"}, {}, _item("20240103", 5, 6, 4, 5, 9)]
        rows = parse_kis_daily_rows(items)
        self.assertEqual([r["Date"] for r in rows], [date(2024, 1, 3)])

    def test_frame_schema_and_empty(self):
        fr = rows_to_frame(parse_kis_daily_rows([_item("20240103", 5, 6, 4, 5, 9, 45)]))
        self.assertEqual(fr.columns, ["Date", "Open", "High", "Low", "Close", "Volume", "Value"])
        self.assertEqual(fr.row(0)[-1], 45)
        self.assertEqual(rows_to_frame([]).height, 0)

    def test_max_date_cannot_be_raised_past_the_default_by_data_in_the_payload(self):
        self.assertEqual(parse_kis_daily_rows([_item("20250101", 5, 6, 4, 5, 9)]), [])


class TestComparison(unittest.TestCase):
    """A 10:1 split on session 30: raw closes 100,000 before / 10,000 after; adjusted is 10,000 throughout."""

    def setUp(self):
        self.dates = business_days(date(2022, 1, 3), 60)
        n = len(self.dates)
        self.adj_close = np.full(n, 10_000.0)
        self.raw_close = np.where(np.arange(n) < 30, 100_000.0, 10_000.0)
        self.vol_adj = np.full(n, 2_000_000.0)       # adjusted volume would be 10x the raw volume pre-split
        self.raw_vol = np.where(np.arange(n) < 30, 200_000.0, 2_000_000.0)
        self.raw_rows = {d: (self.raw_close[i], self.raw_close[i], self.raw_close[i], self.raw_close[i], self.raw_vol[i])
                         for i, d in enumerate(self.dates)}

    def test_event_detected(self):
        ev = adjustment_events(self.dates, self.adj_close, self.raw_close)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][0], self.dates[30])
        self.assertAlmostEqual(ev[0][1], 0.1)

    def test_rule_differences(self):
        adj = {"open": self.adj_close.copy(), "close": self.adj_close, "volume": self.vol_adj}
        r = compare_ticker(self.dates, "KOSPI", adj, self.raw_rows)
        self.assertEqual(r["sessions"], 60)
        self.assertAlmostEqual(r["ratio_max"], 10.0)
        # 20% slot = 2,000,000 KRW: 200 shares at 10,000, 20 at 100,000 -> differs on the 30 pre-split sessions
        self.assertAlmostEqual(r["qty_differs"], 0.5)
        self.assertEqual(r["raw_unaffordable"], 0.0)
        # trading value 10,000 x 2,000,000 = 2e10 (>= 1e10) vs raw 100,000 x 200,000 = 2e10: same flag here
        self.assertEqual(r["liquid_differs"], 0.0)
        self.assertEqual(r["limit_differs"], 0)

    def test_unadjusted_volume_changes_the_liquidity_flag(self):
        adj = {"open": self.adj_close.copy(), "close": self.adj_close, "volume": np.full(60, 4_000_000.0)}
        rows = {d: (c, c, c, c, v) for d, c, v in zip(self.dates, self.raw_close, np.full(60, 100_000.0))}
        r = compare_ticker(self.dates, "KOSPI", adj, rows)
        self.assertGreater(r["liquid_differs"], 0.0)          # adjusted 4e10 vs raw 1e9..1e10 pre/post split
        self.assertGreater(r["liquid_adj"], r["liquid_raw"])

    def test_off_grid_adjusted_open_is_counted(self):
        adj_open = self.adj_close + 3.0                        # 10,003 is off the 50-won grid, raw 10,000 is on it
        adj = {"open": adj_open, "close": self.adj_close, "volume": self.vol_adj}
        raw_rows = {d: (self.adj_close[i],) * 4 + (self.raw_vol[i],) for i, d in enumerate(self.dates)}
        r = compare_ticker(self.dates, "KOSPI", adj, raw_rows)
        self.assertEqual(r["adj_open_off_grid"], 1.0)
        self.assertEqual(r["raw_open_off_grid"], 0.0)

    def test_window_and_missing_rows(self):
        adj = {"open": self.adj_close.copy(), "close": self.adj_close, "volume": self.vol_adj}
        rows = {d: v for d, v in list(self.raw_rows.items())[:40]}
        r = compare_ticker(self.dates, "KOSPI", adj, rows, start=self.dates[10], end=self.dates[39])
        self.assertEqual(r["sessions"], 30)

    def test_sample_and_markdown(self):
        adj = {"open": self.adj_close.copy(), "close": self.adj_close, "volume": self.vol_adj}
        res = {"AAA": compare_ticker(self.dates, "KOSPI", adj, self.raw_rows),
               "BBB": compare_ticker(self.dates, "KOSPI", adj, {d: (10_000.0,) * 5 for d in self.dates})}
        self.assertEqual(pick_sample(res), ["AAA"])
        md = to_markdown(res, {"AAA": "Alpha"}, ["AAA"], 2, "snap", "test")
        self.assertIn("AAA Alpha", md)
        self.assertIn("×0.1", md)


if __name__ == "__main__":
    unittest.main()
