"""tests/test_universe_views.py — Trading Universe filters (docs/ui.md 2.6 /
8.9): the pure matcher, the table's row values and filtering, and the 신호 포착
panel's per-condition check boxes (2026-11-19, replacing the quick picks)."""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QToolButton

from ui.universe_views import count_matches, describe_ranges, empty_state, matches, normalize, same_state

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

    def test_markets_without_a_toggle_always_show(self):
        """2026-11-15: single names added from NASDAQ / NYSE have no market
        toggle, so the toggles never hide them; toggled markets still filter."""
        for m in ("NASDAQ", "NYSE"):
            self.assertTrue(matches(_row(market=m), {"market": ["KOSPI", "KOSDAQ"]}), m)
        self.assertFalse(matches(_row(market="NASDAQ 150"), {"market": ["KOSPI", "KOSDAQ"]}))
        self.assertFalse(matches(_row(market="S&P500"), {"market": ["KOSPI"]}))

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

    def test_counts(self):
        rows = [_row(values={"tper": 8.0}), _row(values={"tper": 30.0}), _row(values={"tper": -5.0}),
                _row(values={"range52w": 0.93})]
        self.assertEqual(count_matches(rows, {"ranges": {"tper": [0, 12]}}), 1)      # negative PER is not "low"
        self.assertEqual(count_matches(rows, {"ranges": {"range52w": [90, None]}}), 1)

    def test_normalize_same_state_and_descriptions(self):
        a = normalize({"market": "KOSPI", "search": "  x ", "ranges": {"tper": [None, 12], "bogus": [1, 2],
                                                                      "fper": [None, None]}})
        self.assertEqual(a, {"market": ["KOSPI"], "status": "", "search": "x", "ranges": {"tper": [None, 12.0]}})
        self.assertTrue(same_state(a, dict(a, name="mine")))
        self.assertEqual(describe_ranges({"tper": [0, 12], "range52w": [90, None]}),
                         "tPER 0–12 · 52주 위치 ≥ 90")


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
        self.assertEqual(tab.filters_btn.text(), "신호 포착 (1)")
        self.assertTrue(tab.filters_btn.isChecked())
        tab._on_ranges_applied({})
        self.assertEqual(self._visible(tab), ["A", "B", "C"])
        self.assertEqual(tab.filters_btn.text(), "신호 포착")

    def test_tper_filter(self):
        tab = self._tab([_stock("A", per=8.0), _stock("B", per=25.0), _stock("C")])
        tab._on_ranges_applied({"tper": [0, 12]})
        self.assertEqual(self._visible(tab), ["A"])
        self.assertTrue(tab.filters_btn.toolTip().startswith("tPER 0–12"))


class _FakeConditionsDialog:
    """Stands in for SignalConditionsDialog: returns `result` (None = cancelled)."""
    result = None
    seen = None

    def __init__(self, conditions, parent=None):
        _FakeConditionsDialog.seen = dict(conditions)

    def exec(self):
        return self.result is not None

    def conditions(self):
        return self.result


