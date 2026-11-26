"""tests/test_universe_tab.py — UniverseTab behaviour that does not need the
network: the startup re-fetch of user-added tickers renders once per batch."""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])


def _no_disk(_path, default=None):
    return default


def _stock(ticker, cap):
    return {"ticker": ticker, "name": f"S{ticker}", "market": "KOSPI", "market_cap": cap, "price": 1.0, "changes": {}}


class TestStartupAddedTickersRenderOnce(unittest.TestCase):

    def _tab(self):
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=_no_disk):
            tab = UniverseTab()
        tab.custom_settings = {"added": [], "deleted": []}
        return tab

    def setUp(self):
        # The tab persists all_data to UNIVERSE_CACHE_FILE after a batch render
        # and after a user add; never let a test touch the real cache file.
        p = patch("ui.universe_tab.atomic_save_json")
        self.save_json = p.start()
        self.addCleanup(p.stop)

    def test_startup_arrivals_are_batched_into_one_render(self):
        tab = self._tab()
        with patch.object(tab, "load_custom_settings"), patch.object(tab, "save_custom_settings"), \
             patch.object(tab, "_reload_table") as reload, patch.object(tab, "filter_table") as filt:
            for t, cap in (("100001", 5), ("100002", 50), ("100003", 20)):
                tab._handle_single_stock_loaded(_stock(t, cap), "", is_startup=True, ticker_hint=t)
            # Nothing rendered yet; the single-shot timer is armed instead.
            self.assertEqual(reload.call_count, 0)
            self.assertTrue(tab._startup_render_timer.isActive())
            self.assertEqual(self.save_json.call_count, 0)

            tab._startup_render_timer.stop()
            tab._render_startup_batch()
            self.assertEqual(reload.call_count, 1)
            self.assertEqual(filt.call_count, 1)
        # Sorted by market cap descending, like on_finished_all.
        self.assertEqual([d["ticker"] for d in tab.all_data], ["100002", "100003", "100001"])
        # The batch render persists the added tickers into the startup cache (review_agy.md 2.4).
        self.assertEqual(self.save_json.call_count, 1)
        self.assertEqual([d["ticker"] for d in self.save_json.call_args.args[1]], ["100002", "100003", "100001"])

    def test_user_add_still_renders_immediately(self):
        tab = self._tab()
        with patch.object(tab, "load_custom_settings"), patch.object(tab, "save_custom_settings"), \
             patch.object(tab, "_reload_table") as reload, patch.object(tab, "filter_table"), \
             patch.object(tab, "update_total_status"):
            tab._handle_single_stock_loaded(_stock("100009", 1), "", is_startup=False, ticker_hint="100009")
            self.assertEqual(reload.call_count, 1)
            self.assertFalse(tab._startup_render_timer.isActive())
        self.assertEqual(self.save_json.call_count, 1)
        self.assertEqual([d["ticker"] for d in self.save_json.call_args.args[1]], ["100009"])


