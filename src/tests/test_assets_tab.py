"""tests/test_assets_tab.py — TradingRecordTab's sort-safe record lookups.

review_agy.md #1: _on_cell_double_clicked/_delete_selected used to index
self._records (always ascending by date) with the QTableWidget's visual row
index, which no longer matches once the table's default descending sort
indicator (set in _build_ui) re-sorts the rows. Both now look up the record
by the date bound to column 0's UserRole instead.

Isolation note: trade_db.save_asset_records() does a full-table replace, and
TradingRecordTab methods call it on every edit/delete. An earlier version of
this file only patched trade_db.load_asset_records() for the duration of the
constructor call (inside _make_tab()'s `with patch(...)` block), so
_save_records() calls made later in a test body -- after that block had
already exited -- hit the real trade_db.save_asset_records() against the
live portfolio.db, replacing the user's actual weekly asset snapshots with
test data (discovered via a "total assets tab data wrong" bug report;
recovered from archive/auto_20260920_152914/portfolio.db, see roadmap.md
2026-09-20). Every test here now patches both the read AND write path for
its entire duration via setUp/tearDown, not just around construction.
"""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication([])


class _AssetsTabTestCase(unittest.TestCase):
    """Patches trade_db's asset-record read/write for the whole test, and
    the startup side effect (the background metrics thread)
    that does not belong in a unit test either."""

    def setUp(self):
        self._load_patch = patch("ui.assets_tab.trade_db.load_asset_records", return_value=[])
        self._save_patch = patch("ui.assets_tab.trade_db.save_asset_records")
        self._preload_patch = patch("ui.assets_tab.TradingRecordTab._start_metrics_preload")
        self.mock_load = self._load_patch.start()
        self.mock_save = self._save_patch.start()
        self._preload_patch.start()
        self.addCleanup(self._load_patch.stop)
        self.addCleanup(self._save_patch.stop)
        self.addCleanup(self._preload_patch.stop)

    def _make_tab(self):
        from ui.assets_tab import TradingRecordTab
        tab = TradingRecordTab()
        tab._metrics_ready = False  # skip network rate/kospi lookups
        return tab


