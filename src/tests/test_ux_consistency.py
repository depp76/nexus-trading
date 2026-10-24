"""tests/test_ux_consistency.py — the shared UI rules of docs/ui.md section 8.

8.1 buttons come from make_button (one height, a role objectName, no emoji);
8.2 tables from setup_table (row height, selection, empty hint);
8.4/8.5 informational results go to a status line instead of a modal box.
"""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QTableWidget, QPushButton

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


if __name__ == "__main__":
    unittest.main()