class TestAddTickerRow(unittest.TestCase):
    """2026-11-15: no "추가할 시장" caption; NASDAQ / NYSE can be added; the
    input hint follows the market."""

    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=_no_disk):
            self.tab = UniverseTab()

    def test_add_dialog_markets_caption_and_hint(self):
        from PyQt6.QtWidgets import QLabel
        from ui.dialogs import AddTickerDialog
        dlg = AddTickerDialog("KOSPI")
        combo = dlg.market_combo
        # one US choice (2026-11-15); the stored value is "US"
        self.assertEqual([combo.itemText(i) for i in range(combo.count())], ["KOSPI", "KOSDAQ", "미국"])
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], ["KOSPI", "KOSDAQ", "US"])
        # the dialog labels its market field so it is not read as the toolbar filter
        self.assertTrue([w for w in dlg.findChildren(QLabel) if w.text() == "추가할 시장"])
        self.assertIn("005930", dlg.ticker_input.placeholderText())
        self.assertFalse(dlg.add_btn.isEnabled())
        combo.setCurrentIndex(combo.findData("US"))
        self.assertIn("AAPL", dlg.ticker_input.placeholderText())
        dlg.ticker_input.setText(" ko ")
        self.assertTrue(dlg.add_btn.isEnabled())
        self.assertEqual(dlg.values(), ("US", "KO"))

    def test_add_button_opens_the_dialog_and_remembers_the_market(self):
        tab = self.tab
        calls = []

        class _Dlg:
            DialogCode = type("DC", (), {"Accepted": 1})

            def __init__(self, market, parent=None):
                calls.append(("open", market))

            def exec(self):
                return 1

            def values(self):
                return "US", "KO"

        with patch("ui.universe_tab.AddTickerDialog", _Dlg), \
                patch.object(tab, "add_ticker", side_effect=lambda m, t: calls.append(("add", m, t))):
            tab.add_ticker_btn.click()
            tab.add_ticker_btn.click()
        self.assertEqual(calls, [("open", "KOSPI"), ("add", "US", "KO"), ("open", "US"), ("add", "US", "KO")])

    def test_us_add_goes_to_the_fetch_thread_with_its_market(self):
        tab = self.tab
        tab.custom_settings = {"added": [], "deleted": []}
        started = []

        class _Thread:
            def __init__(self, market, ticker):
                started.append((market, ticker))
                self.finished = type("S", (), {"connect": lambda *_: None})()

            def start(self):
                pass

        with patch("ui.universe_tab.SingleStockFetchThread", _Thread),                 patch.object(tab, "_track_thread", side_effect=lambda t, attr=None: t),                 patch.object(tab, "load_custom_settings"), patch.object(tab, "save_custom_settings"):
            tab.add_ticker("US", "ko")
        self.assertEqual(started, [("US", "KO")])

    def test_market_toggles_show_index_names_and_fit(self):
        tab = self.tab
        labels = {m: b.text().replace("&&", "&") for m, b in tab._market_buttons.items()}
        # 2026-11-18 (docs/ui.md 8.32): the NASDAQ toggle is the 150 largest NASDAQ-listed
        # names, not the Nasdaq-100 index; its stored key is "NASDAQ 150" since 2026-11-26.
        self.assertEqual(labels, {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "NASDAQ 150": "NASDAQ 150", "S&P500": "S&P 500"})
        tip = tab._market_buttons["NASDAQ 150"].toolTip()
        self.assertTrue(tip.startswith("NASDAQ:"))
        self.assertIn("상위 150종목", tip)
        self.assertIn("ADR", tip)
        self.assertNotIn("NASDAQ 100", tip)
        for b in tab._market_buttons.values():
            self.assertGreaterEqual(b.width(), b.sizeHint().width())


class TestStatusFilterButton(unittest.TestCase):
    """The toolbar's status filter is one button cycling 종목(전체) -> 종목(보유)
    -> 종목(관심) (user direction 2026-11-16) at a fixed size and drives the
    table filter."""

    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=_no_disk):
            self.tab = UniverseTab()

    def test_cycles_label_checked_state_and_filter(self):
        tab = self.tab
        btn = tab.tg_filter_btn
        width0 = btn.width()
        self.assertEqual(btn.text(), "종목(전체)")
        self.assertFalse(btn.isChecked())
        seen = []
        with patch.object(tab.table, "apply_col_filters", side_effect=lambda *a, **k: seen.append(k["status"])):
            for expected_label, expected_status, expected_checked in (
                    ("종목(보유)", "On", True), ("종목(관심)", "Tg", True), ("종목(전체)", "", False),
                    ("종목(보유)", "On", True)):
                btn.click()
                self.assertEqual(btn.text(), expected_label)
                self.assertEqual(btn.isChecked(), expected_checked)
                self.assertEqual(tab._status_filter, expected_status)
                self.assertEqual(btn.width(), width0)
        self.assertEqual(seen, ["On", "Tg", "", "On"])
        self.assertEqual(btn.minimumWidth(), btn.maximumWidth())     # fixed width: label changes never resize it

    def test_view_state_sets_the_button(self):
        tab = self.tab
        tab.apply_view_state({"status": "Tg"})
        self.assertEqual(tab.tg_filter_btn.text(), "종목(관심)")
        self.assertTrue(tab.tg_filter_btn.isChecked())
        self.assertEqual(tab._status_filter, "Tg")

    def test_every_toolbar_button_has_one_fixed_width(self):
        tab = self.tab
        btns = [*tab._market_buttons.values(), tab.tg_filter_btn, tab.filters_btn,
                tab.add_ticker_btn, tab.signal_btn, tab.refresh_btn]
        widths = {b.width() for b in btns}
        self.assertEqual(len(widths), 1)
        for b in btns:
            self.assertEqual(b.minimumWidth(), b.maximumWidth())
            self.assertGreaterEqual(b.width(), b.sizeHint().width())
        for _ in range(3):                                          # every status label fits
            tab.tg_filter_btn.click()
            self.assertGreaterEqual(tab.tg_filter_btn.width(), tab.tg_filter_btn.sizeHint().width())
        tab._ranges = {"tper": [0, 12]}
        tab._update_filters_button()
        self.assertEqual(tab.filters_btn.width(), widths.pop())    # "필터 (1)" keeps the size

    def test_filter_hides_rows_of_other_statuses(self):
        from ui.universe_table import StockTable
        table = StockTable()
        rows = {"A": "-", "B": "On", "C": "Tg"}
        table.setRowCount(0)
        with patch.object(table, "_row_identity", side_effect=lambda r: {"ticker": list(rows)[r],
                                                                          "status": list(rows.values())[r],
                                                                          "name": "", "market": "KOSPI"}):
            table.setRowCount(3)
            for status, visible in (("", {"A", "B", "C"}), ("On", {"B"}), ("Tg", {"C"})):
                table.apply_col_filters("", status=status, market="ALL")
                shown = {t for i, t in enumerate(rows) if not table.isRowHidden(i)}
                self.assertEqual(shown, visible, status)


