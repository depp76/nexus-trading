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
        self.assertEqual(a, {"market": ["KOSPI"], "status": "", "search": "x", "ranges": {"tper": [None, 12.0]}})
        self.assertTrue(same_state(a, dict(a, name="mine")))
        self.assertEqual(describe_ranges({"tper": [0, 12], "range52w": [90, None]}),
                         "tPER 0–12 · 52W position ≥ 90")
        self.assertEqual(describe_view({"status": "On", "ranges": {}}), "보유")
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
        self.assertEqual(tab.filters_btn.text(), "필터 (1)")
        self.assertTrue(tab.filters_btn.isChecked())
        tab._on_ranges_applied({})
        self.assertEqual(self._visible(tab), ["A", "B", "C"])
        self.assertEqual(tab.filters_btn.text(), "필터")

    def test_tper_filter_and_low_per_view_count(self):
        tab = self._tab([_stock("A", per=8.0), _stock("B", per=25.0), _stock("C")])
        tab._on_ranges_applied({"tper": [0, 12]})
        self.assertEqual(self._visible(tab), ["A"])
        self.assertIn("저PER  (1)", [tab.views_combo.itemText(i) for i in range(tab.views_combo.count())])


class TestViews(_TabCase):
    def _texts(self, tab):
        c = tab.views_combo
        return [c.itemText(i) for i in range(c.count())]

    def test_counts_and_applying_a_builtin_view(self):
        tab = self._tab([_stock("A", price=99, low=50, high=100), _stock("B", price=60, low=50, high=100)])
        texts = self._texts(tab)
        self.assertIn("52주 신고가 근접  (1)", texts)
        self.assertIn("저PER  (0)", texts)
        idx = texts.index("52주 신고가 근접  (1)")
        tab._on_view_activated(idx)
        self.assertEqual(self._visible(tab), ["A"])
        self.assertEqual(tab.views_combo.currentText(), "52주 신고가 근접  (1)")  # the active view is selected
        clear = self._texts(tab).index("전체 보기 (필터 해제)")
        tab._on_view_activated(clear)
        self.assertEqual(self._visible(tab), ["A", "B"])
        self.assertEqual(tab.views_combo.currentIndex(), 0)

    def test_save_and_delete_a_user_view(self):
        tab = self._tab([_stock("A", market="KOSPI"), _stock("B", market="KOSDAQ")])
        tab.set_markets(["KOSDAQ"])                 # switching KOSPI off drops its rows
        self.assertEqual(self._visible(tab), ["B"])
        tab.search_input.setText("S")               # markets alone are not a filter
        with patch("ui.universe_tab.QInputDialog.getText", return_value=("Kosdaq only", True)):
            tab._on_view_activated(self._texts(tab).index("현재 필터를 보기로 저장..."))
        self.assertEqual(tab.custom_settings["views"][0]["name"], "Kosdaq only")
        self.assertEqual(tab.custom_settings["views"][0]["market"], ["KOSDAQ"])   # the view keeps the markets
        self.assertIn("Kosdaq only  (1)", self._texts(tab))
        tab.apply_view_state(empty_view_state())    # clear filters: markets stay as they are
        self.assertEqual(tab.selected_markets(), ["KOSDAQ"])
        self.assertEqual(self._visible(tab), ["B"])
        with patch("ui.universe_tab.QInputDialog.getItem", return_value=("Kosdaq only", True)), \
             patch("ui.universe_tab.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            tab._on_view_activated(self._texts(tab).index("저장된 보기 삭제..."))
        self.assertEqual(tab.custom_settings["views"], [])
        self.assertFalse(any(t.startswith("Kosdaq only") for t in self._texts(tab)))

    def test_builtin_names_are_reserved(self):
        tab = self._tab([_stock("A")])
        tab.search_input.setText("S")
        with patch("ui.universe_tab.QInputDialog.getText", return_value=("저PER", True)), \
             patch("ui.universe_tab.QMessageBox.warning") as warn:
            tab._on_view_activated(self._texts(tab).index("현재 필터를 보기로 저장..."))
        warn.assert_called_once()
        self.assertEqual(tab.custom_settings.get("views", []), [])


class TestMarketLists(unittest.TestCase):
    """docs/ui.md 2.1 (2026-11-14): `market` is the list of markets that are on."""

    def test_list_matches_and_index_rows_always_pass(self):
        state = {"market": ["KOSPI", "NASDAQ 100"]}
        self.assertTrue(matches(_row(market="KOSPI"), state))
        self.assertTrue(matches(_row(market="NASDAQ 100"), state))
        self.assertFalse(matches(_row(market="S&P500"), state))
        self.assertTrue(matches(_row(market="Index"), state))
        self.assertTrue(matches(_row(market="Index"), {"market": "KOSDAQ"}))   # an old single-market view

    def test_normalize_orders_and_drops_unknown_markets(self):
        self.assertEqual(normalize({"market": ["S&P500", "KOSPI", "bogus"]})["market"], ["KOSPI", "S&P500"])
        self.assertEqual(normalize({"market": []})["market"], "ALL")
        self.assertEqual(normalize({"market": "ALL"})["market"], "ALL")
        self.assertEqual(describe_view({"market": ["KOSPI", "NASDAQ 100"]}), "KOSPI+NASDAQ")


class _FakeFetch:
    """Stands in for AllDataFetchThread: records what it was asked to fetch."""
    instances = []

    def __init__(self, markets=(), include_indices=True):
        self.markets, self.include_indices = list(markets), include_indices
        self.started = False
        _FakeFetch.instances.append(self)
        for sig in ("market_progress", "market_loaded", "failed", "finished_all"):
            setattr(self, sig, _Sig())

    def start(self):
        self.started = True

    def isRunning(self):
        return self.started        # never returns from run(): the worst case for a queued finished_all

    def isFinished(self):
        return False


class _Sig:
    def connect(self, *_):
        pass


class TestMarketToggles(_TabCase):
    def setUp(self):
        _FakeFetch.instances = []

    def _patched(self):
        return patch("ui.universe_tab.AllDataFetchThread", _FakeFetch)

    def test_defaults_kospi_and_kosdaq_only(self):
        tab = self._tab([_stock("A")])
        checked = {m: b.isChecked() for m, b in tab._market_buttons.items()}
        self.assertEqual(checked, {"KOSPI": True, "KOSDAQ": True, "NASDAQ 100": False, "S&P500": False})
        self.assertEqual(tab._market_buttons["S&P500"].text(), "S&&P500")      # shows "S&P500", no mnemonic
        self.assertEqual(tab.selected_markets(), ["KOSPI", "KOSDAQ"])

    def test_saved_markets_are_restored(self):
        from ui.universe_tab import UniverseTab, CUSTOM_SETTINGS_FILE
        settings = {"markets": ["KOSPI", "S&P500"]}
        with patch.object(UniverseTab, "refresh_data"), \
             patch("ui.universe_tab.safe_load_json",
                   side_effect=lambda path, default=None: settings if path == CUSTOM_SETTINGS_FILE else default):
            tab = UniverseTab()
        self.assertEqual(tab.selected_markets(), ["KOSPI", "S&P500"])
        self.assertTrue(tab._market_buttons["S&P500"].isChecked())

    def test_the_last_market_cannot_be_switched_off(self):
        tab = self._tab([_stock("A")])
        with self._patched():
            tab._market_buttons["KOSDAQ"].click()
            tab._market_buttons["KOSPI"].click()
        self.assertEqual(tab.selected_markets(), ["KOSPI"])
        self.assertTrue(tab._market_buttons["KOSPI"].isChecked())

    def test_switching_a_market_on_fetches_only_it_and_merges(self):
        tab = self._tab([_stock("A"), _stock("B", market="KOSDAQ")])
        tab.custom_settings["deleted"] = ["GONE"]
        with self._patched():
            tab._market_buttons["NASDAQ 100"].click()
        self.assertEqual(tab.custom_settings["markets"], ["KOSPI", "KOSDAQ", "NASDAQ 100"])
        (fetch,) = _FakeFetch.instances
        self.assertEqual((fetch.markets, fetch.include_indices, fetch.started), (["NASDAQ 100"], False, True))
        tab._on_markets_fetched([_stock("AAPL", market="NASDAQ 100"), _stock("GONE", market="NASDAQ 100"),
                                 _stock("A", market="NASDAQ 100")])          # deleted / already here: skipped
        self.assertEqual(self._visible(tab), ["A", "AAPL", "B"])

    def test_switching_a_market_off_drops_its_rows_but_keeps_added_tickers(self):
        tab = self._tab([_stock("A"), _stock("B", market="KOSDAQ"), _stock("C", market="KOSDAQ")])
        tab.custom_settings["added"] = [{"ticker": "C", "market": "KOSDAQ"}]
        with self._patched():
            tab._market_buttons["KOSDAQ"].click()
        self.assertEqual(_FakeFetch.instances, [])
        self.assertEqual(sorted(x["ticker"] for x in tab.all_data), ["A", "C"])
        self.assertEqual(self._visible(tab), ["A"])                            # C stays, hidden

    def test_refresh_fetches_the_selected_markets(self):
        tab = self._tab([_stock("A")])
        tab._markets = {"KOSPI", "S&P500"}
        with self._patched():
            from ui.universe_tab import UniverseTab
            UniverseTab.refresh_data(tab)
        (fetch,) = _FakeFetch.instances
        self.assertEqual((fetch.markets, fetch.include_indices), (["KOSPI", "S&P500"], True))

    def test_a_stale_lightweight_tick_is_dropped(self):
        tab = self._tab([_stock("A"), _stock("B")])
        stale = [dict(x) for x in tab.all_data[:1]]        # rows changed while the tick ran
        tab._on_universe_lightweight_loaded(stale)
        self.assertEqual(len(tab.all_data), 2)

    def test_a_name_in_two_us_markets_moves_when_one_goes_off(self):
        """AAPL is in NASDAQ 100 and S&P500: switching NASDAQ off with S&P500
        on keeps it, under S&P500 (found by the 2026-11-14 toggle check)."""
        tab = self._tab([_stock("A")])
        with self._patched():
            tab._market_buttons["NASDAQ 100"].click()
            tab._on_markets_fetched([_stock("AAPL", market="NASDAQ 100"), _stock("NVDA", market="NASDAQ 100")])
            tab._market_buttons["S&P500"].click()
            tab._on_markets_fetched([_stock("AAPL", market="S&P500"), _stock("JPM", market="S&P500")])
            aapl = next(x for x in tab.all_data if x["ticker"] == "AAPL")
            self.assertEqual((aapl["market"], aapl["also_in"]), ("NASDAQ 100", ["S&P500"]))
            tab._market_buttons["NASDAQ 100"].click()
        self.assertEqual((aapl["market"], aapl["also_in"]), ("S&P500", ["NASDAQ 100"]))
        self.assertEqual(self._visible(tab), ["A", "AAPL", "JPM"])

    def test_an_added_ticker_does_not_count_as_its_market_being_loaded(self):
        tab = self._tab([_stock("A"), _stock("B", market="KOSDAQ"), _stock("C", market="KOSDAQ")])
        tab.custom_settings["added"] = [{"ticker": "C", "market": "KOSDAQ"}]
        with self._patched():
            tab._market_buttons["KOSDAQ"].click()                  # off: B goes, added C stays hidden
            tab._market_buttons["KOSDAQ"].click()                  # on again: KOSDAQ is fetched
        self.assertEqual([f.markets for f in _FakeFetch.instances], [["KOSDAQ"]])

    def test_a_market_switched_on_during_a_fetch_is_fetched_after_it(self):
        """finished_all is queued: the slot can run while the finishing QThread
        still reports isRunning(); the waiting market must not wait forever."""
        tab = self._tab([_stock("A"), _stock("B", market="KOSDAQ")])
        with self._patched():
            tab._market_buttons["NASDAQ 100"].click()
            tab._market_buttons["S&P500"].click()                  # first fetch still "running"
            self.assertEqual(len(_FakeFetch.instances), 1)
            tab._on_markets_fetched([_stock("NVDA", market="NASDAQ 100")])
        self.assertEqual([f.markets for f in _FakeFetch.instances], [["NASDAQ 100"], ["S&P500"]])


def empty_view_state():
    return empty_state()


if __name__ == "__main__":
    unittest.main()