class TestSortSafeRecordLookup(_AssetsTabTestCase):

    def setUp(self):
        super().setUp()
        self.tab = self._make_tab()
        self.tab._records = [
            {"date": "2026-01-02", "total": 1000.0, "manual": True},
            {"date": "2026-01-09", "total": 2000.0, "manual": True},
            {"date": "2026-01-16", "total": 3000.0, "manual": True},
        ]
        self.tab._refresh_table()
        # Default sort indicator is descending by date (column 0), so
        # visual row 0 is 2026-01-16 even though self._records[0] is
        # 2026-01-02 -- the exact mismatch this fix targets.
        self.assertEqual(self.tab._records[0]["date"], "2026-01-02")
        self.assertIn("2026-01-16", self.tab._table.item(0, 0).text())

    def test_double_click_edits_the_visually_selected_date_not_records0(self):
        with patch("ui.assets_tab.QInputDialog.getText", return_value=("3,500", True)):
            self.tab._on_cell_double_clicked(0, 4)  # visually the 2026-01-16 row
        edited = next(r for r in self.tab._records if r["date"] == "2026-01-16")
        self.assertEqual(edited["total"], 3500.0)
        self.assertTrue(edited["manual"])
        # The oldest record (self._records[0] before the fix) must be untouched.
        untouched = next(r for r in self.tab._records if r["date"] == "2026-01-02")
        self.assertEqual(untouched["total"], 1000.0)
        self.mock_save.assert_called()  # exercised the save path, but against the mock

    def test_delete_selected_removes_the_visually_selected_date_not_records0(self):
        self.tab._table.selectRow(0)  # visually the 2026-01-16 row
        with patch("ui.assets_tab.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.tab._delete_selected()
        remaining_dates = {r["date"] for r in self.tab._records}
        self.assertEqual(remaining_dates, {"2026-01-02", "2026-01-09"})


class TestMultiYearFridays(_AssetsTabTestCase):
    """review_agy.md #3: the Friday list used to only generate the current
    year's Fridays, so a record from a prior year couldn't be selected/edited
    at all. Since 2026-11-16 the list feeds AddRecordDialog."""

    def test_list_extends_back_to_the_earliest_record_year(self):
        self.mock_load.return_value = [{"date": "2024-03-08", "total": 1000.0, "manual": True}]
        tab = self._make_tab()
        fridays = tab._record_fridays()
        self.assertEqual(fridays[0][1][:4], "2024")
        self.assertIn("2024-03-08", {d for _, d in fridays})

    def test_without_records_the_list_starts_this_year(self):
        import datetime as _dt
        fridays = self._make_tab()._record_fridays()
        self.assertEqual(int(fridays[0][1][:4]), _dt.date.today().year)


class TestOneRowToolbar(_AssetsTabTestCase):
    """User direction 2026-11-16 (docs/ui.md 8.17): one toolbar row -- the
    currency cycle button, the rate and the live figures, then Graph and
    Add Record (the dialog) last; the input row is gone."""

    def test_currency_button_cycles(self):
        tab = self._make_tab()
        btn = tab._currency_btn
        self.assertEqual((btn.text(), btn.isChecked(), tab._currency), ("통화(KRW)", False, "KRW"))
        btn.click()
        self.assertEqual((btn.text(), btn.isChecked(), tab._currency), ("통화(USD)", True, "USD"))
        self.assertEqual(tab.export_state(), {"currency": "USD"})
        btn.click()
        self.assertEqual((btn.text(), btn.isChecked(), tab._currency), ("통화(KRW)", False, "KRW"))
        tab.import_state({"currency": "USD"})
        self.assertEqual((btn.text(), btn.isChecked()), ("통화(USD)", True))

    def test_everything_is_on_one_row_with_add_record_last(self):
        tab = self._make_tab()
        tab.resize(1400, 700)
        tab.show()
        self.addCleanup(tab.close)
        app.processEvents()
        y = lambda w: w.mapTo(tab, w.rect().center()).y()   # noqa: E731
        x = lambda w: w.mapTo(tab, w.rect().topLeft()).x()  # noqa: E731
        order = [tab._currency_btn, tab._rate_lbl, tab.live_asset_lbl, tab.live_diff_lbl,
                 tab._graph_btn, tab._add_btn]
        self.assertEqual(len({y(w) for w in order}), 1)
        self.assertEqual([x(w) for w in order], sorted(x(w) for w in order))
        self.assertFalse(hasattr(tab, "_date_combo"))
        self.assertFalse(hasattr(tab, "_asset_edit"))
        self.assertFalse(hasattr(tab, "_export_btn"))   # removed, user direction 2026-11-16

    def test_add_record_saves_what_the_dialog_returns(self):
        tab = self._make_tab()

        class _Dlg:
            DialogCode = __import__("PyQt6.QtWidgets", fromlist=["QDialog"]).QDialog.DialogCode
            seen = []

            def __init__(self, fridays, live_total, existing, parent=None):
                _Dlg.seen.append((fridays[-1][1], live_total, existing))

            def exec(self):
                return self.DialogCode.Accepted

            def values(self):
                return "2026-01-09", 1234.0, True

        tab._current_live_asset = 999.0
        with patch("ui.assets_tab.AddRecordDialog", _Dlg):
            tab._on_add_record_clicked()
        self.assertEqual(tab._records, [{"date": "2026-01-09", "total": 1234.0, "manual": True}])
        self.assertEqual(_Dlg.seen[0][1:], (999.0, {}))
        self.assertIsNone(tab._add_dlg)
        self.mock_save.assert_called()


class TestAddRecordDialog(unittest.TestCase):
    FRIDAYS = [("W01 (2026-01-02)", "2026-01-02"), ("W02 (2026-01-09)", "2026-01-09")]

    def _dlg(self, live=0.0, existing=None):
        from ui.dialogs import AddRecordDialog
        return AddRecordDialog(self.FRIDAYS, live, existing or {})

    def test_latest_week_is_auto_filled_and_not_manual(self):
        dlg = self._dlg(live=1_500_000.0)
        self.assertEqual(dlg.date_combo.currentData(), "2026-01-09")
        self.assertEqual(dlg.asset_edit.text(), "1,500,000")
        self.assertEqual(dlg.values(), ("2026-01-09", 1_500_000.0, False))

    def test_typed_value_is_manual_and_comma_formatted(self):
        from PyQt6.QtTest import QTest
        dlg = self._dlg(live=1_500_000.0)
        dlg.asset_edit.clear()
        QTest.keyClicks(dlg.asset_edit, "1234567")
        self.assertEqual(dlg.asset_edit.text(), "1,234,567")
        self.assertEqual(dlg.values(), ("2026-01-09", 1_234_567.0, True))

    def test_past_week_is_not_auto_filled_and_save_needs_a_number(self):
        dlg = self._dlg(live=1_500_000.0, existing={"2026-01-02": 900.0})
        dlg.date_combo.setCurrentIndex(0)
        self.assertEqual(dlg.asset_edit.text(), "")
        self.assertFalse(dlg.save_btn.isEnabled())
        self.assertIn("900", dlg.hint.text())
        dlg.asset_edit.setText("nan")
        self.assertFalse(dlg.save_btn.isEnabled())
        dlg.this_week_btn.click()
        self.assertEqual(dlg.asset_edit.text(), "1,500,000")
        self.assertTrue(dlg.save_btn.isEnabled())

    def test_live_update_while_open(self):
        dlg = self._dlg()
        self.assertEqual(dlg.asset_edit.text(), "")
        dlg.set_live_total(2_000_000.0)
        self.assertEqual(dlg.asset_edit.text(), "2,000,000")


class TestSharedToolbarWidth(_AssetsTabTestCase):
    """User direction 2026-11-16: Universe, History and Total Assets toolbar
    buttons share one width, wide enough for every label any of them shows."""

    def test_three_tabs_one_width(self):
        from ui.assets_tab import TradingRecordTab
        from ui.history_tab import TradingHistoryTab
        from ui.universe_tab import UniverseTab
        from ui.common import toolbar_button_width
        with patch("ui.universe_tab.safe_load_json", side_effect=lambda _p, default=None: default):
            u = UniverseTab()
        h = TradingHistoryTab()
        h._settings_save_timer.stop()
        a = TradingRecordTab()
        buttons = [*u._market_buttons.values(), u.tg_filter_btn, u.filters_btn, u.add_ticker_btn,
                   u.trend_score_btn, u.refresh_btn,
                   h._period_btn, h._group_btn, h._stale_btn, h._summary_btn, h._fetch_dep_btn,
                   h._reload_btn, h._add_trade_btn,
                   a._currency_btn, a._graph_btn, a._add_btn]
        widths = {b.width() for b in buttons}
        self.assertEqual(widths, {toolbar_button_width()})
        for b in buttons:
            self.assertGreaterEqual(b.width(), b.sizeHint().width(), b.text())


class TestMetricsSnapshotOnly(_AssetsTabTestCase):
    """review_codex.md #9: after the preload, table refreshes read the
    worker's snapshot and never fetch on the UI thread; a stale snapshot or
    a new date only schedules another background run."""

    def setUp(self):
        super().setUp()
        self.mock_load.return_value = [{"date": "2026-01-02", "total": 1_450_000.0, "manual": True}]
        self.tab = self._make_tab()
        self.preload = self.tab._start_metrics_preload   # the patched mock
        self.tab._on_metrics_preloaded({"2026-01-02": (1450.0, 2500.0)}, 1460.0)
        self.preload.reset_mock()

    def _no_network(self):
        return patch("data.fx._fetch_usd_krw_history", side_effect=AssertionError("network on UI thread")), \
            patch("data.history.get_historical_data", side_effect=AssertionError("network on UI thread"))

    def test_refresh_uses_the_snapshot(self):
        fx_patch, hist_patch = self._no_network()
        with fx_patch, hist_patch:
            self.tab._refresh_table()
        self.assertEqual(self.tab._rate_kospi_for_date("2026-01-02"), (1450.0, 2500.0))
        self.assertEqual(self.tab._rate_lbl.text(), "USD/KRW: 1,460.0")
        self.preload.assert_not_called()

    def test_stale_snapshot_or_new_date_schedules_a_worker(self):
        import datetime as _dt
        self.tab._metrics_fetched_at -= _dt.timedelta(hours=1)
        fx_patch, hist_patch = self._no_network()
        with fx_patch, hist_patch:
            self.tab._refresh_table()
        self.preload.assert_called()

        self.preload.reset_mock()
        self.tab._metrics_fetched_at = _dt.datetime.now()
        self.assertEqual(self.tab._rate_kospi_for_date("2026-01-09"), (0.0, 0.0))
        self.preload.assert_called()


if __name__ == "__main__":
    unittest.main()
