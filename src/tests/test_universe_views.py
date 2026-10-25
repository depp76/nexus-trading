"""tests/test_universe_views.py — Trading Universe range filters and saved
views (docs/ui.md 2.6 / 8.9): the pure matcher, the table's row values and
filtering, and the tab's Views list."""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.universe_views import (
    BUILTIN_VIEWS, count_matches, describe_ranges, describe_view, empty_state, matches, normalize, same_state,
)

app = QApplication.instance() or QApplication([])


def _row(**kw):
    values = {"tper": None, "range52w": None, "chg": 0.0, "cap": None}
    values.update(kw.pop("values", {}))
    base = {"market": "KOSPI", "status": "-", "name": "Samsung", "ticker": "005930", "mode": "pct", "values": values}
    base.update(kw)
    return base


class TestMatches(unittest.TestCase):
    def test_market_status_search(self):
        r = _row(status="On")
        self.assertTrue(matches(r, empty_state()))
        self.assertFalse(matches(r, {"market": "KOSDAQ"}))
        self.assertTrue(matches(r, {"status": "On"}))
        self.assertFalse(matches(r, {"status": "Tg"}))
        self.assertTrue(matches(r, {"search": "sams"}))
        self.assertTrue(matches(r, {"search": "0059"}))
        self.assertFalse(matches(r, {"search": "hynix"}))

    def test_ranges_and_units(self):
        r = _row(values={"tper": 8.0, "range52w": 0.95})
        self.assertTrue(matches(r, {"ranges": {"tper": [0, 12]}}))
        self.assertFalse(matches(r, {"ranges": {"tper": [10, None]}}))
        self.assertTrue(matches(r, {"ranges": {"range52w": [90, None]}}))    # typed in %, stored 0..1
        self.assertFalse(matches(r, {"ranges": {"range52w": [None, 90]}}))

    def test_missing_value_never_matches_a_filtered_column(self):
        self.assertFalse(matches(_row(), {"ranges": {"tper": [None, 12]}}))

    def test_change_filters_skip_rows_not_in_percent(self):
        bond = _row(mode="bp", values={"chg": 3.0})
        self.assertFalse(matches(bond, {"ranges": {"chg": [0, None]}}))
        self.assertTrue(matches(bond, {"ranges": {"tper": [None, None]}}))  # empty bounds = no filter

    def test_builtin_views_and_counts(self):
        rows = [_row(values={"tper": 8.0}), _row(values={"tper": 30.0}), _row(values={"tper": -5.0}),
                _row(values={"range52w": 0.93})]
        low_per, near_high = BUILTIN_VIEWS
        self.assertEqual(count_matches(rows, low_per), 1)      # negative PER is not "low"
        self.assertEqual(count_matches(rows, near_high), 1)

    def test_normalize_same_state_and_descriptions(self):
        a = normalize({"market": "KOSPI", "search": "  x ", "ranges": {"tper": [None, 12], "bogus": [1, 2],
                                                                      "fper": [None, None]}})
        self.assertEqual(a, {"market": "KOSPI", "status": "", "search": "x", "ranges": {"tper": [None, 12.0]}})
        self.assertTrue(same_state(a, dict(a, name="mine")))
        self.assertEqual(describe_ranges({"tper": [0, 12], "range52w": [90, None]}),
                         "tPER 0–12 · 52W position ≥ 90")
        self.assertEqual(describe_view({"status": "On", "ranges": {}}), "Held")
        self.assertEqual(describe_view(empty_state()), "No filter")


def _stock(ticker, market="KOSPI", per=None, price=100.0, low=None, high=None, mode="pct", chg=0.0):
    changes = {"1d": chg}
    if low is not None:
        changes.update({"52w_low": low, "52w_high": high})
    item = {"ticker": ticker, "name": f"S{ticker}", "market": market, "market_cap": 10**12, "price": price,
            "change_mode": mode, "changes": changes}
    if per is not None:
        item["trailing_per"] = per
    return item


class _TabCase(unittest.TestCase):
    def _tab(self, data):
        from ui.universe_tab import UniverseTab
        with patch.object(UniverseTab, "refresh_data"), \
             patch("ui.universe_tab.safe_load_json", return_value=None):
            tab = UniverseTab()
        tab.save_custom_settings = lambda: None      # never touch the real custom_settings.json
        tab.all_data = data
        tab._reload_table()
        tab.filter_table()
        return tab

    def _visible(self, tab):
        t = tab.table
        return sorted(t.row_values(r)["ticker"] for r in range(t.rowCount()) if not t.isRowHidden(r))


