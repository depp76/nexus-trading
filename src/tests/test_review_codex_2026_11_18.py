"""tests/test_review_codex_2026_11_18.py — regression tests for the four
findings of review_codex.md (2026-11-18, base 5cbb2ca):

1. a snapshot is published only when complete and verified, and never over an
   existing one (strategy/trend_following/snapshot.py, run.build_snapshot);
2. a language switch does not wait on the UI thread or kill workers: the old
   window is hidden and retired after its last worker ends (ui.common.retire_when_idle);
3. a strategy-signal run started before a Universe refresh does not refill the
   shared SignalCache or pin old ranks (SignalCache.generation);
4. reports finishing in the same second are kept side by side (run.write_report).
"""
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from strategy.signal_api import SignalCache
from strategy.trend_following import run as run_mod
from strategy.trend_following import snapshot as snap_mod
from strategy.trend_following.snapshot import (
    SnapshotExistsError, dataset_sha256, load_snapshot, read_manifest, save_snapshot,
)
from tests.strategy.trend_following.helpers import synthetic_dataset

app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 1. snapshots
# ---------------------------------------------------------------------------
class TestSnapshotIsPublishedWhole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_existing_snapshot_is_never_overwritten(self):
        first = save_snapshot(self.ds, self.root / "s")
        other = synthetic_dataset(index_up=False, with_flows=False)      # different data, same name
        with self.assertRaises(SnapshotExistsError):
            save_snapshot(other, self.root / "s")
        back = load_snapshot(self.root / "s")                 # still the original data, hash intact
        self.assertEqual(dataset_sha256(back), first["sha256"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["s"])     # no staging left

    def test_a_failed_write_leaves_nothing_behind(self):
        real = snap_mod._file_sha256
        calls = []

        def second_write_fails(path):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path)

        with patch.object(snap_mod, "_file_sha256", side_effect=second_write_fails), self.assertRaises(OSError):
            save_snapshot(self.ds, self.root / "s")
        self.assertEqual(list(self.root.iterdir()), [])      # neither the target nor a staging folder

    def test_a_snapshot_that_does_not_read_back_is_not_published(self):
        with patch.object(snap_mod, "dataset_sha256", side_effect=["a" * 64, "b" * 64]), \
                self.assertRaises(snap_mod.SnapshotIntegrityError):
            save_snapshot(self.ds, self.root / "s")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_build_snapshot_refuses_an_existing_name_before_fetching(self):
        save_snapshot(self.ds, self.root / "taken")
        with patch.object(run_mod, "default_snapshot_dir", side_effect=lambda n: self.root / n), \
                patch.object(run_mod, "load_dataset") as load:
            with self.assertRaises(SnapshotExistsError):
                run_mod.build_snapshot("taken")
            load.assert_not_called()
            self.assertEqual(run_mod.main(["--build-snapshot", "taken"]), 2)       # CLI: message, exit 2
            load.assert_not_called()
        self.assertEqual(read_manifest(self.root / "taken")["sha256"], dataset_sha256(self.ds))


# ---------------------------------------------------------------------------
# 4. reports
# ---------------------------------------------------------------------------
class TestReportsDoNotOverwriteEachOther(unittest.TestCase):
    def test_same_second_reports_get_their_own_files(self):
        man = {"sha256": "f" * 64}
        now = datetime(2026, 11, 18, 13, 11, 34)
        with tempfile.TemporaryDirectory() as tmp:
            p1 = run_mod.write_report("first", tmp, "robustness", man, now=now)
            p2 = run_mod.write_report("second", tmp, "robustness", man, now=now)
            p3 = run_mod.write_report("third", tmp, "robustness", man, now=now)
            self.assertEqual(len({p1, p2, p3}), 3)
            self.assertTrue(p2.name.endswith("_20261118_131134_2.md"))
            self.assertEqual([p.read_text(encoding="utf-8") for p in (p1, p2, p3)], ["first", "second", "third"])


# ---------------------------------------------------------------------------
# 3. signal cache generations
# ---------------------------------------------------------------------------
def _picks(vid="C2"):
    from strategy.trend_following.signal_provider import pick_signals
    from tests.strategy.trend_following.test_scoring import score_book
    book, _ = score_book()
    return pick_signals(vid, book, None)