class TestRowStatusButton(unittest.TestCase):
    """The identity cell's fixed-size blank/Held/Target button cycles the
    row's status on click (user direction 2026-09-29); other clicks in the
    cell do not."""

    def _table(self):
        from ui.universe_table import StockTable
        table = StockTable()
        table.resize(900, 300)
        table.show()
        rows = [{"name": n, "ticker": t, "market": "KOSPI", "market_cap": 1e11, "trailing_per": 10.0,
                 "forward_per": 9.0, "price": 100.0, "currency": "", "change_mode": "pct", "changes": {}}
                for t, n in (("000001", "A"), ("000002", "B"))]
        table.load_data(rows, highlights={"000002": "On"})
        return table

    def test_click_on_the_button_emits_toggle_for_that_row_only(self):
        from PyQt6.QtCore import QPoint, QRect, Qt
        from PyQt6.QtTest import QTest
        from ui.delegates import IdentityDelegate
        from ui.universe_columns import COL_IDENTITY
        table = self._table()
        seen = []
        table.toggle_requested.connect(seen.append)
        col_w = table.columnWidth(COL_IDENTITY)
        for row in (0, 1):
            cell = QRect(0, table.rowViewportPosition(row), col_w, table.rowHeight(row))
            btn = IdentityDelegate.status_button_rect(cell)
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=btn.center())
            # the name area of the same cell is an ordinary click
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(cell.x() + 20, cell.center().y()))
        self.assertEqual(seen, ["000001", "000002"])
        table.close()

    def test_button_click_leaves_focus_selection_and_current_cell_alone(self):
        """User report 2026-11-20: the click moved focus to the table and the
        swallowed press left a stale drag-select origin for the move/release."""
        from PyQt6.QtCore import QPoint, QRect, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget
        from ui.delegates import IdentityDelegate
        from ui.universe_columns import COL_IDENTITY
        from ui.universe_table import StockTable
        host = QWidget()
        lay = QVBoxLayout(host)
        edit = QLineEdit()
        lay.addWidget(edit)
        table = StockTable()
        lay.addWidget(table)
        host.resize(900, 300)
        host.show()
        rows = [{"name": n, "ticker": t, "market": "KOSPI", "market_cap": 1e11, "trailing_per": 10.0,
                 "forward_per": 9.0, "price": 100.0, "currency": "", "change_mode": "pct", "changes": {}}
                for t, n in (("000001", "A"), ("000002", "B"), ("000003", "C"))]
        table.load_data(rows, highlights={})
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(table.columnViewportPosition(1) + 5, table.rowViewportPosition(0) + 5))
        edit.setFocus()
        QApplication.processEvents()
        before = (table.currentIndex().row(), table.currentIndex().column(),
                  sorted({i.row() for i in table.selectedIndexes()}))
        cell = QRect(0, table.rowViewportPosition(2), table.columnWidth(COL_IDENTITY), table.rowHeight(2))
        c = IdentityDelegate.status_button_rect(cell).center()
        QTest.mousePress(table.viewport(), Qt.MouseButton.LeftButton, pos=c)
        QTest.mouseMove(table.viewport(), c + QPoint(2, 1))
        QTest.mouseRelease(table.viewport(), Qt.MouseButton.LeftButton, pos=c + QPoint(2, 1))
        QApplication.processEvents()
        after = (table.currentIndex().row(), table.currentIndex().column(),
                 sorted({i.row() for i in table.selectedIndexes()}))
        self.assertEqual(after, before)
        self.assertIs(QApplication.focusWidget(), edit)
        host.close()

    def test_button_rect_is_the_same_size_in_every_state(self):
        from PyQt6.QtCore import QRect
        from ui.delegates import IdentityDelegate, STATUS_BUTTON
        self.assertEqual(set(STATUS_BUTTON), {"-", "On", "Tg"})
        for w in (150, 260, 400):
            r = IdentityDelegate.status_button_rect(QRect(0, 0, w, 28))
            self.assertEqual((r.width(), r.height()), (IdentityDelegate.STATUS_BTN_W, IdentityDelegate.STATUS_BTN_H))
            self.assertEqual(r.right(), w - 1 - IdentityDelegate.STATUS_BTN_MARGIN)


