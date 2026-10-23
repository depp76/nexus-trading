"""tests/test_assets_report.py — the Total Assets early/mid-month report
(ui/assets_report.py maths + AssetsReportDialog smoke)."""
import unittest

from PyQt6.QtWidgets import QApplication

from ui.assets_report import report_dates, holdings_on, cash_on, close_on, build_report

app = QApplication.instance() or QApplication([])


def _trade(ticker, bd, qty, ba, sd="", sa=0.0, market="KOSPI"):
    return {"company": ticker.upper(), "market": market, "ticker": ticker, "buy_date": bd,
            "qty": qty, "buy_amount": ba, "sell_date": sd, "sell_amount": sa}


TRADES = [
    _trade("aaa", "2026-01-02", 10, 1000.0, "2026-01-20", 1500.0),
    _trade("bbb", "2026-01-14", 5, 500.0),
    _trade("qqq", "2026-02-02", 2, 200.0, market="US"),
]
# Cash after the last trade: 10,000 start - 1000 - 500 + 1500 - 200
END_CASH = 9800.0


class TestReportDates(unittest.TestCase):

    def test_first_day_and_last_day_on_or_before_15th(self):
        days = ["2026-01-02", "2026-01-05", "2026-01-15", "2026-01-16",
                "2026-02-02", "2026-02-13", "2026-02-16"]
        self.assertEqual(report_dates(days, "2026-01-01", "2026-12-31"), [
            ("2026-01-02", "early"), ("2026-01-15", "mid"),
            ("2026-02-02", "early"), ("2026-02-13", "mid"),
        ])

    def test_range_bounds_and_no_duplicate_mid(self):
        days = ["2026-03-13", "2026-03-16", "2026-04-01"]
        # March starts on the 13th here, so its mid point is the same day.
        self.assertEqual(report_dates(days, "2026-03-01", "2026-03-31"), [("2026-03-13", "early")])


class TestPositionsAndCash(unittest.TestCase):

    def test_holdings_include_same_day_buys_and_drop_same_day_sells(self):
        self.assertEqual(set(holdings_on(TRADES, "2026-01-14")), {"aaa", "bbb"})
        self.assertEqual(set(holdings_on(TRADES, "2026-01-20")), {"bbb"})
        self.assertEqual(holdings_on(TRADES, "2026-01-14")["bbb"]["cost"], 500.0)

    def test_cash_rolls_back_from_the_anchor(self):
        self.assertEqual(cash_on(TRADES, END_CASH, "2026-12-31"), 9800.0)
        self.assertEqual(cash_on(TRADES, END_CASH, "2026-01-20"), 10000.0)
        self.assertEqual(cash_on(TRADES, END_CASH, "2026-01-14"), 8500.0)
        self.assertEqual(cash_on(TRADES, END_CASH, "2026-01-01"), 10000.0)

    def test_close_on_uses_last_close_at_or_before(self):
        series = [("2026-01-02", 1.0), ("2026-01-05", 2.0)]
        self.assertEqual(close_on(series, "2026-01-04"), 1.0)
        self.assertEqual(close_on(series, "2026-01-05"), 2.0)
        self.assertEqual(close_on(series, "2026-01-01"), 0.0)


class TestBuildReport(unittest.TestCase):

    def test_values_cash_kospi_and_us_conversion(self):
        closes = {"aaa": [("2026-01-02", 120.0)], "bbb": [("2026-01-14", 110.0)],
                  "qqq": [("2026-02-02", 1.0)]}
        kospi = [("2026-01-14", 4000.0), ("2026-02-02", 4400.0)]
        points = [("2026-01-14", "mid"), ("2026-02-02", "early")]
        rows = build_report(TRADES, END_CASH, points, closes, kospi, {"2026-02-02": 1500.0})
        r0, r1 = rows
        self.assertEqual(r0["value"], 10 * 120.0 + 5 * 110.0)
        self.assertEqual(r0["cash"], 8500.0)
        self.assertEqual(r0["total"], 1750.0 + 8500.0)
        self.assertEqual(r0["kospi_cum_pct"], 0.0)
        self.assertAlmostEqual(r1["kospi_cum_pct"], 10.0)
        qqq = next(h for h in r1["holdings"] if h["ticker"] == "qqq")
        self.assertEqual(qqq["value"], 2 * 1500.0)
        self.assertTrue(r1["priced"])

    def test_missing_close_falls_back_to_cost(self):
        rows = build_report(TRADES, END_CASH, [("2026-01-14", "mid")], {}, [("2026-01-14", 1.0)], {})
        self.assertEqual(rows[0]["value"], 1500.0)
        self.assertFalse(rows[0]["priced"])


class TestReportDialog(unittest.TestCase):

    def test_dialog_fills_summary_and_holdings(self):
        from ui.dialogs import AssetsReportDialog
        closes = {"aaa": [("2026-01-02", 120.0)], "bbb": [("2026-01-14", 110.0)]}
        rows = build_report(TRADES, END_CASH, [("2026-01-02", "early"), ("2026-01-14", "mid")],
                            closes, [("2026-01-02", 4000.0)], {})
        dlg = AssetsReportDialog(rows)
        self.assertEqual(dlg.summary.rowCount(), 2)
        dlg.summary.selectRow(0)
        self.assertEqual(dlg.holdings.rowCount(), 1)
        self.assertEqual(dlg.holdings.item(0, 1).text(), "aaa")
        dlg.summary.selectRow(1)
        self.assertEqual(dlg.holdings.rowCount(), 2)


if __name__ == "__main__":
    unittest.main()