class TestSignalScan(_TabCase):
    """신호 포착 (docs/ui.md 8.9, 2026-11-19): the panel lists the added signals
    as check boxes; "신호 추가" opens the condition form as a pop-up dialog."""

    def _popup(self, tab):
        tab._open_range_filters()
        return tab._range_popup

    @staticmethod
    def _toggle(cb, checked):
        cb.setChecked(checked)
        cb.clicked.emit(checked)

    def _add(self, tab, result):
        _FakeConditionsDialog.result = result
        with patch("ui.universe_tab.SignalConditionsDialog", _FakeConditionsDialog):
            tab._open_signal_conditions()
        return tab._range_popup

    def test_empty_panel_and_button_name(self):
        tab = self._tab([_stock("A")])
        self.assertEqual(tab.filters_btn.text(), "신호 포착")
        popup = self._popup(tab)
        self.assertEqual(popup._checks, {})
        self.assertFalse(popup._empty.isHidden())
        self.assertFalse(hasattr(popup, "clear_btn"))                 # no "모두 지우기" (2026-11-19)
        self.assertEqual(popup.add_btn.text(), "추가")
        popup.close()

    def test_added_signals_show_as_ticked_boxes_with_counts(self):
        tab = self._tab([_stock("A", per=8.0, price=99, low=50, high=100),
                         _stock("B", per=8.0, price=60, low=50, high=100),
                         _stock("C", per=25.0, price=99, low=50, high=100)])
        popup = self._add(tab, {"tper": [None, 12], "range52w": [90, None]})
        self.assertEqual(self._visible(tab), ["A"])
        self.assertEqual(tab.filters_btn.text(), "신호 포착 (2)")
        self.assertEqual([cb.text() for cb in popup._checks.values()],
                         ["tPER ≤ 12  (2)", "52주 위치 ≥ 90  (2)"])
        self.assertTrue(all(cb.isChecked() for cb in popup._checks.values()))

        self._toggle(popup._checks["tper"], False)                  # applied at once, panel open
        self.assertEqual(self._visible(tab), ["A", "C"])
        self.assertEqual(tab._ranges_off, {"tper": [None, 12.0]})
        self.assertIn("꺼짐: tPER", tab.filters_btn.toolTip())
        self._toggle(popup._checks["tper"], True)
        self.assertEqual(self._visible(tab), ["A"])
        popup.close()

    def test_the_dialog_edits_and_keeps_unticked_signals_off(self):
        tab = self._tab([_stock("A", per=8.0)])
        tab._on_ranges_applied({"cap": [100, None]}, {"tper": [0, 12]})
        self._add(tab, {"cap": [100, None], "tper": [0, 12], "d20": [5, None]}).close()
        self.assertEqual(_FakeConditionsDialog.seen, {"cap": [100.0, None], "tper": [0.0, 12.0]})
        self.assertEqual(tab._ranges, {"cap": [100.0, None], "d20": [5.0, None]})
        self.assertEqual(tab._ranges_off, {"tper": [0.0, 12.0]})
        self._add(tab, {"tper": [0, 10]}).close()                   # edited: ticked; cap / d20 emptied: gone
        self.assertEqual((tab._ranges, tab._ranges_off), ({"tper": [0.0, 10.0]}, {}))

    def test_cancel_changes_nothing(self):
        tab = self._tab([_stock("A")])
        tab._on_ranges_applied({"cap": [100, None]})
        tab._range_popup = None
        self._add(tab, None)
        self.assertEqual(tab._ranges, {"cap": [100.0, None]})
        self.assertIsNone(tab._range_popup)

    def test_remove_signals_one_by_one(self):
        tab = self._tab([_stock("A", per=8.0)])
        tab._on_ranges_applied({"cap": [100, None]}, {"tper": [0, 12]})
        popup = self._popup(tab)
        popup._rows["cap"].findChild(QToolButton).click()
        self.assertEqual((tab._ranges, tab._ranges_off), ({}, {"tper": [0.0, 12.0]}))
        popup._rows["tper"].findChild(QToolButton).click()
        self.assertEqual((tab._ranges, tab._ranges_off), ({}, {}))
        self.assertFalse(popup._empty.isHidden())
        popup.close()

    def test_signals_are_saved_and_restored(self):
        from ui.universe_tab import UniverseTab, CUSTOM_SETTINGS_FILE
        tab = self._tab([_stock("A")])
        tab._on_ranges_applied({"cap": [100, None]}, {"tper": [0, 12]})
        settings = {"signals": tab.custom_settings["signals"]}
        with patch.object(UniverseTab, "refresh_data"),              patch("ui.universe_tab.safe_load_json",
                   side_effect=lambda path, default=None: settings if path == CUSTOM_SETTINGS_FILE else default):
            other = UniverseTab()
        self.assertEqual((other._ranges, other._ranges_off), ({"cap": [100.0, None]}, {"tper": [0.0, 12.0]}))
        self.assertEqual(other.filters_btn.text(), "신호 포착 (1)")

    def test_off_signals_survive_the_language_switch_state(self):
        tab = self._tab([_stock("A", per=8.0)])
        tab._on_ranges_applied({"cap": [100, None]}, {"tper": [0, 12]})
        state = tab.export_state()
        other = self._tab([_stock("A", per=8.0)])
        other.import_state(state)
        self.assertEqual((other._ranges, other._ranges_off), ({"cap": [100.0, None]}, {"tper": [0.0, 12.0]}))