if __name__ == "__main__":
    unittest.main()


class _FakeScoreThread:
    instances = []

    def __init__(self, items, variant_id="C1", n_top=10):
        self.items = list(items)
        self.n_top = n_top
        self.variant_id = variant_id
        self.started = False
        self.progress = _FakeSignal()
        self.finished = _FakeSignal()
        _FakeScoreThread.instances.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return False

    def isFinished(self):
        return not self.started

    def blockSignals(self, _b):
        pass


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for s in self.slots:
            s(*a)


class TestSignalButton(unittest.TestCase):
    """The "전략 신호" button runs the picked variant over the rows of its
    markets on a tracked worker and pins the lists when the worker finishes."""

    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        _FakeScoreThread.instances.clear()

    def _tab(self):
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=_no_disk):
            tab = UniverseTab()
        tab.all_data = [
            _stock("005930", 500), {**_stock("035720", 50), "market": "KOSDAQ"},
            {**_stock("AAPL", 900), "market": "NASDAQ 150"},
            {**_stock("KS11", 0), "market": "Index", "is_index": True},
        ]
        return tab

    def test_scores_only_kr_equities_on_a_tracked_thread(self):
        tab = self._tab()
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._on_signal_clicked()
        self.assertEqual(len(_FakeScoreThread.instances), 1)
        worker = _FakeScoreThread.instances[0]
        self.assertTrue(worker.started)
        self.assertEqual([d["ticker"] for d in worker.items], ["005930", "035720"])
        self.assertEqual(worker.n_top, 10)
        self.assertFalse(tab.signal_btn.isEnabled())
        self.assertIn(worker, tab.collect_threads_to_stop())
        self.assertIs(tab._signal_thread, worker)

    def test_a_run_from_today_is_reused_without_a_worker(self):
        """An MA chart's "계산" (or an earlier pin) left today's run in the shared cache."""
        from strategy.trend_following.signal_provider import pick_signals
        from tests.strategy.trend_following.test_scoring import score_book
        tab = self._tab()
        book, _ = score_book()
        picks = pick_signals("C2", book, None)
        tab._signal_cache.put(picks)
        tab._signal_variant = "C2"
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._on_signal_clicked()
        self.assertEqual(_FakeScoreThread.instances, [])
        self.assertIs(tab._last_picks, picks)
        self.assertTrue(tab.signal_btn.isChecked())

    def test_empty_universe_warns_instead_of_starting(self):
        """docs/ui.md 8.5: an informational result goes to the tab's status
        line, not a modal box."""
        tab = self._tab()
        tab.all_data = [{**_stock("AAPL", 900), "market": "NASDAQ 150"}]
        statuses = []
        tab.status_text_changed.connect(statuses.append)
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread), \
             patch("ui.universe_tab.QMessageBox.information") as info:
            tab._on_signal_clicked()
        info.assert_not_called()
        self.assertTrue(any("계산할 KOSPI/KOSDAQ 종목이 없습니다" in s for s in statuses), statuses)   # 전략 신호: ...
        self.assertIn("계산할 KOSPI/KOSDAQ 종목이 없습니다", tab.footer_lbl.text())
        self.assertEqual(_FakeScoreThread.instances, [])
        self.assertTrue(tab.signal_btn.isEnabled())

    def _score_tab(self):
        tab = self._tab()
        tab.all_data = [_stock(t, cap) for t, cap in
                        (("AAA", 900), ("UP", 800), ("DOWN", 700), ("BBB", 600), ("BOUNCE", 500),
                         ("PULL", 400), ("THIN", 300), ("SPIKE", 200))]
        return tab

    @staticmethod
    def _rec(variant="C1"):
        """The worker's result: signal_api.SignalPicks of `variant`."""
        from strategy.trend_following.signal_provider import pick_signals
        from tests.strategy.trend_following.test_scoring import score_book
        book, _dates = score_book()
        return pick_signals(variant, book, None)

    def test_finished_pins_top_then_bottom_then_the_rest(self):
        tab = self._score_tab()
        statuses = []
        tab.status_text_changed.connect(statuses.append)
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._on_signal_clicked()
        self.assertFalse(tab.signal_btn.isChecked())
        worker = _FakeScoreThread.instances[0]
        self.assertEqual(worker.variant_id, tab._signal_variant)
        worker.progress.emit("Daily history 25/40")
        self.assertIn("전략 신호: Daily history 25/40", statuses[-1])

        rec = self._rec()
        top = [s.ticker for s in rec.top]
        bottom = [s.ticker for s in rec.bottom]
        self.assertEqual(sorted(top), ["PULL", "UP"])
        self.assertEqual(len(bottom), 3)
        with patch("ui.universe_tab.QMessageBox.warning") as warn:
            worker.finished.emit(rec, "")
        warn.assert_not_called()
        self.assertTrue(tab.signal_btn.isEnabled())
        self.assertTrue(tab.signal_btn.isChecked())
        # Top rows in rank order, then Bottom rows, then the rest in cap order.
        self.assertEqual([d["ticker"] for d in tab.all_data], top + bottom + ["AAA", "BBB", "THIN"])
        self.assertEqual(tab.all_data[0]["signal_tag"], "C1 상위 1")             # short tag (2026-11-16)
        self.assertEqual(tab.all_data[len(top)]["signal_tag"], "C1 하위 1")
        self.assertIn(" · C1 상위 1 · +", tab.table.item(0, 0).toolTip())       # ticker, market and figure on the tooltip
        self.assertNotIn("signal_tag", tab.all_data[-1])
        meta = tab.table.item(0, 0).data(0x0100)["meta"]      # identity UserRole payload
        self.assertEqual(meta, "C1 상위 1")                                  # the tag only, the name keeps the room
        self.assertEqual(tab.table.item(0, 0).text(), f"S{top[0]}")
        self.assertIn("고정", statuses[-1])

        # A full refresh keeps the pin (the labels are re-applied to the new dicts).
        fresh = [_stock(t, cap) for t, cap in (("BBB", 600), ("PULL", 400), ("UP", 800), ("BOUNCE", 500))]
        with patch.object(tab, "load_custom_settings"):
            tab.custom_settings = {"added": [], "deleted": [], "highlights": {}}
            tab.on_finished_all(fresh)
        self.assertEqual([d["ticker"] for d in tab.all_data][:2], top)
        self.assertEqual(tab.all_data[2]["ticker"], "BOUNCE")
        self.assertTrue(tab.all_data[0]["signal_tag"].startswith("C1 상위 1"))

        # Clicking again unpins: default cap order, labels gone, button unchecked.
        tab._on_signal_clicked()
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertEqual(tab._signal_order, {})
        self.assertEqual([d["ticker"] for d in tab.all_data], ["UP", "BBB", "BOUNCE", "PULL"])
        self.assertFalse(any("signal_tag" in d for d in tab.all_data))
        self.assertNotIn("상위", tab.table.item(0, 0).data(0x0100)["meta"])
        self.assertIn("복원", statuses[-1])

    def test_failure_leaves_the_order_alone_and_unchecks(self):
        tab = self._score_tab()
        before = [d["ticker"] for d in tab.all_data]
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._on_signal_clicked()
        worker = _FakeScoreThread.instances[0]
        with patch("ui.universe_tab.QMessageBox.warning") as warn:
            worker.finished.emit(None, "boom")
        warn.assert_called_once()
        self.assertTrue(tab.signal_btn.isEnabled())
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertEqual([d["ticker"] for d in tab.all_data], before)
        self.assertEqual(tab._signal_order, {})

    def test_pin_drops_the_column_sort_so_the_order_shows(self):
        from PyQt6.QtCore import Qt
        tab = self._score_tab()
        tab._reload_table()
        tab.table.sortItems(1, Qt.SortOrder.AscendingOrder)      # user sorted by Cap
        tab.table._on_sort_indicator_changed(1, Qt.SortOrder.AscendingOrder)
        self.assertEqual(tab.table._sort_col, 1)
        tab._on_signals_finished(self._rec(), "")
        self.assertEqual(tab.table._sort_col, -1)
        rows = [tab.table.item(r, 0).data(0x0100)["ticker"] for r in range(tab.table.rowCount())]
        self.assertEqual(rows, [d["ticker"] for d in tab.all_data])

    def test_user_column_sort_unpins_and_a_refresh_does_not_bring_it_back(self):
        from PyQt6.QtCore import Qt
        tab = self._score_tab()
        tab._on_signals_finished(self._rec(), "")
        self.assertTrue(tab.signal_btn.isChecked())
        # load_data's own indicator re-apply must not count as a user sort.
        self.assertTrue(tab._signal_order)
        tab.table.sortItems(1, Qt.SortOrder.DescendingOrder)     # header click on Cap
        tab.table._filter_header.sortIndicatorChanged.emit(1, Qt.SortOrder.DescendingOrder)
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertEqual(tab._signal_order, {})
        self.assertFalse(any("signal_tag" in d for d in tab.all_data))
        self.assertNotIn("상위", tab.table.item(0, 0).data(0x0100)["meta"])
        self.assertEqual(tab.table._sort_col, 1)                 # the user's sort stays
        with patch.object(tab, "load_custom_settings"):
            tab.custom_settings = {"added": [], "deleted": [], "highlights": {}}
            tab.on_finished_all([_stock("UP", 800), _stock("PULL", 400)])
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertFalse(any("signal_tag" in d for d in tab.all_data))


