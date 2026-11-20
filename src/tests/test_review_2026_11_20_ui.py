"""tests/test_review_2026_11_20_ui.py — regression tests for the thread
lifecycle / main window / Universe, Total Assets and Strategy tab findings of
review_agy.md (2026-11-20, base 4ace769): C3, H6-H9, H16, M1, M6, M8, M12
(items 2, 3, 5), L1 and L4 (the ui parts in this scope).

The main-window items (H16, M1, M8, L4 failures) run in a child process, as
tests/test_language_switch.py does: importing main loads the saved UI
language and a switch re-imports every ui module, which must not leak into
the other test modules.
"""
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeWorker(QThread):
    """Never started; `running` stands in for isRunning(), signals are emitted by hand."""
    finished_all = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    report = pyqtSignal(list, dict, list, dict, str)
    preloaded = pyqtSignal(dict, float)

    def __init__(self, running=True, variant_id=None):
        super().__init__()
        self.running = running
        self.cancelled = False
        self.variant_id = variant_id

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancelled = True


class _DeadThread:
    """A zombie whose C++ side is gone."""
    def isRunning(self):
        raise RuntimeError("wrapped C/C++ object has been deleted")


# ---------------------------------------------------------------------------
# C3 / H6: retire_thread and stale_sender
# ---------------------------------------------------------------------------
class TestRetireThread(unittest.TestCase):
    def test_a_dead_zombie_does_not_drop_the_running_thread(self):
        from ui.common import retire_thread
        running = _FakeWorker(running=True)
        owner = SimpleNamespace(_zombie_threads=[_DeadThread()], _x=running)
        retire_thread(owner, "_x")
        self.assertIsNone(owner._x)
        self.assertEqual(owner._zombie_threads, [running])
        self.assertTrue(running.signalsBlocked())
        self.assertTrue(running.cancelled)


class TestStaleSender(unittest.TestCase):
    def _owner(self):
        from PyQt6.QtCore import QObject
        from ui.common import stale_sender

        class Owner(QObject):
            def __init__(self):
                super().__init__()
                self.calls = []

            def slot(self, text):
                self.calls.append((text, stale_sender(self, "_t")))
        return Owner()

    def test_only_a_replaced_worker_is_stale(self):
        owner = self._owner()
        current, old = _FakeWorker(), _FakeWorker()
        owner._t = current
        for w in (current, old):
            w.progress.connect(owner.slot)
        current.progress.emit("current")
        old.progress.emit("old")
        owner.slot("direct")
        self.assertEqual(owner.calls, [("current", False), ("old", True), ("direct", False)])


# ---------------------------------------------------------------------------
# H7: the four run() methods always emit their completion signal
# ---------------------------------------------------------------------------
class TestThreadsAlwaysEmit(unittest.TestCase):
    def test_single_stock_fetch(self):
        from threads.fetch_threads import SingleStockFetchThread
        t, got = SingleStockFetchThread("KOSPI", "005930"), []
        t.finished.connect(lambda *a: got.append(a))
        with patch("threads.fetch_threads.fetch_single_stock", side_effect=ValueError("boom")):
            t.run()
        self.assertEqual(got, [(None, "boom", "005930")])

    def test_stock_ma(self):
        from threads.fetch_threads import StockMaThread
        t, got = StockMaThread("005930", "Samsung", "KOSPI", "pct"), []
        t.finished.connect(lambda *a: got.append(a))
        with patch("threads.fetch_threads.fetch_stock_ma_multi", side_effect=ValueError("boom")):
            t.run()
        self.assertEqual(got, [("005930", "Samsung", None, "boom", [], "KOSPI", "pct", [])])

    def test_index_ma(self):
        from threads.fetch_threads import IndexMaThread
        t, got = IndexMaThread(), []
        t.finished.connect(lambda *a: got.append(a))
        with patch("threads.fetch_threads.fetch_all_indices_mas", side_effect=ValueError("boom")):
            t.run()
        self.assertEqual(got, [({}, None)])

    def test_position_prices(self):
        from threads.fetch_threads import PositionPriceFetchThread
        t = PositionPriceFetchThread(["A"], ["1"], ["KOSPI"], [1.0], [1.0], [1.0], request_id=7)
        got = []
        t.prices_ready.connect(lambda *a: got.append(a))
        with patch.object(PositionPriceFetchThread, "_resolve_kr_codes", side_effect=KeyError("x")):
            t.run()
        self.assertEqual(got, [([], 7)])