class TestSignalConditionsDialog(unittest.TestCase):
    def test_prefill_parse_and_invalid_blocks_save(self):
        from ui.dialogs import SignalConditionsDialog
        dlg = SignalConditionsDialog({"tper": [0, 12]})
        self.assertEqual(dlg._edits["tper"][1].text(), "12")
        dlg._edits["d20"][0].setText("10")
        dlg._edits["d20"][1].setText("-5")                          # swapped into lo <= hi
        self.assertEqual(dlg.conditions(), {"tper": [0.0, 12.0], "d20": [-5.0, 10.0]})
        dlg._edits["cap"][0].setText("abc")
        dlg._on_ok()
        self.assertEqual(dlg.result(), 0)                           # not accepted
        dlg._edits["cap"][0].setText("1,000")
        dlg._on_ok()
        self.assertEqual(dlg.result(), 1)
        self.assertEqual(dlg.conditions()["cap"], [1000.0, None])


class TestMarketLists(unittest.TestCase):
    """docs/ui.md 2.1 (2026-11-14): `market` is the list of markets that are on."""

    def test_list_matches_and_index_rows_always_pass(self):
        state = {"market": ["KOSPI", "NASDAQ 150"]}
        self.assertTrue(matches(_row(market="KOSPI"), state))
        self.assertTrue(matches(_row(market="NASDAQ 150"), state))
        self.assertFalse(matches(_row(market="S&P500"), state))
        self.assertTrue(matches(_row(market="Index"), state))
        self.assertTrue(matches(_row(market="Index"), {"market": "KOSDAQ"}))   # an old single-market view

    def test_normalize_orders_and_drops_unknown_markets(self):
        self.assertEqual(normalize({"market": ["S&P500", "KOSPI", "bogus"]})["market"], ["KOSPI", "S&P500"])
        self.assertEqual(normalize({"market": []})["market"], "ALL")
        self.assertEqual(normalize({"market": "ALL"})["market"], "ALL")


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
        self.assertEqual(checked, {"KOSPI": True, "KOSDAQ": True, "NASDAQ 150": False, "S&P500": False})
        self.assertEqual(tab._market_buttons["S&P500"].text(), "S&&P 500")     # shows "S&P 500", no mnemonic
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
            tab._market_buttons["NASDAQ 150"].click()
        self.assertEqual(tab.custom_settings["markets"], ["KOSPI", "KOSDAQ", "NASDAQ 150"])
        (fetch,) = _FakeFetch.instances
        self.assertEqual((fetch.markets, fetch.include_indices, fetch.started), (["NASDAQ 150"], False, True))
        tab._on_markets_fetched([_stock("AAPL", market="NASDAQ 150"), _stock("GONE", market="NASDAQ 150"),
                                 _stock("A", market="NASDAQ 150")])          # deleted / already here: skipped
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
        """AAPL is in NASDAQ 150 and S&P500: switching NASDAQ off with S&P500
        on keeps it, under S&P500 (found by the 2026-11-14 toggle check)."""
        tab = self._tab([_stock("A")])
        with self._patched():
            tab._market_buttons["NASDAQ 150"].click()
            tab._on_markets_fetched([_stock("AAPL", market="NASDAQ 150"), _stock("NVDA", market="NASDAQ 150")])
            tab._market_buttons["S&P500"].click()
            tab._on_markets_fetched([_stock("AAPL", market="S&P500"), _stock("JPM", market="S&P500")])
            aapl = next(x for x in tab.all_data if x["ticker"] == "AAPL")
            self.assertEqual((aapl["market"], aapl["also_in"]), ("NASDAQ 150", ["S&P500"]))
            tab._market_buttons["NASDAQ 150"].click()
        self.assertEqual((aapl["market"], aapl["also_in"]), ("S&P500", ["NASDAQ 150"]))
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
            tab._market_buttons["NASDAQ 150"].click()
            tab._market_buttons["S&P500"].click()                  # first fetch still "running"
            self.assertEqual(len(_FakeFetch.instances), 1)
            tab._on_markets_fetched([_stock("NVDA", market="NASDAQ 150")])
        self.assertEqual([f.markets for f in _FakeFetch.instances], [["NASDAQ 150"], ["S&P500"]])


def empty_view_state():
    return empty_state()


if __name__ == "__main__":
    unittest.main()