class TestStrategySignalMenu(unittest.TestCase):
    """2026-11-16: "전략 신호" is a menu of check boxes -- "선택 안 함" and one
    per strategy row, exactly one ticked."""

    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        _FakeScoreThread.instances.clear()

    def _tab(self):
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=_no_disk):
            tab = UniverseTab()
        tab.all_data = [_stock("005930", 500), {**_stock("035720", 50), "market": "KOSDAQ"}]
        return tab

    @staticmethod
    def _ticked(tab):
        return ["none"] * tab._signal_none_check.isChecked() + [v for v, cb in tab._signal_checks.items() if cb.isChecked()]

    def test_menu_has_none_and_every_variant_as_check_boxes(self):
        from PyQt6.QtWidgets import QCheckBox
        from strategy.trend_following import VARIANTS
        tab = self._tab()
        self.assertEqual(tab.signal_btn.text(), "전략 신호")
        self.assertIs(tab.signal_btn.menu(), tab._signal_menu)
        self.assertEqual(list(tab._signal_checks), list(VARIANTS))
        self.assertTrue(all(isinstance(cb, QCheckBox) for cb in tab._signal_checks.values()))
        self.assertEqual(tab._signal_none_check.text(), "선택 안 함")
        self.assertEqual(self._ticked(tab), ["none"])                    # nothing pinned at start
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._signal_checks["A2"].click()
        self.assertEqual(_FakeScoreThread.instances[-1].variant_id, "A2")
        self.assertEqual(self._ticked(tab), ["A2"])

    def test_exit_lists_are_tagged_and_none_unpins(self):
        from strategy.signal_api import SignalName, SignalPicks
        import datetime as dt
        tab = self._tab()
        picks = SignalPicks("A1", dt.date(2026, 11, 13),
                            [SignalName("005930", "S", "KOSPI", 1.0, 0.1, "RS +10.0%p")],
                            [SignalName("035720", "K", "KOSDAQ", 1.0, -0.2, "RS -20.0%p")],
                            regime_on=False, uses_regime=True, n_universe=2, n_liquid=2,
                            n_top_candidates=1, n_bottom_candidates=1)
        statuses = []
        tab.status_text_changed.connect(statuses.append)
        tab._on_signals_finished(picks, "")
        self.assertEqual([d["signal_tag"] for d in tab.all_data], ["A1 상위 1", "A1 이탈 1"])
        self.assertEqual(tab.table.item(0, 0).toolTip(), "005930 · KOSPI · A1 상위 1 · RS +10.0%p")
        self.assertIn("오늘 진입 신호 1/1", statuses[-1])
        self.assertIn("시장 조건(L1) 꺼짐", statuses[-1])
        self.assertTrue(tab.signal_btn.isChecked())
        self.assertEqual(self._ticked(tab), ["A1"])
        self.assertIs(tab._signal_cache.get("A1"), picks)                # shared with the MA chart
        tab._signal_none_check.click()                                    # 선택 안 함
        self.assertEqual(tab._signal_order, {})
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertEqual(self._ticked(tab), ["none"])
        self.assertFalse(any("signal_tag" in d for d in tab.all_data))

    def test_a_failed_run_falls_back_to_none(self):
        tab = self._tab()
        with patch("ui.universe_tab.SignalPicksThread", _FakeScoreThread):
            tab._signal_checks["B"].click()
        with patch("ui.universe_tab.QMessageBox.warning"):
            _FakeScoreThread.instances[-1].finished.emit(None, "boom")
        self.assertEqual(self._ticked(tab), ["none"])