# ---------------------------------------------------------------------------
# H6 / H8 / L4: Trading Universe
# ---------------------------------------------------------------------------
class TestUniverseTab(unittest.TestCase):
    def setUp(self):
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)

    def _tab(self):
        from tests.test_universe_tab import TestSignalButton
        return TestSignalButton._tab(self)

    def test_refresh_retires_the_tick_and_the_market_fetch(self):
        tab = self._tab()
        lw, mk = _FakeWorker(), _FakeWorker()
        tab._lw_fetch_thread, tab._market_fetch_thread = lw, mk
        tab._markets_pending = True
        with patch.object(tab, "_reload_table"), patch("ui.universe_tab.AllDataFetchThread"):
            tab.refresh_data()
        self.assertIsNone(tab._lw_fetch_thread)
        self.assertIsNone(tab._market_fetch_thread)
        self.assertFalse(tab._markets_pending)
        self.assertTrue(lw.cancelled and mk.cancelled)
        self.assertIn(lw, tab.collect_threads_to_stop())    # parked, still stopped at shutdown

    def test_a_retired_market_fetch_adds_nothing(self):
        tab = self._tab()
        old = _FakeWorker()
        old.finished_all.connect(tab._on_markets_fetched)
        tab._market_fetch_thread = None                       # retired by a refresh
        before = list(tab.all_data)
        old.finished_all.emit([{"ticker": "NEW", "market": "KOSPI"}])
        self.assertEqual(tab.all_data, before)

    def _tick(self, tab):
        updated = [dict(x) for x in tab.all_data]
        updated[0].update(price=2.0, changes={"1d": 1.0}, trailing_per=12.5, forward_per=10.0)
        return updated

    def test_tick_updates_price_fields_in_place(self):
        tab = self._tab()
        row = tab.all_data[0]
        row["signal_tag"] = "C1 Top 1"                         # set on the row while the tick ran
        updated = self._tick(tab)
        updated[0]["signal_tag"] = "stale copy"
        with patch.object(tab.table, "update_changed_rows") as upd:
            tab._on_universe_lightweight_loaded(updated)
        self.assertIs(tab.all_data[0], row)
        self.assertEqual((row["price"], row["changes"], row["trailing_per"], row["forward_per"]),
                         (2.0, {"1d": 1.0}, 12.5, 10.0))
        self.assertEqual(row["signal_tag"], "C1 Top 1")
        upd.assert_called_once()
        self.assertIs(upd.call_args.args[0], tab.all_data)
        self.assertEqual(upd.call_args.args[1], {0})

    def test_a_retired_tick_is_ignored(self):
        tab = self._tab()
        old = _FakeWorker()
        old.finished_all.connect(tab._on_universe_lightweight_loaded)
        tab._lw_fetch_thread = _FakeWorker()                   # a newer tick
        updated = self._tick(tab)
        old.finished_all.emit(updated)
        self.assertEqual(tab.all_data[0]["price"], 1.0)

    def test_language_switch_state_keeps_the_add_market_and_unpinned_picks(self):
        tab = self._tab()
        picks = object()
        tab._add_market, tab._last_picks = "NASDAQ 100", picks
        state = tab.export_state()
        new = self._tab()
        new.import_state(state)
        self.assertEqual(new._add_market, "NASDAQ 100")
        self.assertIs(new._last_picks, picks)

    def test_english_failure_line_uses_the_display_name(self):
        from i18n import set_language, language
        tab = self._tab()
        msgs = []
        tab.status_text_changed.connect(msgs.append)
        lang = language()
        set_language("en")
        try:
            tab._on_market_fetch_failed("x", ["NASDAQ 100"])
        finally:
            set_language(lang)
        self.assertNotIn("NASDAQ 100", msgs[-1])


# ---------------------------------------------------------------------------
# H9: the MA chart's "전략 점검" workers
# ---------------------------------------------------------------------------
class _FakeCheckThread(_FakeWorker):
    finished = pyqtSignal(object, str, str)
    instances: list = []

    def __init__(self, item, vid, picks=None):
        super().__init__(running=True, variant_id=vid)
        _FakeCheckThread.instances.append(self)

    def start(self):
        pass


