"""tests/test_refresh_status.py — auto-refresh failure reporting and the
Universe heatmap legend (docs/ui.md 8.7).

UniverseLightweightFetchThread imports its fetch functions from the
`data_fetcher` facade inside run(), so these tests patch the facade names
(the project's usual rule is to patch the implementation modules; here the
facade binding is the one the thread actually calls).
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from threads.fetch_threads import UniverseLightweightFetchThread
from ui.common import refresh_state

app = QApplication.instance() or QApplication([])

_KR = [{"ticker": "005930", "market": "KOSPI", "price": 100.0, "changes": {}}]


def _run(thread):
    failures, finished = [], []
    thread.failed.connect(failures.append)
    thread.finished_all.connect(finished.append)
    thread.run()   # synchronous: signals are delivered directly
    return failures, finished


class TestLightweightFailure(unittest.TestCase):
    def test_no_quotes_is_a_failure(self):
        with patch("data_fetcher.fetch_naver_realtime_prices", return_value={}):
            failures, finished = _run(UniverseLightweightFetchThread(_KR))
        self.assertEqual(failures, ["no quotes received (Naver/Yahoo)"])
        self.assertEqual(len(finished), 1)   # finished_all still fires (table keeps its data)

    def test_exception_is_a_failure(self):
        with patch("data_fetcher.fetch_naver_realtime_prices", side_effect=ConnectionError("timed out")):
            failures, _ = _run(UniverseLightweightFetchThread(_KR))
        self.assertEqual(failures, ["timed out"])

    def test_quotes_received_is_not_a_failure(self):
        with patch("data_fetcher.fetch_naver_realtime_prices", return_value={"005930": 100.0}):
            failures, finished = _run(UniverseLightweightFetchThread(_KR))
        self.assertEqual(failures, [])
        self.assertEqual(len(finished), 1)


class TestUniverseForwardsFailure(unittest.TestCase):
    def test_failed_tick_keeps_sync_time_and_reports_reason(self):
        from ui.universe_tab import UniverseTab
        with patch.object(UniverseTab, "refresh_data"), \
             patch("ui.universe_tab.safe_load_json", return_value=None):
            tab = UniverseTab()
        reasons, syncs, statuses = [], [], []
        tab.refresh_failed.connect(reasons.append)
        tab.sync_time_changed.connect(syncs.append)
        tab.status_text_changed.connect(statuses.append)
        tab._on_universe_lightweight_failed("no quotes received (Naver/Yahoo)")
        tab._on_universe_lightweight_loaded(list(tab.all_data))
        self.assertEqual(reasons, ["no quotes received (Naver/Yahoo)"])
        self.assertEqual(syncs, [])                       # last good update time is kept
        self.assertTrue(statuses and statuses[-1].startswith("Auto-update failed"))
        # the next good tick clears it
        tab._on_universe_lightweight_loaded(list(tab.all_data))
        self.assertEqual(len(syncs), 1)
        self.assertEqual(len(reasons), 1)


class TestRefreshState(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 10, 0, 0)

    def state(self, enabled=True, sync_ago=None, fail_ago=None):
        sync = None if sync_ago is None else self.NOW - timedelta(seconds=sync_ago)
        fail = None if fail_ago is None else (self.NOW - timedelta(seconds=fail_ago), "boom")
        return refresh_state(enabled, sync, fail, self.NOW, 60000)[0]

    def test_states(self):
        self.assertEqual(self.state(enabled=False, fail_ago=1), "off")
        self.assertEqual(self.state(), "waiting")
        self.assertEqual(self.state(sync_ago=30), "ok")
        self.assertEqual(self.state(sync_ago=121), "late")
        self.assertEqual(self.state(sync_ago=60, fail_ago=1), "failed")   # failure newer than success
        self.assertEqual(self.state(sync_ago=1, fail_ago=60), "ok")       # recovered since
        self.assertEqual(self.state(fail_ago=5), "failed")               # never succeeded

    def test_failed_tooltip_has_time_and_reason(self):
        _, tip = refresh_state(True, None, (self.NOW, "boom"), self.NOW, 60000)
        self.assertEqual(tip, "Update failed at 10:00:00: boom")


class TestHeatLegend(unittest.TestCase):
    def test_text_follows_column_scales(self):
        from ui.widgets import HeatLegend, COLUMNS
        scale = {c.key: c.scale for c in COLUMNS}
        text = HeatLegend.scale_text()
        self.assertIn(f"Chg ±{scale['chg']:g}%", text)
        self.assertIn(f"±{scale['d3']:g}%", text)
        self.assertIn(f"MA Div ±{scale['ma20div']:g} from 100", text)

    def test_universe_footer_shows_legend(self):
        from ui.universe_tab import UniverseTab
        with patch.object(UniverseTab, "refresh_data"), \
             patch("ui.universe_tab.safe_load_json", return_value=None):
            tab = UniverseTab()
        self.assertGreater(tab.heat_legend.sizeHint().width(), tab.heat_legend.BAR_W)


if __name__ == "__main__":
    unittest.main()
