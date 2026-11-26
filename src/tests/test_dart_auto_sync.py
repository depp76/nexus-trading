"""DART collection at app start (docs/dart_db_plan_2026-11-26.md 8 and 11;
docs/ui.md 8.33): the once-a-day rule, the collection status read, the
DartSyncThread signals and cancel, DartAutoSync's start condition and label,
and the main window stopping a running collection within 2 s when it closes."""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from data.dart.store import connect
from data.dart.sync import SyncResult, collection_status, is_due, is_initial_load
from ui.colors import DANGER, WARN
from ui.dart_sync_thread import DartAutoSync, DartSyncThread

_APP = QApplication.instance() or QApplication([])
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = dt.timezone(dt.timedelta(hours=9))


def _pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _kst(h, m=0, day=26):
    return dt.datetime(2026, 11, day, h, m, tzinfo=KST)


class TestDueRule(unittest.TestCase):
    def test_start_runs_unless_done_after_evening(self):
        self.assertTrue(is_due(None, None, _kst(9), at_start=True))
        self.assertTrue(is_due(_kst(8).isoformat(), "done", _kst(9), at_start=True))
        self.assertTrue(is_due(_kst(20, day=25).isoformat(), "done", _kst(9), at_start=True))
        self.assertFalse(is_due(_kst(19, 30).isoformat(), "done", _kst(21), at_start=True))
        self.assertFalse(is_due(_kst(19, 30).isoformat(), "partial", _kst(21), at_start=True))
        self.assertTrue(is_due(_kst(19, 30).isoformat(), "failed", _kst(21), at_start=True))

    def test_check_timer_runs_once_after_evening(self):
        self.assertFalse(is_due(_kst(9).isoformat(), "done", _kst(18, 59), at_start=False))
        self.assertTrue(is_due(_kst(9).isoformat(), "done", _kst(19, 1), at_start=False))
        self.assertFalse(is_due(_kst(19, 5).isoformat(), "done", _kst(19, 35), at_start=False))

    def test_naive_local_time_is_read_as_kst(self):
        self.assertFalse(is_due("2026-11-26T19:42:00", "done", _kst(22), at_start=True))


class TestCollectionStatus(unittest.TestCase):
    def test_counts_periods_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "dart.db")
            conn = connect(db)
            conn.executemany("INSERT INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt) "
                             "VALUES (?, 'C1', ?, 2025, '2025-05-15')",
                             [("r1", "11013"), ("r2", "11012"), ("r3", "11012")])
            conn.execute("INSERT INTO fin_fetch(rcept_no, status) VALUES ('r1', 'ok')")
            conn.execute("INSERT INTO api_usage VALUES ('2026-11-26', 100)")
            conn.execute("INSERT INTO sync_state(key, value) VALUES ('list_backfill_done', '1')")
            conn.commit()
            conn.close()
            st = collection_status(db, today=dt.date(2026, 11, 26))
        self.assertEqual((st["periods_total"], st["periods_pending"]), (2, 1))
        self.assertEqual((st["calls_today"], st["calls_left"]), (100, 18_900))
        self.assertTrue(st["list_done"])
        self.assertFalse(is_initial_load(st))
        self.assertTrue(is_initial_load({"list_done": False}))
        self.assertTrue(is_initial_load({"list_done": True, "periods_pending": 5000}))


_INFO = {"last_run": "2026-11-26T19:42:00+09:00", "last_status": "done", "list_done": True,
         "periods_total": 1000, "periods_pending": 0, "calls_left": 18_000, "last_added": 3, "last_calls": 9}


class TestThread(unittest.TestCase):
    def _thread(self, runner, due=True, at_start=True):
        t = DartSyncThread(at_start, runner=runner, status_reader=lambda: dict(_INFO), due=lambda *a: due)
        got = {"started": [], "progress": [], "done": []}
        t.started_info.connect(lambda i: got["started"].append(i))
        t.progress.connect(lambda *a: got["progress"].append(a))
        t.sync_done.connect(lambda o: got["done"].append(o))
        self.addCleanup(t.wait, 3000)
        return t, got

    def test_start_then_done_signals(self):
        def runner(mode, trigger, cancel, on_progress):
            self.assertEqual((mode, trigger), ("auto", "app"))
            on_progress(1, 2, "financials 005930 2025 11013")
            return SyncResult("done", "r", calls=4, filings_added=2)
        t, got = self._thread(runner)
        t.start()
        self.assertTrue(_pump(lambda: got["done"]))
        self.assertEqual(len(got["started"]), 1)
        self.assertEqual(got["progress"], [(1, 2, "financials 005930 2025 11013")])
        self.assertEqual(got["done"][0]["status"], "done")

    def test_not_due_skips_the_run(self):
        calls = []
        t, got = self._thread(lambda *a, **k: calls.append(1), due=False)
        t.start()
        self.assertTrue(_pump(lambda: got["done"]))
        self.assertEqual((got["done"][0]["status"], calls, got["started"]), ("not_due", [], []))

    def test_runner_exception_is_failed(self):
        def runner(*a, **k):
            raise RuntimeError("boom")
        t, got = self._thread(runner)
        t.start()
        self.assertTrue(_pump(lambda: got["done"]))
        self.assertEqual(got["done"][0]["status"], "failed")
        self.assertIn("boom", got["done"][0]["error"])

    def test_cancel_stops_quickly(self):
        def runner(mode, trigger, cancel, on_progress):
            while not cancel.is_set():
                time.sleep(0.01)
            return SyncResult("partial", "r", error="cancelled")
        t, got = self._thread(runner)
        t.start()
        self.assertTrue(_pump(lambda: got["started"]))
        t0 = time.monotonic()
        t.cancel()
        self.assertTrue(t.wait(2000))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(_pump(lambda: got["done"]))
        self.assertEqual(got["done"][0]["status"], "partial")


