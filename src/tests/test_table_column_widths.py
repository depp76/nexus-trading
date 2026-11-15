"""tests/test_table_column_widths.py — toolbar filters must not resize columns
(2026-11-15). Both grids size their columns from the viewport width; with an
as-needed vertical scroll bar that width changed whenever a filter changed the
row count, so Trading History's Company column and every Universe flex column
jumped by the bar's width."""
import datetime as dt
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])


def _widths(table):
    app.processEvents()
    return [table.columnWidth(c) for c in range(table.columnCount())]


class TestHistoryColumnsStayPut(unittest.TestCase):
    def setUp(self):
        from ui.history_tab import TradingHistoryTab
        self.tab = TradingHistoryTab()
        self.addCleanup(self.tab._settings_save_timer.stop)
        today = dt.date.today()

        def rec(i, closed):
            r = {"orig_key": f"k{i}", "company": f"Company {i}", "ticker": f"{i:06d}", "market": "KOSPI",
                 "buy_date": (today - dt.timedelta(days=7 * i)).isoformat(), "buy_price": 1000.0, "qty": 10.0,
                 "buy_amount": 10000.0, "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0,
                 "curr_price": 1100.0, "wk1": 1.0, "wk2": -2.0, "mth1": 3.0, "curr_pl": 1000.0, "curr_pl_pct": 10.0}
            if closed:
                r.update(sell_date=(today - dt.timedelta(days=7 * i - 3)).isoformat(), sell_price=1200.0,
                         sell_qty=10.0, sell_amount=12000.0)
            TradingHistoryTab._compute_pl_fields(r)
            return r

        self.tab._closed_data = [rec(i, True) for i in range(1, 50)]
        self.tab._open_data = [rec(i, False) for i in range(50, 55)]
        self.tab.resize(1900, 700)
        self.tab.show()
        self.addCleanup(self.tab.close)
        app.processEvents()
        self.tab._apply_filter()

    def test_period_and_grouping_buttons_keep_the_widths(self):
        t = self.tab._table
        base = _widths(t)
        self.assertGreater(t.rowCount(), 30)
        for period in ("1M", "3M", "YTD", "All"):
            self.tab._on_period_filter_changed(period)
            self.assertEqual(_widths(t), base, period)
        self.tab._sort_by_date = True
        self.tab._apply_filter()
        self.assertEqual(_widths(t), base, "by month")
        self.tab._on_stale_toggle_changed(not self.tab._hide_stale_closed)
        self.assertEqual(_widths(t), base, "stale toggle")

    def test_no_horizontal_scroll_when_the_columns_fit(self):
        t = self.tab._table
        self.tab._on_period_filter_changed("1M")      # few rows
        self.tab._on_period_filter_changed("All")     # many rows
        app.processEvents()
        self.assertLessEqual(sum(_widths(t)), t.viewport().width())
        self.assertFalse(t.horizontalScrollBar().isVisible())


class TestUniverseColumnsStayPut(unittest.TestCase):
    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=lambda _p, default=None: default):
            self.tab = UniverseTab()
        self.tab.custom_settings = {"added": [], "deleted": []}
        self.tab.all_data = [
            {"ticker": f"{i:06d}", "name": f"Name{i}", "market": "KOSPI", "market_cap": 1000 - i, "price": 1000.0,
             "change_pct": 0.5, "per": 10.0, "fper": 9.0, "changes": {"3D": 1.0, "10D": 2.0, "20D": 3.0}}
            for i in range(60)]
        self.tab.resize(1900, 700)
        self.tab.show()
        self.addCleanup(self.tab.close)
        app.processEvents()
        self.tab._reload_table()

    def test_search_filter_keeps_the_widths(self):
        t = self.tab.table
        base = _widths(t)
        for text in ("Name1", "Name12", "nothing matches", ""):
            self.tab.search_input.setText(text)
            self.assertEqual(_widths(t), base, text)


if __name__ == "__main__":
    unittest.main()