class TestStrategyCheckSectionLifecycle(unittest.TestCase):
    def setUp(self):
        _FakeCheckThread.instances.clear()
        p = patch("ui.strategy_check_section.StrategyCheckThread", _FakeCheckThread)
        p.start()
        self.addCleanup(p.stop)

    def _dialog(self):
        from tests.test_stock_ma_dialog import _df
        from ui.dialogs import StockMaDialog
        return StockMaDialog("005930", "Samsung", "KOSPI", _df())

    def test_one_check_per_variant_and_close_cancels_and_detaches(self):
        from ui import strategy_check_section as scs
        d = self._dialog()
        sec = d.strategy_section
        sec.set_variant("A0")
        sec.set_variant("C1")
        sec.set_variant("A0")                                  # A0's check is still running
        self.assertEqual([t.variant_id for t in _FakeCheckThread.instances], ["A0"])
        worker = _FakeCheckThread.instances[0]
        d.close()
        self.assertTrue(worker.cancelled)
        self.assertTrue(worker.signalsBlocked())
        self.assertIn(worker, scs._DETACHED)
        sec._run_stock("A1")                                   # nothing starts after close
        self.assertEqual(len(_FakeCheckThread.instances), 1)
        worker.running = False
        scs._DETACHED.remove(worker)

    def test_progress_of_another_variant_does_not_overwrite(self):
        d = self._dialog()
        self.addCleanup(d.close)
        sec = d.strategy_section
        self.assertEqual(sec.variant(), "C1")
        before = sec.status_lbl.text()
        other, mine = _FakeWorker(variant_id="C2"), _FakeWorker(variant_id="C1")
        for w in (other, mine):
            w.progress.connect(sec._on_universe_progress)
        other.progress.emit("C2 progress")
        self.assertEqual(sec.status_lbl.text(), before)
        mine.progress.emit("C1 progress")
        self.assertEqual(sec.status_lbl.text(), "C1 progress")

    def test_a_dead_opener_tracker_is_dropped(self):
        d = self._dialog()
        self.addCleanup(d.close)
        sec = d.strategy_section
        sec._ctx.track = MagicMock(side_effect=RuntimeError("deleted"))
        sec.set_variant("A0")
        self.assertIsNone(sec._ctx.track)
        self.assertEqual(len(sec._threads), 1)


# ---------------------------------------------------------------------------
# M6 / M12-3 / M12-5: Total Assets
# ---------------------------------------------------------------------------
class TestAssetsTab(unittest.TestCase):
    def setUp(self):
        for target, kw in (("ui.assets_tab.trade_db.load_asset_records", {"return_value": []}),
                           ("ui.assets_tab.TradingRecordTab._start_metrics_preload", {})):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = patch("ui.assets_tab.trade_db.save_asset_records")
        self.mock_save = p.start()
        self.addCleanup(p.stop)

    def _tab(self):
        from ui.assets_tab import TradingRecordTab
        tab = TradingRecordTab()
        tab._metrics_ready = False
        return tab

    def test_metrics_reply_forgets_its_thread_first(self):
        tab = self._tab()
        tab._metrics_thread = _FakeWorker()
        seen = []
        with patch.object(tab, "_refresh_table", side_effect=lambda: seen.append(tab._metrics_thread)):
            tab._on_metrics_preloaded({}, 1300.0)
        self.assertEqual(seen, [None])

    def test_closing_the_graph_stops_the_report(self):
        tab = self._tab()
        tab._records = [{"date": "2026-01-02", "total": 100.0, "manual": True}]
        tab._metrics_ready = True
        tab._date_metrics_cache = {"2026-01-02": (1300.0, 2500.0)}
        report = _FakeWorker()
        tab._report_thread = report
        with patch("ui.assets_tab.TotalAssetsGraphDialog"):
            tab._show_graph()
        self.assertIsNone(tab._report_thread)
        self.assertTrue(report.cancelled)
        with patch("ui.assets_tab.QMessageBox") as box, patch("ui.assets_tab.AssetsReportDialog") as dlg:
            report.blockSignals(False)
            report.report.connect(tab._on_report_fetched)
            report.report.emit([], {}, [], {}, "late")
        box.warning.assert_not_called()
        dlg.assert_not_called()

    def test_a_week_without_a_rate_is_a_gap_in_the_usd_line(self):
        tab = self._tab()
        tab._records = [{"date": d, "total": t, "manual": True}
                        for d, t in (("2026-01-02", 100.0), ("2026-01-09", 110.0), ("2026-01-16", 120.0))]
        tab._metrics_ready = True
        tab._date_metrics_cache = {"2026-01-02": (0.0, 2500.0), "2026-01-09": (1000.0, 2500.0),
                                   "2026-01-16": (1000.0, 2500.0)}
        with patch("ui.assets_tab.TotalAssetsGraphDialog") as dlg:
            tab._show_graph()
        _dates, _k, _a, usd_returns, _totals, usd_totals = dlg.call_args.args[:6]
        self.assertTrue(math.isnan(usd_returns[0]) and math.isnan(usd_totals[0]))
        self.assertAlmostEqual(usd_returns[1], 0.0)              # the first week with a rate is the base
        self.assertAlmostEqual(usd_returns[2], 120 / 110 * 100 - 100)

    def test_a_failed_save_changes_nothing(self):
        tab = self._tab()
        tab._records = [{"date": "2026-01-02", "total": 100.0, "manual": True}]
        self.mock_save.side_effect = OSError("disk full")
        with patch("ui.assets_tab.QMessageBox"):
            tab._save_record("2026-01-02", 999.0, False)
            tab._save_record("2026-01-09", 5.0, False)
        self.assertEqual(tab._records, [{"date": "2026-01-02", "total": 100.0, "manual": True}])
        self.mock_save.side_effect = None
        tab._save_record("2026-01-09", 5.0, False)
        self.assertEqual([r["date"] for r in tab._records], ["2026-01-02", "2026-01-09"])


