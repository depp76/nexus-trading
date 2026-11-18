"""tests/test_ma60_conversion.py — 2026-11-18 (user direction, spec v07 10-13):
every remaining MA50 became MA60 -- the long divergence ratio (data and the MA
chart's divergence panel), the Universe "MA60 Div" column / range filter, and
saved views that filtered "MA50 Div" are moved to "MA60 Div" when read."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from data.indicators import _compute_indicators, fetch_historical_changes  # noqa: E402
from ui.universe_columns import COLUMNS, COL_MA60DIV  # noqa: E402
from ui.universe_views import (  # noqa: E402
    FIELD_BY_KEY, LEGACY_RANGE_KEYS, matches, migrate_ranges, migrate_views, normalize,
)


def _frame(n=120):
    close = 100 * np.cumprod(1 + np.random.default_rng(2).normal(0.001, 0.02, n))
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": np.full(n, 1e5)})


class TestData(unittest.TestCase):

    def test_divergence_is_against_ma60(self):
        df = _compute_indicators(_frame(), (20,))
        close = df["Close"].to_numpy()
        self.assertNotIn("MA50", df.columns)
        self.assertNotIn("MA50_Div", df.columns)
        self.assertAlmostEqual(df["MA60"][-1], close[-60:].mean())
        self.assertAlmostEqual(df["MA60_Div"][-1], close[-1] / close[-60:].mean() * 100)

    def test_a_60_in_windows_is_not_repeated(self):
        df = _compute_indicators(_frame(), (10, 20, 60))          # fetch_stock_ma_multi's default
        self.assertEqual([c for c in df.columns if c.startswith("MA") and not c.endswith("Div")],
                         ["MA10", "MA20", "MA60"])

    def test_universe_changes_key(self):
        f = _frame(80)
        cp = float(f["Close"][-1])
        ch = fetch_historical_changes("T", cp, f, mode="pct")
        self.assertAlmostEqual(ch["ma60_div"], cp / f["Close"].to_numpy()[-60:].mean() * 100)
        self.assertNotIn("ma50_div", ch)
        short = fetch_historical_changes("T", cp, _frame(55), mode="pct")
        self.assertEqual(short["ma60_div"], 0.0)                   # fewer than 60 closes: "-"


class TestUniverseColumnAndFilter(unittest.TestCase):

    def test_column_and_filter_are_ma60(self):
        self.assertEqual((COLUMNS[COL_MA60DIV].key, COLUMNS[COL_MA60DIV].label), ("ma60div", "MA60 Div"))
        self.assertIn("ma60div", FIELD_BY_KEY)
        self.assertNotIn("ma50div", FIELD_BY_KEY)
        self.assertEqual(LEGACY_RANGE_KEYS, {"ma50div": "ma60div"})

    def test_legacy_key_is_moved_with_its_bounds(self):
        self.assertEqual(normalize({"ranges": {"ma50div": [95, 110]}})["ranges"], {"ma60div": [95.0, 110.0]})
        self.assertEqual(migrate_ranges({"ma50div": [1, 2], "ma60div": [3, 4]}), ({"ma60div": [3, 4]}, True))
        self.assertEqual(migrate_ranges({"tper": [0, 12]}), ({"tper": [0, 12]}, False))
        row = {"market": "KOSPI", "status": "-", "name": "X", "ticker": "X", "mode": "pct",
               "values": {"ma60div": 104.0}}
        self.assertTrue(matches(row, normalize({"ranges": {"ma50div": [100, 110]}})))

    def test_saved_views_are_rewritten_once_and_announced(self):
        views = [{"name": "MA 위", "market": "ALL", "status": "", "search": "", "ranges": {"ma50div": [100.0, None]}},
                 {"name": "저PER", "market": "ALL", "status": "", "search": "", "ranges": {"tper": [0.0, 12.0]}}]
        self.assertTrue(migrate_views(views))
        self.assertEqual(views[0]["ranges"], {"ma60div": [100.0, None]})
        self.assertFalse(migrate_views(views))                      # already moved

        from ui.universe_tab import UniverseTab
        settings = {"views": [{"name": "old", "market": "ALL", "status": "", "search": "",
                               "ranges": {"ma50div": [None, 98.0]}}]}

        def fake_load(path, default=None):
            return settings if str(path).endswith("custom_settings.json") else default
        with patch("ui.universe_tab.safe_load_json", side_effect=fake_load), \
                patch("ui.universe_tab.atomic_save_json") as save:
            tab = UniverseTab()
            got = []
            tab.status_message.connect(got.append)
            QApplication.processEvents()
        self.assertEqual(tab.custom_settings["views"][0]["ranges"], {"ma60div": [None, 98.0]})
        save.assert_called()
        self.assertEqual(len([m for m in got if "MA60 Div" in m]), 1)


if __name__ == "__main__":
    unittest.main()