class TestSignalCacheGeneration(unittest.TestCase):
    def test_a_run_from_before_a_refresh_is_refused(self):
        cache = SignalCache(today=lambda: date(2026, 11, 18))
        gen = cache.generation
        cache.bump()                                        # a refresh started
        self.assertFalse(cache.put(_picks(), generation=gen))
        self.assertIsNone(cache.get("C2"))
        self.assertTrue(cache.put(_picks(), generation=cache.generation))
        self.assertTrue(cache.put(_picks("C1")))             # no generation: unchecked (direct callers)

    def test_clear_keeps_the_pinned_run_but_starts_a_new_generation(self):
        cache = SignalCache(today=lambda: date(2026, 11, 18))
        pinned = _picks()
        cache.put(pinned)
        gen = cache.generation
        cache.clear(keep=pinned)
        self.assertIs(cache.get("C2"), pinned)
        self.assertGreater(cache.generation, gen)

    def _universe_tab(self):
        from tests.test_universe_tab import TestSignalButton
        p = patch("ui.universe_tab.atomic_save_json")
        p.start()
        self.addCleanup(p.stop)
        return TestSignalButton._tab(self)

    def test_universe_tab_drops_a_run_that_lands_after_a_refresh(self):
        from threads.strategy_threads import SignalPicksThread
        tab = self._universe_tab()
        worker = SignalPicksThread([], "C2")                # never started; its signal is emitted by hand
        worker.cache_generation = tab._signal_cache.generation
        worker.finished.connect(tab._on_signals_finished)
        with patch.object(tab, "_reload_table"), patch.object(tab, "update_status_display"), \
                patch("ui.universe_tab.AllDataFetchThread"):
            tab.refresh_data()                               # the rows are being replaced
        worker.finished.emit(_picks(), "")
        self.assertIsNone(tab._signal_cache.get("C2"))
        self.assertFalse(tab.signal_btn.isChecked())
        self.assertEqual(tab._signal_order, {})
        # a run started now is current and pins as before
        fresh = SignalPicksThread([], "C2")
        fresh.cache_generation = tab._signal_cache.generation
        fresh.finished.connect(tab._on_signals_finished)
        picks = _picks()
        fresh.finished.emit(picks, "")
        self.assertIs(tab._signal_cache.get("C2"), picks)
        self.assertTrue(tab.signal_btn.isChecked())

    def test_chart_calc_on_old_rows_is_not_cached(self):
        from threads.strategy_threads import SignalPicksThread
        from ui.strategy_check_section import SignalContext, StrategyCheckSection
        cache = SignalCache()
        ctx = SignalContext(items=[{"ticker": "X", "market": "KOSPI"}], cache=cache, generation=cache.generation)
        with patch.object(StrategyCheckSection, "_run_stock"):
            sec = StrategyCheckSection("X", "X", "KOSPI", ctx)
        cache.clear()                                        # the Universe refreshed after the chart opened
        worker = SignalPicksThread(ctx.items, "C2")
        worker.cache_generation = ctx.generation
        worker.finished.connect(sec._on_universe_done)
        sec._running.add("C2")
        worker.finished.emit(_picks(), "")
        self.assertIsNone(cache.get("C2"))
        self.assertIn("유니버스가 새로 고쳐져", sec._errors["C2"])
        self.assertNotIn("C2", sec._running)


# ---------------------------------------------------------------------------
# 2. language switch: retire the old window after its workers, without blocking
# ---------------------------------------------------------------------------
class _SlowWorker(QThread):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def run(self):
        self.release.wait(10)


class _FakeWindow:
    def __init__(self):
        self.hidden = False
        self.retired = False

    def hide(self):
        self.hidden = True

    def retire(self):
        self.retired = True


class TestRetireWhenIdle(unittest.TestCase):
    def test_old_window_waits_for_its_worker_without_blocking_the_ui(self):
        from ui.common import retire_when_idle
        worker = _SlowWorker()
        worker.start()
        self.addCleanup(lambda: (worker.release.set(), worker.wait(3000)))
        old = _FakeWindow()
        t0 = time.monotonic()
        retire_when_idle(app, old, [worker])
        self.assertLess(time.monotonic() - t0, 0.5)              # returned at once
        self.assertTrue(old.hidden)
        self.assertFalse(old.retired)
        self.assertIn(old, app._nexus_retiring)
        ticks = []
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: ticks.append(1))
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            app.processEvents()
        self.assertEqual(ticks, [1])                             # UI events kept flowing
        self.assertFalse(old.retired)                            # worker still running
        self.assertTrue(worker.isRunning())                      # and not killed
        worker.release.set()
        deadline = time.monotonic() + 3
        while not old.retired and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertTrue(old.retired)
        self.assertNotIn(old, app._nexus_retiring)

    def test_nothing_running_retires_at_once(self):
        from ui.common import retire_when_idle
        old = _FakeWindow()
        retire_when_idle(app, old, [])
        self.assertTrue(old.hidden and old.retired)


if __name__ == "__main__":
    unittest.main()