class TestTableFiltering(_TabCase):
    def test_row_values_and_range_filter(self):
        tab = self._tab([_stock("A", price=95, low=50, high=100), _stock("B", price=60, low=50, high=100),
                         _stock("C")])
        vals = {tab.table.row_values(r)["ticker"]: tab.table.row_values(r)["values"]
                for r in range(tab.table.rowCount())}
        self.assertAlmostEqual(vals["A"]["range52w"], 0.9)
        self.assertIsNone(vals["C"]["range52w"])              # no 52W range yet -> no value
        tab._on_ranges_applied({"range52w": [85, None]})
        self.assertEqual(self._visible(tab), ["A"])
        self.assertEqual(tab.filters_btn.text(), "Filters (1)")
        self.assertTrue(tab.filters_btn.isChecked())
        tab._on_ranges_applied({})
        self.assertEqual(self._visible(tab), ["A", "B", "C"])
        self.assertEqual(tab.filters_btn.text(), "Filters")

    def test_tper_filter_and_low_per_view_count(self):
        tab = self._tab([_stock("A", per=8.0), _stock("B", per=25.0), _stock("C")])
        tab._on_ranges_applied({"tper": [0, 12]})
        self.assertEqual(self._visible(tab), ["A"])
        self.assertIn("Low tPER  (1)", [tab.views_combo.itemText(i) for i in range(tab.views_combo.count())])


class TestViews(_TabCase):
    def _texts(self, tab):
        c = tab.views_combo
        return [c.itemText(i) for i in range(c.count())]

    def test_counts_and_applying_a_builtin_view(self):
        tab = self._tab([_stock("A", price=99, low=50, high=100), _stock("B", price=60, low=50, high=100)])
        texts = self._texts(tab)
        self.assertIn("Near 52W High  (1)", texts)
        self.assertIn("Low tPER  (0)", texts)
        idx = texts.index("Near 52W High  (1)")
        tab._on_view_activated(idx)
        self.assertEqual(self._visible(tab), ["A"])
        self.assertEqual(tab.views_combo.currentText(), "Near 52W High  (1)")  # the active view is selected
        clear = self._texts(tab).index("Show all (clear filters)")
        tab._on_view_activated(clear)
        self.assertEqual(self._visible(tab), ["A", "B"])
        self.assertEqual(tab.views_combo.currentIndex(), 0)

    def test_save_and_delete_a_user_view(self):
        tab = self._tab([_stock("A", market="KOSPI"), _stock("B", market="KOSDAQ")])
        tab._on_market_filter_changed("KOSDAQ")
        with patch("ui.universe_tab.QInputDialog.getText", return_value=("Kosdaq only", True)):
            tab._on_view_activated(self._texts(tab).index("Save current filters as a view..."))
        self.assertEqual(tab.custom_settings["views"][0]["name"], "Kosdaq only")
        self.assertEqual(tab.custom_settings["views"][0]["market"], "KOSDAQ")
        self.assertIn("Kosdaq only  (1)", self._texts(tab))
        tab.apply_view_state(empty_view_state())
        self.assertEqual(self._visible(tab), ["A", "B"])
        with patch("ui.universe_tab.QInputDialog.getItem", return_value=("Kosdaq only", True)), \
             patch("ui.universe_tab.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            tab._on_view_activated(self._texts(tab).index("Delete a saved view..."))
        self.assertEqual(tab.custom_settings["views"], [])
        self.assertFalse(any(t.startswith("Kosdaq only") for t in self._texts(tab)))

    def test_builtin_names_are_reserved(self):
        tab = self._tab([_stock("A")])
        tab._on_market_filter_changed("KOSPI")
        with patch("ui.universe_tab.QInputDialog.getText", return_value=("Low tPER", True)), \
             patch("ui.universe_tab.QMessageBox.warning") as warn:
            tab._on_view_activated(self._texts(tab).index("Save current filters as a view..."))
        warn.assert_called_once()
        self.assertEqual(tab.custom_settings.get("views", []), [])


def empty_view_state():
    return empty_state()


if __name__ == "__main__":
    unittest.main()
