"""data.price_crosscheck (KIS vs Naver daily bars) and the adjusted switch of data.raw_prices. No network."""
import unittest
from datetime import date, datetime
from unittest.mock import patch

import polars as pl

from data import raw_prices
from data.price_crosscheck import CrossCheck, compare_frames, render_markdown


def _frame(rows):
    return pl.DataFrame([{"Date": d, "Open": c, "High": c, "Low": c, "Close": c, "Volume": v} for d, c, v in rows])


D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


class TestCompare(unittest.TestCase):
    def test_identical_is_clean(self):
        f = _frame([(D1, 100, 10), (D2, 101, 11)])
        res = compare_frames("A", f, f, D1)
        self.assertTrue(res.clean)
        self.assertEqual(res.common, 2)

    def test_price_gap_above_tolerance_reports_worst_day(self):
        res = compare_frames("A", _frame([(D1, 100, 10), (D2, 110, 10)]), _frame([(D1, 100, 10), (D2, 100, 10)]), D1)
        self.assertFalse(res.clean)
        self.assertEqual(res.diffs["Close"].mismatches, 1)
        self.assertEqual(res.diffs["Close"].worst_date, D2)
        self.assertAlmostEqual(res.diffs["Close"].max_pct, 10.0)

    def test_gap_within_tolerance_is_clean_but_still_measured(self):
        res = compare_frames("A", _frame([(D1, 1001, 10)]), _frame([(D1, 1000, 10)]), D1)
        self.assertTrue(res.clean)
        self.assertAlmostEqual(res.diffs["Close"].max_pct, 0.1)

    def test_one_sided_dates_and_datetime_dates(self):
        ref = _frame([(datetime(2024, 1, 2), 100, 10), (datetime(2024, 1, 4), 100, 10)])
        res = compare_frames("A", _frame([(D1, 100, 10), (D2, 100, 10)]), ref, D1)
        self.assertEqual((res.only_kis, res.only_ref, res.common), ([D2], [D3], 1))
        self.assertFalse(res.clean)

    def test_zero_reference_volume_is_a_mismatch_not_a_crash(self):
        res = compare_frames("A", _frame([(D1, 100, 5)]), _frame([(D1, 100, 0)]), D1)
        self.assertEqual(res.diffs["Volume"].mismatches, 1)
        self.assertTrue(compare_frames("A", _frame([(D1, 100, 0)]), _frame([(D1, 100, 0)]), D1).clean)

    def test_holdout_rows_are_never_compared_even_if_asked_for(self):
        hold = date(2025, 1, 2)
        res = compare_frames("A", _frame([(D1, 100, 10), (hold, 999, 1)]), _frame([(D1, 100, 10), (hold, 1, 1)]),
                             D1, end=date(2025, 12, 31))
        self.assertTrue(res.clean)
        self.assertEqual(res.common, 1)

    def test_halted_day_compares_only_the_close(self):
        kis = pl.DataFrame([{"Date": D1, "Open": 100, "High": 100, "Low": 100, "Close": 100, "Volume": 0}])
        naver = pl.DataFrame([{"Date": D1, "Open": 0.0, "High": 0.0, "Low": 0.0, "Close": 100.0, "Volume": 0.0}])
        self.assertTrue(compare_frames("A", kis, naver, D1).clean)
        moved = pl.DataFrame([{"Date": D1, "Open": 0.0, "High": 0.0, "Low": 0.0, "Close": 90.0, "Volume": 0.0}])
        self.assertEqual(compare_frames("A", kis, moved, D1).diffs["Close"].mismatches, 1)

    def test_no_data_is_not_clean(self):
        self.assertFalse(compare_frames("A", None, None, D1).clean)

    def test_report_lists_failures_and_counts(self):
        f = _frame([(D1, 100, 10)])
        md = render_markdown([compare_frames("A", f, f, D1), CrossCheck("B", error="boom")], D1, D3)
        self.assertIn("일치: 1 / 2종목", md)
        self.assertIn("조회 실패: boom", md)

    def test_report_separates_a_volume_only_gap(self):
        res = compare_frames("A", _frame([(D1, 100, 50)]), _frame([(D1, 100, 10)]), D1)
        self.assertIn("거래량만 차이", render_markdown([res], D1, D3))


class TestAdjustedSwitch(unittest.TestCase):
    def _flags(self, fn):
        seen = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"rt_cd": "0", "output2": []}

        def fake_get(url, headers, params, timeout):
            seen.append(params["FID_ORG_ADJ_PRC"])
            return Resp()

        with patch.object(raw_prices, "_kis_headers", return_value={}), \
                patch.object(raw_prices._KIS_SESSION, "get", side_effect=fake_get):
            fn("005930", D1, D3)
        return seen

    def test_raw_and_adjusted_send_opposite_flags(self):
        self.assertEqual(self._flags(raw_prices.get_raw_daily_prices), ["1"])
        self.assertEqual(self._flags(raw_prices.get_adjusted_daily_prices), ["0"])
