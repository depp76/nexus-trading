"""NEX-12: development-window guard (DEV_END = 2024-12-31) in load_dataset,
ResearchRequest and run_research. Synthetic data only, no network."""
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
import polars as pl

from strategy.trend_following.config import DEV_END, PERIODS
from strategy.trend_following.dataset import HoldoutGuardError, load_dataset
from strategy.trend_following.research import ResearchRequest, run_research
from tests.strategy.trend_following.helpers import business_days, flow_frame, make_frame, path

N = 800                                   # sessions from 2022-01-03: runs into 2025
DATES = business_days(date(2022, 1, 3), N)
CLOSES = path(10_000.0, [(N, 0.001)])
LISTING = [{"Code": "000001", "Name": "AAA"}, {"Code": "000002", "Name": "BBB"}]


def _fake_history(code, start, *a, **k):
    return make_frame(DATES, CLOSES * (2.0 if code == "000002" else 1.0))


def _patches():
    base = "strategy.trend_following.dataset."
    return (
        patch(base + "get_historical_data", side_effect=_fake_history),
        patch(base + "_load_listing", return_value=LISTING),
        patch(base + "get_investor_flows", side_effect=lambda c, s: flow_frame(DATES, CLOSES, [(N, 100.0)])),
        patch(base + "get_cd91_series", side_effect=lambda s, e: pl.DataFrame({"Date": DATES, "Rate": np.full(N, 3.0)})),
    )


class TestDatasetCutoff(unittest.TestCase):
    def _load(self, **kw):
        ps = _patches()
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        return load_dataset(date(2023, 6, 1), include_flows=True, min_rows=10, **kw)

    def test_fixture_really_contains_rows_after_the_guard(self):
        self.assertGreater(DATES[-1], date(2025, 1, 2))

    def test_default_end_is_cut_at_dev_end(self):
        ds = self._load()
        self.assertLessEqual(ds.dates[-1], DEV_END)
        self.assertEqual(ds.dates[-1], max(d for d in DATES if d <= DEV_END))
        for arr in (ds.index_close, ds.stocks.close, ds.flow_fi, ds.bm.close, ds.rf_annual):
            self.assertEqual(arr.shape[0], len(ds.dates))
        self.assertEqual(ds.info["cutoff"], DEV_END.isoformat())

    def test_end_inside_window_is_respected(self):
        ds = self._load(end=date(2024, 6, 28))
        self.assertLessEqual(ds.dates[-1], date(2024, 6, 28))

    def test_end_after_dev_end_raises_without_flag(self):
        for end in (date(2025, 3, 31), date(2025, 1, 1)):
            with self.assertRaises(HoldoutGuardError):
                load_dataset(date(2023, 6, 1), end)

    def test_start_in_holdout_raises(self):
        with self.assertRaises(HoldoutGuardError):
            load_dataset(date(2025, 2, 3), None)

    def test_flag_lifts_the_guard(self):
        ds = self._load(end=date(2025, 1, 31), allow_holdout=True)
        self.assertGreater(ds.dates[-1], DEV_END)

    def test_request_guard(self):
        with self.assertRaises(HoldoutGuardError):
            ResearchRequest(start=date(2021, 1, 4), end=date(2025, 6, 30))
        ResearchRequest(start=date(2021, 1, 4), end=DEV_END)
        ResearchRequest(start=date(2021, 1, 4), end=date(2025, 6, 30), allow_holdout=True)

    def test_run_research_refuses_a_dataset_past_the_guard(self):
        ds = self._load(end=date(2025, 1, 31), allow_holdout=True)
        req = ResearchRequest(start=date(2023, 6, 1), variant_ids=("A0",), cost_multipliers=(1.0,), run_event_study=False)
        with self.assertRaises(HoldoutGuardError):
            run_research(ds, req)

    def test_periods_table_is_untouched(self):
        self.assertEqual(PERIODS[-1], ("2025-26", 2025, 2026))      # spec 6-4 wording belongs to NEX-13


if __name__ == "__main__":
    unittest.main()