class TestGraphDialogGap(unittest.TestCase):
    def test_nan_usd_week_shows_a_dash(self):
        from ui.dialogs.assets_graph import TotalAssetsGraphDialog
        nan = math.nan
        dlg = TotalAssetsGraphDialog(["2026-01-02", "2026-01-09", "2026-01-16"], [0.0, 1.0, 2.0],
                                     [0.0, 5.0, 6.0], [0.0, 4.0, nan], [1e8, 1.05e8, 1.06e8],
                                     [70000.0, 72000.0, nan])
        self.addCleanup(dlg.close)
        self.assertTrue(dlg.end_label_texts()[1][0].endswith(" -"))
        self.assertTrue(dlg.tooltip_lines(2)[2][0].endswith(" -"))
        self.assertEqual(len(dlg._end_label_artists), 2)         # no label for the missing value


# ---------------------------------------------------------------------------
# M12-2: report FX carry-forward and a non-positive base
# ---------------------------------------------------------------------------
class TestAssetsReport(unittest.TestCase):
    def test_usd_rate_is_carried_forward(self):
        from ui.assets_report import build_report
        trades = [{"ticker": "AAPL", "company": "Apple", "market": "NASDAQ", "qty": 1,
                   "buy_amount": 100_000.0, "buy_date": "2026-01-02"}]
        closes = {"AAPL": [("2026-01-02", 100.0), ("2026-01-05", 110.0)]}
        kospi = [("2026-01-02", 2500.0), ("2026-01-05", 2600.0)]
        rows = build_report(trades, 0.0, [("2026-01-02", "early"), ("2026-01-05", "mid")], closes, kospi,
                            {"2026-01-02": 1400.0})
        self.assertTrue(rows[1]["priced"])
        self.assertAlmostEqual(rows[1]["value"], 110.0 * 1400.0)

    def test_returns_start_at_the_first_positive_total(self):
        from ui.assets_report import build_report
        trades = [{"ticker": "005930", "company": "S", "market": "KOSPI", "qty": 1,
                   "buy_amount": 100.0, "buy_date": "2026-01-05"}]
        kospi = [("2026-01-02", 2500.0), ("2026-01-05", 2600.0), ("2026-01-06", 2700.0)]
        closes = {"005930": [("2026-01-05", 100.0), ("2026-01-06", 110.0)]}
        # end cash 0: before the buy the rolled-back cash is +100; make it negative instead
        rows = build_report(trades, -150.0, [("2026-01-02", "early"), ("2026-01-05", "mid"),
                                             ("2026-01-06", "mid")], closes, kospi, {})
        self.assertLessEqual(rows[0]["total"], 0)
        self.assertIsNone(rows[0]["total_cum_pct"])
        self.assertIsNone(rows[0]["kospi_cum_pct"])
        self.assertIsNone(rows[1]["total_cum_pct"])              # total -50: still no base
        self.assertIsNone(rows[2]["total_cum_pct"])              # total -40


# ---------------------------------------------------------------------------
# L1: gemini model at call time
# ---------------------------------------------------------------------------
class TestGeminiModel(unittest.TestCase):
    def test_model_is_read_at_call_time(self):
        import gemini_helper
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-test"}):
            self.assertEqual(gemini_helper._model(), "gemini-test")
        with patch.dict(os.environ, {"GEMINI_MODEL": " "}):
            self.assertEqual(gemini_helper._model(), gemini_helper._DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# L4: Strategy tab view state across a language switch
# ---------------------------------------------------------------------------
class TestTrendTabState(unittest.TestCase):
    def test_show_all_and_result_tab_are_carried(self):
        from strategy.trend_following.research import ResearchRequest, run_research
        from tests.strategy.trend_following.helpers import synthetic_dataset
        from ui.trend_following_tab import TrendFollowingTab
        tab = TrendFollowingTab()
        req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",), cost_multipliers=(1.0,),
                              run_event_study=False)
        tab.set_variant_choice(("A0",))
        tab._on_research_finished(run_research(synthetic_dataset(), req), "")
        tab.show_all_cb.setChecked(True)
        tab.result_tabs.setCurrentIndex(2)
        new = TrendFollowingTab()
        new.import_state(tab.export_state())
        self.assertTrue(new.show_all_cb.isChecked())
        self.assertEqual(new.result_tabs.currentIndex(), 2)