class _FakeThread(DartSyncThread):
    runs: list = []

    def __init__(self, at_start):
        super().__init__(at_start, runner=self._run, status_reader=lambda: dict(self.info), due=lambda *a: True)

    info = dict(_INFO)
    result = SyncResult("done", "r", calls=7, filings_added=12)

    def _run(self, mode, trigger, cancel, on_progress):
        _FakeThread.runs.append(self.at_start)
        on_progress(10, 40, "financials x")
        return self.result


class TestAutoSync(unittest.TestCase):
    def setUp(self):
        _FakeThread.runs = []
        _FakeThread.info = dict(_INFO)
        _FakeThread.result = SyncResult("done", "r", calls=7, filings_added=12)

    def _sync(self, **kw):
        s = DartAutoSync(thread_factory=_FakeThread, start_delay_ms=50, check_interval_ms=60_000, **kw)
        self.addCleanup(lambda: [t.wait(3000) for t in s.threads()])
        msgs = []
        s.status_message.connect(msgs.append)
        return s, msgs

    def test_waits_for_both_universe_and_delay(self):
        s, _ = self._sync()
        s.arm(universe_ready=False)
        _pump(lambda: False, 0.3)                       # delay passed, Universe not refreshed
        self.assertEqual(_FakeThread.runs, [])
        s.on_universe_refreshed("09:00")
        self.assertTrue(_pump(lambda: _FakeThread.runs and not s.is_running()))
        self.assertEqual(_FakeThread.runs, [True])

    def test_universe_first_then_delay(self):
        s, _ = self._sync()
        s.arm(universe_ready=False)
        s.on_universe_refreshed()
        self.assertEqual(_FakeThread.runs, [])          # 10 s (here 50 ms) not over yet
        self.assertTrue(_pump(lambda: _FakeThread.runs))

    def test_done_label_and_status_message(self):
        s, msgs = self._sync()
        s.arm(universe_ready=True)
        self.assertTrue(_pump(lambda: _FakeThread.runs and s.label.text().startswith("DART 11-26")))
        self.assertEqual(s.label.text(), "DART 11-26 19:42")
        self.assertIn("새 공시 12건", s.label.toolTip())
        self.assertIn("18,000", s.label.toolTip())
        self.assertEqual(msgs, ["DART 새 공시 12건 반영"])

    def test_progress_labels(self):
        s, _ = self._sync()
        s._on_started(dict(_INFO, periods_pending=0))
        s._on_progress(1234, 2000, "financials a")
        self.assertEqual(s.label.text(), "DART 수집 중 1,234건")
        s._on_started(dict(_INFO, list_done=True, periods_total=10_000, periods_pending=6_000))
        self.assertEqual(s.label.text(), "DART 초기 수집 40%")
        s._on_progress(1_700, 6_000, "financials a")       # 4,300 left of 10,000
        self.assertEqual(s.label.text(), "DART 초기 수집 57%")
        s._on_progress(3, 9, "filing list 2025-01-01..2025-03-31")
        self.assertEqual(s.label.text(), "DART 초기 수집 57%")

    def test_partial_failed_no_key_labels(self):
        s, msgs = self._sync()
        s._on_done({"status": "partial", "error": "daily limit", "info": _INFO})
        self.assertEqual(s.label.text(), "DART 일부 수집")
        self.assertIn(WARN, s.label.styleSheet())
        s._on_done({"status": "failed", "error": "HTTPError: 500\nmore", "info": _INFO})
        self.assertEqual((s.label.text(), s.label.toolTip()), ("DART 실패", "HTTPError: 500"))
        self.assertIn(DANGER, s.label.styleSheet())
        s._on_done({"status": "no_key", "info": _INFO})
        self.assertEqual(s.label.text(), "DART 키 없음")
        self.assertIn("DART_API_KEY", s.label.toolTip())
        self.assertEqual(msgs, [])

    def test_not_due_shows_the_newest_run_of_any_runner(self):
        s, msgs = self._sync()
        cli = dict(_INFO, last_run_status="partial", last_trigger="cli", last_error="daily limit")
        s._on_done({"status": "not_due", "info": cli})
        self.assertEqual(s.label.text(), "DART 일부 수집")
        self.assertIn("실행: CLI", s.label.toolTip())
        sched = dict(_INFO, last_run_status="done", last_trigger="scheduler")
        s._on_done({"status": "not_due", "info": sched})
        self.assertEqual(s.label.text(), "DART 11-26 19:42")
        self.assertIn("실행: 스케줄러", s.label.toolTip())
        self.assertEqual(msgs, [])                  # only the app's own runs post a status-bar message

    def test_skip_retries_after_a_while(self):
        s = DartAutoSync(thread_factory=_FakeThread, start_delay_ms=10, check_interval_ms=60_000, retry_ms=50)
        self.addCleanup(lambda: [t.wait(3000) for t in s.threads()])
        s._started_once = True
        s._on_done({"status": "skipped", "info": {}})
        self.assertEqual(s.label.text(), "DART 대기")
        self.assertTrue(_pump(lambda: _FakeThread.runs == [True]))

    def test_disabled_never_runs(self):
        s, _ = self._sync(enabled=False)
        s.arm(universe_ready=True)
        _pump(lambda: False, 0.3)
        self.assertEqual(_FakeThread.runs, [])
        self.assertTrue(s.label.isHidden())

    def test_check_timer_runs_with_at_start_false_and_stop_ends_it(self):
        s = DartAutoSync(thread_factory=_FakeThread, start_delay_ms=10, check_interval_ms=100)
        self.addCleanup(lambda: [t.wait(3000) for t in s.threads()])
        s.arm(universe_ready=True)
        self.assertTrue(_pump(lambda: len(_FakeThread.runs) >= 2, 3.0))
        self.assertEqual(_FakeThread.runs[:2], [True, False])
        s.stop()
        _pump(lambda: not s.is_running())
        n = len(_FakeThread.runs)
        _pump(lambda: False, 0.4)
        self.assertEqual(len(_FakeThread.runs), n)