class TestNasdaqDisplayName(unittest.TestCase):
    """2026-11-18 (user direction, docs/ui.md 8.32): the stored key "NASDAQ 150"
    (was "NASDAQ 100" until 2026-11-26) shows as "NASDAQ" in row meta and status lines."""

    def test_row_meta_and_status_show_nasdaq(self):
        from ui.universe_columns import _market_label
        from ui.universe_tab import _part_label
        from ui.universe_views import MARKETS, market_display
        self.assertEqual(_market_label("NASDAQ 150"), "NASDAQ")
        self.assertEqual(_part_label("NASDAQ 150"), "NASDAQ")
        self.assertEqual((_market_label("KOSPI"), _market_label("S&P500")), ("KOSPI", "S&P500"))
        self.assertEqual(market_display("NYSE"), "NYSE")
        self.assertIn("NASDAQ 150", MARKETS)                      # the stored key


class TestNasdaqKeyMigration(unittest.TestCase):
    """2026-11-26 (user direction): the NASDAQ market key was renamed "NASDAQ 100"
    -> "NASDAQ 150"; saved settings and cached rows are migrated on load."""

    def test_migrate_replaces_whole_values_only(self):
        from ui.universe_views import migrate_market_keys
        settings = {"markets": ["KOSPI", "NASDAQ 100"], "signals": [{"market": ["NASDAQ 100"]}],
                    "added": [{"ticker": "QQQ", "name": "Invesco NASDAQ 100 ETF", "market": "NASDAQ 100"}]}
        out, changed = migrate_market_keys(settings)
        self.assertTrue(changed)
        self.assertEqual(out["markets"], ["KOSPI", "NASDAQ 150"])
        self.assertEqual(out["signals"], [{"market": ["NASDAQ 150"]}])
        self.assertEqual(out["added"][0], {"ticker": "QQQ", "name": "Invesco NASDAQ 100 ETF", "market": "NASDAQ 150"})
        index_row = {"name": "Nasdaq 100", "market": "Index", "ticker": "^NDX"}
        self.assertEqual(migrate_market_keys([index_row]), ([index_row], False))

    def test_market_index_overlay_maps_to_ndx(self):
        from data.market import INDEX_TICKERS, MARKET_INDEX_LABELS
        self.assertEqual(INDEX_TICKERS[MARKET_INDEX_LABELS["NASDAQ 150"]][0], "^NDX")
