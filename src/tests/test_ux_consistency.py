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


def _is_symbol(ch: str) -> bool:
    """Arrows, math/technical symbols, shapes, dingbats and emoji (8.1 bans
    them on buttons); Hangul and other letters are fine."""
    cp = ord(ch)
    return 0x2190 <= cp <= 0x2BFF or cp >= 0x1F000


class TestTabButtons(unittest.TestCase):
    """Every push button a tab builds follows 8.1: no emoji label, at most one
    primary per tab."""

    def _check(self, tab, primary_text):
        buttons = tab.findChildren(QPushButton)
        primaries = [b.text() for b in buttons if b.objectName() == "primary"]
        self.assertEqual(primaries, [primary_text])
        for b in buttons:
            self.assertFalse(any(_is_symbol(ch) for ch in b.text()), repr(b.text()))

    def test_strategy_tab(self):
        from ui.trend_following_tab import TrendFollowingTab
        tab = TrendFollowingTab()
        self._check(tab, "리서치 실행")
        self.assertEqual(tab.stop_btn.objectName(), "")

    def test_assets_tab_has_no_delete_button(self):
        with patch("ui.assets_tab.trade_db.load_asset_records", return_value=[]), \
             patch("ui.assets_tab.trade_db.save_asset_records"), \
             patch("ui.assets_tab.TradingRecordTab._start_metrics_preload"), \
             patch("ui.assets_tab.TradingRecordTab._schedule_daily_sync"):
            from ui.assets_tab import TradingRecordTab
            tab = TradingRecordTab()
            self._check(tab, "기록 추가")
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


class TestBadgesAndKpiCard(unittest.TestCase):
    """ui_redesign_2026-11 P0-3: badges are objectName-only labels, KpiCard
    shows a value, an optional badge and a help line."""

    def test_badge_kinds(self):
        from ui.common import make_badge
        names = {"ok": "badgeOk", "warn": "badgeWarn", "fail": "badgeFail", "na": "badgeNa"}
        for kind, name in names.items():
            lbl = make_badge(kind, "x")
            self.assertEqual((lbl.objectName(), lbl.text()), (name, "x"))
        with self.assertRaises(ValueError):
            make_badge("bad", "x")

    def test_badge_rules_are_in_the_app_qss(self):
        from ui.theme import app_qss
        qss = app_qss("")
        for name in ("badgeOk", "badgeWarn", "badgeFail", "badgeNa", "KpiCard", "kpiHelp", "settingsChip"):
            self.assertIn(f"#{name}", qss)

    def test_kpi_card_set(self):
        from ui.common import KpiCard
        from ui.theme import TEXT
        card = KpiCard("Sharpe", "help line")
        self.assertEqual((card.objectName(), card.label.text(), card.value.text()),
                         ("KpiCard", "SHARPE", "-"))
        self.assertTrue(card.badge.isHidden())
        card.set("1.62", "#3b6fc4", badge=("ok", "Target met"), help="new help")
        self.assertEqual((card.value.text(), card.badge.objectName(), card.badge.text(), card.help.text()),
                         ("1.62", "badgeOk", "Target met", "new help"))
        self.assertFalse(card.badge.isHidden())
        self.assertIn("#3b6fc4", card.value.styleSheet())
        card.set("0.40", badge=("fail", "Below 1.5"))
        self.assertEqual(card.badge.objectName(), "badgeFail")
        self.assertEqual(card.help.text(), "new help")      # help kept when not given
        card.set("-")
        self.assertTrue(card.badge.isHidden())
        self.assertIn(TEXT, card.value.styleSheet())


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


class TestUniverseRowFitsRangeBar(unittest.TestCase):
    def test_52w_range_cell_fits_the_universe_row(self):
        """The 52W Range cell's bar + label line must fit the Universe row
        (user report 2026-10-25: at the app-wide 24 px the labels were cut)."""
        from ui.widgets import StockTable
        from ui.delegates import RangeBarDelegate
        tbl = StockTable()
        self.assertGreaterEqual(tbl.verticalHeader().defaultSectionSize(),
                                RangeBarDelegate.content_height(tbl.font()))


if __name__ == "__main__":
    unittest.main()


