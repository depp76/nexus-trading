"""tests/test_holdout_ledger.py — trend_following.md 10-7 (2026-11-18) and 10-15
(2026-11-20, review M10): every load whose window reaches past DEV_END leaves
one line in the hold-out ledger before any data is read -- written by the load
layer (``dataset.record_holdout_open`` from ``load_dataset`` / ``load_snapshot``),
so a Strategy-tab run, a script and a direct call are all recorded, each once.
Loads inside the development window leave none. A temporary ledger file only
(conftest.py points paths.HOLDOUT_LEDGER_FILE at one for every test)."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import paths  # noqa: E402
from strategy.trend_following import DEV_END, SPEC_VERSION, ResearchRequest  # noqa: E402
from strategy.trend_following import dataset as dataset_mod  # noqa: E402
from strategy.trend_following.dataset import load_dataset, record_holdout_open  # noqa: E402
from threads.strategy_threads import TrendFollowingResearchThread, holdout_record  # noqa: E402


class TestHoldoutLedger(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "holdout_ledger.jsonl")

    def tearDown(self):
        for root, dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.dir)

    def _lines(self, path=None):
        path = path or self.path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]

    def test_dev_window_writes_nothing(self):
        self.assertIsNone(record_holdout_open(date(2021, 1, 4), DEV_END, "x", ledger=self.path))
        self.assertEqual(self._lines(), [])

    def test_each_opening_appends_one_line(self):
        req = ResearchRequest(start=date(2021, 1, 4), end=date(2026, 11, 18), variant_ids=("A1", "C1"),
                              allow_holdout=True)
        now = datetime(2026, 11, 18, 9, 30)
        rec = holdout_record(req)
        for _ in range(2):
            record_holdout_open(req.start, req.end, rec["source"],
                                {"markets": ["KOSPI"], **{k: v for k, v in rec.items() if k != "source"}},
                                ledger=self.path, now=now)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], {
            "opened_at": "2026-11-18T09:30:00", "source": "strategy_tab", "spec_version": SPEC_VERSION,
            "start": "2021-01-04", "end": "2026-11-18", "variants": ["A1", "C1"], "markets": ["KOSPI"],
            "cost_multipliers": list(req.cost_multipliers)})

    def test_strategy_tab_run_is_recorded_once_before_the_data_load(self):
        """Even a run that fails or is cancelled while loading counts as opened; the thread no
        longer writes its own line, so the load layer's is the only one."""
        req = ResearchRequest(start=date(2021, 1, 4), end=date(2025, 6, 30), variant_ids=("C1",),
                              allow_holdout=True)
        t = TrendFollowingResearchThread(req)
        got = []
        t.finished.connect(lambda r, e: got.append((r, e)))
        with patch.object(paths, "HOLDOUT_LEDGER_FILE", self.path), \
                patch.object(dataset_mod, "get_historical_data", side_effect=RuntimeError("offline")):
            t.run()
        self.assertEqual(got, [(None, "offline")])
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["source"], lines[0]["start"], lines[0]["end"], lines[0]["variants"],
                          lines[0]["markets"]), ("strategy_tab", "2021-01-04", "2025-06-30", ["C1"], ["KOSPI"]))

    def test_direct_load_dataset_is_recorded(self):
        """review 2026-11-20 M10: a script calling load_dataset(..., allow_holdout=True) used to
        open the hold-out without a ledger line (only the Strategy-tab thread wrote one)."""
        with patch.object(paths, "HOLDOUT_LEDGER_FILE", self.path), \
                patch.object(dataset_mod, "get_historical_data", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                load_dataset(date(2021, 1, 4), date(2025, 3, 31), allow_holdout=True)
            with self.assertRaises(RuntimeError):     # allowed but inside the window: not an opening
                load_dataset(date(2021, 1, 4), DEV_END, allow_holdout=True)
        lines = self._lines()
        self.assertEqual([(x["source"], x["end"]) for x in lines], [("load_dataset", "2025-03-31")])

    def test_tests_never_touch_the_real_ledger(self):
        """conftest.py points the ledger at a per-test temporary file (2026-11-18: two Strategy-tab
        tests once wrote false "opened" lines into the real file)."""
        real = os.path.join(paths.BASE_DIR, "holdout_ledger.jsonl")
        self.assertNotEqual(os.path.abspath(paths.HOLDOUT_LEDGER_FILE), os.path.abspath(real))

    def test_missing_folder_is_created(self):
        path = os.path.join(self.dir, "no", "such", "dir", "ledger.jsonl")
        rec = record_holdout_open(date(2021, 1, 4), date(2025, 6, 30), "x", ledger=path)
        self.assertEqual(rec["end"], "2025-06-30")
        self.assertEqual([x["end"] for x in self._lines(path)], ["2025-06-30"])

    def test_unwritable_ledger_does_not_stop_the_run(self):
        blocker = os.path.join(self.dir, "a_file")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        rec = record_holdout_open(date(2021, 1, 4), date(2025, 6, 30), "x",
                                  ledger=os.path.join(blocker, "sub", "ledger.jsonl"))
        self.assertEqual(rec["end"], "2025-06-30")


if __name__ == "__main__":
    unittest.main()
