"""tests/test_history_toolbar.py — Trading History's one-row toolbar
(user direction 2026-11-16, modelled on Trading Universe; docs/ui.md 8.17):
live search over open and closed rows, three cycle buttons, one shared
button width, and the stock trade history moved to the right-click menu."""
import datetime as dt
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])


class TestHistoryToolbar(unittest.TestCase):
    def setUp(self):
        from ui.history_tab import TradingHistoryTab
        self.tab = TradingHistoryTab()
        self.addCleanup(self.tab._settings_save_timer.stop)
        today = dt.date.today()

        def rec(i, company, ticker, closed, days):
            r = {"orig_key": f"k{i}", "company": company, "ticker": ticker, "market": "KOSPI",
                 "buy_date": (today - dt.timedelta(days=days)).isoformat(), "buy_price": 1000.0, "qty": 10.0,
                 "buy_amount": 10000.0, "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0,
                 "curr_price": 1100.0, "wk1": 1.0, "wk2": -2.0, "mth1": 3.0, "curr_pl": 1000.0, "curr_pl_pct": 10.0}
            if closed:
                r.update(sell_date=(today - dt.timedelta(days=max(days - 3, 0))).isoformat(), sell_price=1200.0,
                         sell_qty=10.0, sell_amount=12000.0)
            TradingHistoryTab._compute_pl_fields(r)
            return r

        self.tab._closed_data = [rec(1, "Samsung", "005930", True, 200), rec(2, "Naver", "035420", True, 10)]
        self.tab._open_data = [rec(3, "Samsung", "005930", False, 5), rec(4, "Kakao", "035720", False, 100)]
        self.tab._apply_filter()

    def _companies(self):
        return sorted(rec.get("company") for kind, rec in self.tab._row_data if kind != "monthly")

    def test_search_filters_open_and_closed_rows_by_name_or_ticker(self):
        tab = self.tab
        tab._search_edit.setText("sams")
        self.assertEqual(self._companies(), ["Samsung", "Samsung"])
        tab._search_edit.setText("035720")
        self.assertEqual(self._companies(), ["Kakao"])
        tab._search_edit.clear()
        self.assertEqual(len(self._companies()), 4)

    def test_search_counts_the_korean_syllable_still_being_composed(self):
        """2026-11-18: a Korean IME keeps the last syllable in pre-edit (out of
        QLineEdit.text(), no textChanged), so "삼성전기" used to filter on "삼성전"
        and list 삼성전자 as well until Enter."""
        from PyQt6.QtGui import QInputMethodEvent
        tab = self.tab
        tab._open_data[0]["company"] = "삼성전자"
        tab._closed_data[1]["company"] = "삼성전기"
        tab._apply_filter()
        for committed, preedit in (("삼", ""), ("", "성"), ("성", "ㅈ"), ("", "전"), ("전", "ㄱ"), ("", "기")):
            ev = QInputMethodEvent(preedit, [])
            ev.setCommitString(committed)
            QApplication.sendEvent(tab._search_edit, ev)
        self.assertEqual(tab._search_edit.text(), "삼성전")              # Qt's committed text
        self.assertEqual(tab._search_edit.search_text(), "삼성전기")
        self.assertEqual(self._companies(), ["삼성전기"])
        ev = QInputMethodEvent("", [])
        ev.setCommitString("기")                                           # Enter / next key commits it
        QApplication.sendEvent(tab._search_edit, ev)
        self.assertEqual(self._companies(), ["삼성전기"])
        tab._search_edit.clear()
        self.assertEqual(len(self._companies()), 4)

    def test_period_button_cycles_and_filters(self):
        tab, btn = self.tab, self.tab._period_btn
        self.assertEqual((btn.text(), btn.isChecked()), ("기간(전체)", False))
        for label, period in (("기간(1개월)", "1M"), ("기간(3개월)", "3M"), ("기간(올해)", "YTD"), ("기간(전체)", "All")):
            btn.click()
            self.assertEqual(btn.text(), label)
            self.assertEqual(tab._period_filter, period)
            self.assertEqual(btn.isChecked(), period != "All")
        btn.click()                                     # 1M: buys of the last 30 days only
        self.assertEqual(self._companies(), ["Naver", "Samsung"])

    def test_group_and_stale_buttons_cycle(self):
        tab = self.tab
        tab._group_btn.click()
        self.assertEqual((tab._group_btn.text(), tab._group_btn.isChecked(), tab._sort_by_date),
                         ("보기(월별)", True, True))
        self.assertTrue(any(kind == "monthly" for kind, _ in tab._row_data))
        tab._group_btn.click()
        self.assertEqual((tab._group_btn.text(), tab._group_btn.isChecked(), tab._sort_by_date),
                         ("보기(상태별)", False, False))
        self.assertEqual((tab._stale_btn.text(), tab._stale_btn.isChecked()), ("시세(30일)", False))
        tab._stale_btn.click()
        self.assertEqual((tab._stale_btn.text(), tab._stale_btn.isChecked(), tab._hide_stale_closed),
                         ("시세(전체)", True, False))

    def test_every_toolbar_button_has_one_width_that_fits_every_label(self):
        tab = self.tab
        btns = [tab._period_btn, tab._group_btn, tab._stale_btn, tab._summary_btn,
                tab._fetch_dep_btn, tab._add_trade_btn]
        self.assertEqual(len({b.width() for b in btns}), 1)
        width = btns[0].width()
        for btn in (tab._period_btn, tab._group_btn, tab._stale_btn):
            for _ in range(4):
                btn.click()
                self.assertEqual(btn.width(), width)
                self.assertGreaterEqual(width, btn.sizeHint().width())

    def test_toolbar_is_one_row_with_add_trade_last(self):
        tab = self.tab
        tab.resize(1500, 800)
        tab.show()
        self.addCleanup(tab.close)
        app.processEvents()
        y = lambda w: w.mapTo(tab, w.rect().center()).y()   # noqa: E731
        x = lambda w: w.mapTo(tab, w.rect().topLeft()).x()  # noqa: E731
        order = [tab._search_edit, tab._period_btn, tab._group_btn, tab._stale_btn, tab._summary_btn,
                 tab._fetch_dep_btn, tab._add_trade_btn]
        self.assertEqual(len({y(w) for w in order}), 1)
        self.assertEqual([x(w) for w in order], sorted(x(w) for w in order))
        self.assertFalse(hasattr(tab, "_open_stocks_combo"))
        self.assertFalse(hasattr(tab, "_reload_btn"))          # 2026-11-18: Universe refresh reloads this tab

    def test_stock_history_uses_open_lots_or_closed_trades(self):
        tab = self.tab
        opened = []

        class _Dlg:
            def __init__(self, company, matches, total_pl, total_buy, total_sell, parent=None, **kw):
                opened.append((company, len(matches), round(total_sell), kw.get("is_open_position", False)))

            def exec(self):
                return 0

        with patch("ui.history_tab.StockTradeHistoryDialog", _Dlg):
            tab._show_stock_history("Samsung", is_open=False)
            tab._show_stock_history("Samsung", is_open=True)
        # closed: the recorded sell amount; open: qty x current price
        self.assertEqual(opened, [("Samsung", 1, 12000, False), ("Samsung", 1, 11000, True)])

    def test_language_state_carries_the_search(self):
        tab = self.tab
        tab._search_edit.setText("kakao")
        tab._period_btn.click()
        state = tab.export_state()
        tab._search_edit.clear()
        tab._on_period_filter_changed("All")
        tab.import_state(state)
        self.assertEqual(tab._search_edit.text(), "kakao")
        self.assertEqual((tab._period_btn.text(), tab._period_filter), ("기간(1개월)", "1M"))
