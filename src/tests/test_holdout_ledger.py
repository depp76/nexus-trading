"""tests/test_holdout_ledger.py — trend_following.md 10-7 (2026-11-18): every
Strategy-tab research run whose dates reach past DEV_END leaves one line in
the hold-out ledger before any data is loaded; runs inside the development
window leave none. A temporary ledger file only."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from strategy.trend_following import DEV_END, SPEC_VERSION, ResearchRequest  # noqa: E402
from threads import strategy_threads  # noqa: E402
from threads.strategy_threads import TrendFollowingResearchThread, record_holdout_run  # noqa: E402


class TestHoldoutLedger(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _lines(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def test_dev_window_run_writes_nothing(self):
        req = ResearchRequest(start=date(2021, 1, 4), end=DEV_END, variant_ids=("C1",))
        self.assertIsNone(record_holdout_run(req, self.path))
        self.assertEqual(self._lines(), [])

    def test_holdout_run_appends_one_line_each(self):
        req = ResearchRequest(start=date(2021, 1, 4), end=date(2026, 11, 18), variant_ids=("A1", "C1"),
                              allow_holdout=True)
        now = datetime(2026, 11, 18, 9, 30)
        record_holdout_run(req, self.path, now)
        record_holdout_run(req, self.path, now)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], {
            "opened_at": "2026-11-18T09:30:00", "source": "strategy_tab", "spec_version": SPEC_VERSION,
            "start": "2021-01-04", "end": "2026-11-18", "variants": ["A1", "C1"], "markets": ["KOSPI"],
            "cost_multipliers": list(req.cost_multipliers)})

    def test_written_before_the_data_load(self):
        """Even a run that fails or is cancelled while loading counts as opened."""
        req = ResearchRequest(start=date(2021, 1, 4), end=date(2025, 6, 30), variant_ids=("C1",),
                              allow_holdout=True)
        t = TrendFollowingResearchThread(req)
        got = []
        t.finished.connect(lambda r, e: got.append((r, e)))
        with patch.object(strategy_threads, "HOLDOUT_LEDGER_FILE", self.path), \
                patch.object(strategy_threads.record_holdout_run, "__defaults__", (self.path, None)), \
                patch.object(strategy_threads, "load_dataset", side_effect=RuntimeError("offline")):
            t.run()
        self.assertEqual(got, [(None, "offline")])
        self.assertEqual([x["end"] for x in self._lines()], ["2025-06-30"])

    def test_unwritable_ledger_does_not_stop_the_run(self):
        req = ResearchRequest(start=date(2021, 1, 4), end=date(2025, 6, 30), variant_ids=("C1",),
                              allow_holdout=True)
        rec = record_holdout_run(req, os.path.join(self.path, "no", "such", "dir.jsonl"))
        self.assertEqual(rec["end"], "2025-06-30")


if __name__ == "__main__":
    unittest.main()