# ---------------------------------------------------------------------------
# H16 / M1 / M8 / L4 (main window), in a child process
# ---------------------------------------------------------------------------
_MAIN_SCRIPT = textwrap.dedent(r'''
    import logging, logging.handlers, os, sys, types
    from datetime import datetime
    tmp = sys.argv[1]
    sys.path.insert(0, sys.argv[2])
    import PyQt6.QtCore as QtCore
    from PyQt6.QtCore import QThread, QSettings
    QThread.start = lambda self, *a, **k: None
    _INI = os.path.join(tmp, "settings.ini")

    class _TempSettings(QSettings):
        def __init__(self, *_args, **_kwargs):
            super().__init__(_INI, QSettings.Format.IniFormat)

    QtCore.QSettings = _TempSettings
    import paths, trade_db
    paths.UNIVERSE_CACHE_FILE = os.path.join(tmp, "universe_cache.json")
    paths.CUSTOM_SETTINGS_FILE = os.path.join(tmp, "custom_settings.json")
    trade_db._DB_FILE = os.path.join(tmp, "portfolio.db")
    trade_db.TRADING_RECORD_FILE = os.path.join(tmp, "trading_record.json")
    import i18n
    i18n.save_language("ko")
    import main
    root = logging.getLogger()
    print("NO_FILE_HANDLER", not any(isinstance(h, logging.FileHandler) for h in root.handlers))
    log = os.path.join(tmp, "app.log")
    main.setup_logging(log)
    rot = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    print("ROTATING", len(rot) == 1 and rot[0].maxBytes == 5 * 1024 * 1024 and rot[0].backupCount == 3)
    for h in list(root.handlers):
        root.removeHandler(h); h.close()
    print("RELOAD_NAME", "retire_when_idle" in main._UI_NAMES["ui.common"])

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    w = main.MainWindow()
    w.global_auto_timer.stop()
    w.show(); app.processEvents()
    app._nexus_main_window = w
    w._failures.fail("Trading History", "prices: x", datetime.now())
    w._on_language_requested("en")
    w._on_language_requested("en")                 # a quick second click
    for _ in range(3): app.processEvents()
    new = app._nexus_main_window
    windows = [x for x in app.topLevelWidgets() if isinstance(x, main.MainWindow) and x.isVisible()]
    print("ONE_SWITCH", new is not w and windows == [new])
    print("AGAIN_NOOP", main.switch_language(w, "ko") is w and app._nexus_main_window is new)
    print("FAILURES_KEPT", new._failures.active() is not None)

    class T:
        def __init__(self): self.waits, self.terminated = [], False
        def isRunning(self): return True
        def blockSignals(self, b): pass
        def quit(self): pass
        def wait(self, ms): self.waits.append(ms); return False
        def terminate(self): self.terminated = True
    backup, other = T(), T()
    fake = types.SimpleNamespace(request_stop_background_work=lambda: [backup, other], _auto_backup_thread=backup,
                                 _BACKUP_STOP_WAIT_MS=main.MainWindow._BACKUP_STOP_WAIT_MS,
                                 _THREAD_STOP_WAIT_MS=main.MainWindow._THREAD_STOP_WAIT_MS)
    main.MainWindow.stop_background_work(fake)
    print("NO_TERMINATE", not backup.terminated and not other.terminated
          and backup.waits == [30000] and other.waits == [2000])
    sys.stdout.flush()
    os._exit(0)
''')


class TestMainWindow(unittest.TestCase):
    def test_logging_switch_guard_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
            proc = subprocess.run([sys.executable, "-c", _MAIN_SCRIPT, tmp, _SRC], cwd=_SRC, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=240)
        out = proc.stdout
        for flag in ("NO_FILE_HANDLER", "ROTATING", "RELOAD_NAME", "ONE_SWITCH", "AGAIN_NOOP", "FAILURES_KEPT",
                     "NO_TERMINATE"):
            self.assertIn(f"{flag} True", out, f"{flag} failed\n--- stdout\n{out}\n--- stderr\n{proc.stderr[-3000:]}")


if __name__ == "__main__":
    unittest.main()