_CLOSE_SCRIPT = textwrap.dedent(r'''
    import os, sys, time
    tmp = sys.argv[1]
    sys.path.insert(0, sys.argv[2])
    import paths, trade_db
    paths.UNIVERSE_CACHE_FILE = os.path.join(tmp, "universe_cache.json")
    paths.CUSTOM_SETTINGS_FILE = os.path.join(tmp, "custom_settings.json")
    paths.DART_DB_FILE = os.path.join(tmp, "dart.db")
    paths.DART_CACHE_DIR = os.path.join(tmp, "dart")
    trade_db._DB_FILE = os.path.join(tmp, "portfolio.db")
    trade_db.TRADING_RECORD_FILE = os.path.join(tmp, "trading_record.json")
    from PyQt6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tmp)
    import data.dart.sync as ds
    from data.dart.sync import SyncResult
    state = {"started": False}

    def fake_run(mode, trigger, cancel=None, on_progress=None, **kw):
        state["started"] = True
        while not cancel.is_set():          # a long collection: one report every 0.2 s
            time.sleep(0.2)
            on_progress(1, 100, "financials x")
        return SyncResult("partial", "r", error="cancelled")
    ds.run_sync = fake_run
    ds.collection_status = lambda *a, **k: {"list_done": True, "periods_pending": 0}
    ds.is_due = lambda *a, **k: True
    import ui.dart_sync_thread as dst
    dst.START_DELAY_MS = 0
    import main
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    w = main.MainWindow(initial_refresh=False, run_backup=False)
    w.global_auto_timer.stop()
    w.dart_sync._delay_timer.setInterval(0)
    w.show()
    end = time.monotonic() + 10
    while time.monotonic() < end and not (state["started"] and w.dart_sync.is_running()):
        app.processEvents(); time.sleep(0.01)
    print("RUNNING", w.dart_sync.is_running())
    t = w.dart_sync.thread
    t0 = time.monotonic()
    w.close()
    print("CLOSE_FAST", (time.monotonic() - t0) < 2.0)
    print("STOPPED", not t.isRunning())
    sys.stdout.flush()
    os._exit(0)
''')


class TestMainWindowClose(unittest.TestCase):
    def test_closing_the_window_stops_collection_within_2s(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
            proc = subprocess.run([sys.executable, "-c", _CLOSE_SCRIPT, tmp, _SRC], cwd=_SRC, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=120)
        for flag in ("RUNNING", "CLOSE_FAST", "STOPPED"):
            self.assertIn(f"{flag} True", proc.stdout,
                          f"{flag} failed\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr[-3000:]}")


if __name__ == "__main__":
    unittest.main()