class TestSoftSlateTheme(unittest.TestCase):
    """docs/ui.md 8.16: no default black text on the dark surfaces."""

    def test_palette_text_roles_are_the_theme_text(self):
        from PyQt6.QtGui import QPalette
        from ui.theme import app_palette, TEXT, SURFACE, TEXT_EMPTY
        pal = app_palette()
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                     QPalette.ColorRole.ToolTipText, QPalette.ColorRole.HighlightedText):
            self.assertEqual(pal.color(role).name(), TEXT)
        self.assertEqual(pal.color(QPalette.ColorRole.Base).name(), SURFACE)
        self.assertEqual(pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name(), TEXT_EMPTY)

    def test_text_tokens_keep_4_5_to_1_on_the_surfaces(self):
        from ui import theme, colors

        def lum(h):
            def ch(v):
                v /= 255
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

        def ratio(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        for bg in (theme.SURFACE, theme.ACCENT_BG, theme.HDR_BG):
            for fg in (theme.TEXT, theme.TEXT_MUTED, colors.PROFIT, colors.LOSS, colors.OK, colors.WARN):
                self.assertGreaterEqual(ratio(fg, bg), 4.5, (fg, bg))
        self.assertGreaterEqual(ratio(theme.PRIMARY_TEXT, theme.PRIMARY_BG), 4.5)

    def test_matplotlib_theme_colors_text_and_faces(self):
        from matplotlib import rcParams
        from ui.theme import apply_matplotlib_theme, SURFACE, TEXT
        saved = dict(rcParams)
        try:
            apply_matplotlib_theme()
            self.assertEqual(rcParams["axes.facecolor"], SURFACE)
            self.assertEqual(rcParams["text.color"], TEXT)
        finally:
            rcParams.update(saved)

    def test_spin_box_arrow_icons_exist(self):
        import os
        from ui.theme import ARROW_UP_ICON, ARROW_DOWN_ICON
        self.assertTrue(os.path.isfile(ARROW_UP_ICON))
        self.assertTrue(os.path.isfile(ARROW_DOWN_ICON))


class TestButtonOrderAndSizing(unittest.TestCase):
    """docs/ui.md 8.17: dialog button order, no implicit default button,
    initial sizes inside the screen, remembered splitters, toolbar rows."""

    def _row_texts(self, layout):
        return [layout.itemAt(i).widget().text() for i in range(layout.count())
                if layout.itemAt(i).widget() is not None]

    def test_make_button_is_never_an_implicit_default(self):
        self.assertFalse(make_button("저장", "primary").autoDefault())

    def test_button_row_is_right_aligned_in_the_given_order(self):
        from ui.common import button_row
        row = button_row(make_button("취소"), make_button("저장", "primary"), leading=(make_button("전체"),))
        self.assertEqual(self._row_texts(row), ["전체", "취소", "저장"])
        self.assertIsNotNone(row.itemAt(1).spacerItem())       # the stretch sits before the right group

    def test_edit_dialogs_put_cancel_before_save_and_enter_does_not_save(self):
        from ui.dialogs.trade_edit import BuyEditDialog, SellEditDialog, TradeEntryDialog
        for dlg in (BuyEditDialog({"buy_date": "2026-01-02", "buy_price": 1, "qty": 1}),
                    SellEditDialog({"buy_date": "2026-01-02", "qty": 1}), TradeEntryDialog()):
            buttons = [b for b in dlg.findChildren(QPushButton) if b.text() in ("취소", "저장")]
            self.assertEqual(sorted(b.text() for b in buttons), ["저장", "취소"])
            cancel = next(b for b in buttons if b.text() == "취소")
            save = next(b for b in buttons if b.text() == "저장")
            dlg.show()
            app.processEvents()
            self.assertLess(cancel.mapTo(dlg, cancel.rect().topLeft()).x(), save.mapTo(dlg, save.rect().topLeft()).x())
            self.assertFalse(any(b.isDefault() or b.autoDefault() for b in buttons))
            self.assertGreaterEqual(dlg.minimumWidth(), 380)
            dlg.close()

    def test_variant_picker_bulk_buttons_on_top_and_cancel_ok_below(self):
        from ui.dialogs.variant_picker import VariantPickerDialog
        dlg = VariantPickerDialog(("A0",), flows_on=False)
        dlg.show()
        app.processEvents()
        pos = {b.text(): b.mapTo(dlg, b.rect().topLeft()) for b in dlg.findChildren(QPushButton)}
        self.assertLess(pos["전체 선택"].y(), dlg.list.mapTo(dlg, dlg.list.rect().topLeft()).y())
        self.assertGreater(pos["확인"].y(), dlg.list.mapTo(dlg, dlg.list.rect().bottomLeft()).y())
        self.assertLess(pos["취소"].x(), pos["확인"].x())
        dlg.close()

    def test_fit_to_screen_caps_the_initial_size(self):
        from PyQt6.QtWidgets import QDialog
        from ui.common import fit_to_screen
        dlg = QDialog()
        fit_to_screen(dlg, 100_000, 100_000)
        avail = (dlg.screen() or app.primaryScreen()).availableGeometry()
        self.assertLessEqual(dlg.width(), avail.width())
        self.assertLessEqual(dlg.height(), avail.height())

    def test_remember_splitter_ignores_a_stored_value_that_does_not_fit(self):
        from PyQt6.QtCore import QSettings
        from PyQt6.QtWidgets import QSplitter, QWidget
        from ui.common import remember_splitter, _SETTINGS_SCOPE
        settings = QSettings(*_SETTINGS_SCOPE)
        key = "ui/splitter/_test_only"
        try:
            for stored in ("300,0", "1,2,3", "x,y"):
                settings.setValue(key, stored)
                sp = QSplitter(Qt.Orientation.Vertical)
                sp.addWidget(QWidget())
                sp.addWidget(QWidget())
                sp.resize(100, 1000)
                remember_splitter(sp, "_test_only", [550, 450])
                self.assertTrue(all(s > 0 for s in sp.sizes()), (stored, sp.sizes()))
        finally:
            settings.remove(key)

    def test_universe_add_ticker_has_its_own_row(self):
        from ui.universe_tab import UniverseTab
        with patch.object(UniverseTab, "refresh_data"),              patch("ui.universe_tab.safe_load_json", return_value=None):
            tab = UniverseTab()
        tab.resize(1500, 800)
        tab.show()
        app.processEvents()
        y = lambda w: w.mapTo(tab, w.rect().center()).y()   # noqa: E731
        self.assertEqual(y(tab.refresh_btn), y(tab.search_input))       # filters | analysis + primary
        self.assertGreater(y(tab.add_ticker_btn), y(tab.refresh_btn))   # the add group below
        self.assertEqual(y(tab.market_combo), y(tab.add_ticker_btn))
        tab.hide()
