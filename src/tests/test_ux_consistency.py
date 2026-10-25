"""tests/test_ux_consistency.py — the shared UI rules of docs/ui.md section 8.

8.1 buttons come from make_button (one height, a role objectName, no emoji);
8.2 tables from setup_table (row height, selection, empty hint);
8.4/8.5 informational results go to a status line instead of a modal box.
"""
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem, QPushButton

from ui.common import make_button, setup_table, BTN_H, TABLE_ROW_H, make_footer_label

app = QApplication.instance() or QApplication([])


class TestMakeButton(unittest.TestCase):
    def test_height_role_and_toggle(self):
        btn = make_button("Run", "primary", checkable=True, checked=True, tooltip="tip")
        self.assertEqual(btn.height(), BTN_H)
        self.assertEqual(btn.minimumHeight(), BTN_H)
        self.assertEqual(btn.objectName(), "primary")
        self.assertTrue(btn.isCheckable() and btn.isChecked())
        self.assertEqual(btn.toolTip(), "tip")

    def test_click_handler_and_width(self):
        hits = []
        btn = make_button("Go", on_click=lambda: hits.append(1), width=80)
        self.assertEqual(btn.maximumWidth(), 80)
        btn.click()
        self.assertEqual(hits, [1])


class TestSetupTable(unittest.TestCase):
    def test_defaults(self):
        tbl = setup_table(QTableWidget(0, 3), empty_text="nothing here")
        self.assertEqual(tbl.verticalHeader().defaultSectionSize(), TABLE_ROW_H)
        self.assertFalse(tbl.verticalHeader().isVisibleTo(tbl))
        self.assertEqual(tbl.selectionBehavior(), QTableWidget.SelectionBehavior.SelectRows)
        self.assertTrue(tbl.alternatingRowColors())
        self.assertEqual(tbl._empty_hint.text, "nothing here")

    def test_zebra_off_and_no_hint(self):
        tbl = setup_table(QTableWidget(0, 1), zebra=False)
        self.assertFalse(tbl.alternatingRowColors())
        self.assertFalse(hasattr(tbl, "_empty_hint"))

    def test_empty_hint_paints_only_without_rows(self):
        tbl = setup_table(QTableWidget(0, 2), empty_text="EMPTY")
        tbl.resize(300, 150)
        tbl.show()
        app.processEvents()
        dark_empty = _dark_pixels(tbl)
        tbl.setRowCount(1)
        app.processEvents()
        dark_rows = _dark_pixels(tbl)
        tbl.hide()
        self.assertGreater(dark_empty, dark_rows)

    def test_footer_label(self):
        lbl = make_footer_label("x")
        self.assertEqual(lbl.objectName(), "muted")
        self.assertTrue(lbl.wordWrap())


def _dark_pixels(tbl):
    img = tbl.viewport().grab().toImage()
    return sum(1 for x in range(0, img.width(), 2) for y in range(30, img.height(), 2)
               if img.pixelColor(x, y).lightness() < 200)


class TestTabButtons(unittest.TestCase):
    """Every push button a tab builds follows 8.1: no emoji label, at most one
    primary per tab."""

    def _check(self, tab, primary_text):
        buttons = tab.findChildren(QPushButton)
        primaries = [b.text() for b in buttons if b.objectName() == "primary"]
        self.assertEqual(primaries, [primary_text])
        for b in buttons:
            self.assertTrue(all(ord(ch) < 0x2190 for ch in b.text()), repr(b.text()))

    def test_strategy_tab(self):
        from ui.trend_following_tab import TrendFollowingTab
        tab = TrendFollowingTab()
        self._check(tab, "Run Research")
        self.assertEqual(tab.stop_btn.objectName(), "")

    def test_assets_tab_has_no_delete_button(self):
        with patch("ui.assets_tab.trade_db.load_asset_records", return_value=[]), \
             patch("ui.assets_tab.trade_db.save_asset_records"), \
             patch("ui.assets_tab.TradingRecordTab._start_metrics_preload"), \
             patch("ui.assets_tab.TradingRecordTab._schedule_daily_sync"):
            from ui.assets_tab import TradingRecordTab
            tab = TradingRecordTab()
            self._check(tab, "Add Record")
            self.assertFalse(any("Delete" in b.text() for b in tab.findChildren(QPushButton)))

            # 8.5: informational results go to the status bar, not a modal box.
            messages = []
            tab.status_message.connect(messages.append)
            with patch("ui.assets_tab.QMessageBox.information") as info:
                tab._on_export_clicked()
                tab._show_graph()
            info.assert_not_called()
            self.assertEqual(len(messages), 2, messages)




class TestTableKeys(unittest.TestCase):
    """docs/ui.md 8.6: Ctrl+C copies selected rows as TSV and Enter acts like
    a double-click, on every data table (install_table_keys)."""

    def _table(self):
        tbl = setup_table(QTableWidget(3, 2))
        for r in range(3):
            for c in range(2):
                tbl.setItem(r, c, QTableWidgetItem(f"r{r}c{c}"))
        return tbl

    def test_copy_selected_rows_skipping_summary_rows(self):
        from ui.common import install_table_keys
        tbl = self._table()
        install_table_keys(tbl, skip_row=lambda r: r == 1)
        tbl.selectAll()
        QTest.keySequence(tbl, QKeySequence(QKeySequence.StandardKey.Copy))
        self.assertEqual(QApplication.clipboard().text(), "r0c0\tr0c1\nr2c0\tr2c1")

    def test_enter_calls_activate_with_current_cell(self):
        from ui.common import install_table_keys
        tbl = self._table()
        hits = []
        install_table_keys(tbl, on_activate=lambda r, c: hits.append((r, c)), skip_row=lambda r: r == 1)
        tbl.setCurrentCell(2, 1)
        QTest.keyClick(tbl, Qt.Key.Key_Return)
        tbl.setCurrentCell(1, 0)          # a summary row: ignored
        QTest.keyClick(tbl, Qt.Key.Key_Enter)
        self.assertEqual(hits, [(2, 1)])


class TestKpiStrip(unittest.TestCase):
    def test_cells_values_and_colors(self):
        from ui.common import KpiStrip
        from ui.theme import TEXT
        strip = KpiStrip()
        strip.add("a", "Alpha", "sub")
        strip.set_value("a", "+1", "#3b6fc4", tooltip="t")
        value, sub = strip.cells["a"]
        self.assertEqual((value.text(), value.toolTip(), sub.text()), ("+1", "t", "sub"))
        self.assertIn("#3b6fc4", value.styleSheet())
        strip.set_value("a", "0")          # no color: back to the text color, not the stale one
        self.assertIn(TEXT, value.styleSheet())


class TestDialogTables(unittest.TestCase):
    def test_trade_history_dialog_uses_table_defaults(self):
        from ui.dialogs.trade_history import StockTradeHistoryDialog
        rec = {"buy_date": "2026-01-02", "buy_price": 100, "qty": 10, "buy_amount": 1000,
               "sell_date": "2026-02-02", "sell_price": 110, "sell_qty": 10, "sell_amount": 1100,
               "pl": 100, "pl_pct": 10.0, "days_held": 31}
        dlg = StockTradeHistoryDialog("ACME", [rec], 100, 1000, 1100)
        tbl = dlg.findChild(QTableWidget)
        self.assertEqual(tbl.verticalHeader().defaultSectionSize(), TABLE_ROW_H)
        self.assertFalse(tbl.verticalHeader().isVisibleTo(tbl))
        self.assertEqual(dlg.minimumSize().width(), 0)   # resizable, not pinned
if __name__ == "__main__":
    unittest.main()
