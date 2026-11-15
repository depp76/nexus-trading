"""tests/test_history_prices.py — Trading History price results and edits
(review_codex.md C10-C12).

C10: a position price fetch's results are applied to the records it was asked
for, never to whatever sits at the same list position when they land.
C11: company/ticker/buy edits reach the record only after the DB write
succeeded. C12: a realtime answer that is only partly there keeps the
failure indicator on.
"""
import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication, QDialog

app = QApplication.instance() or QApplication([])


def _rec(key, company, ticker, **extra):
    r = {"orig_key": key, "company": company, "ticker": ticker, "market": "KOSPI",
         "buy_date": "2026-01-01", "buy_price": 100.0, "qty": 1.0, "buy_amount": 100.0,
         "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0,
         "curr_price": 0.0}
    r.update(extra)
    return r


def _res(index, company, ticker, price):
    return {"index": index, "ticker": ticker, "market": "KOSPI", "name": company,
            "curr_price": price, "curr_pl": 0.0, "curr_pl_pct": 0.0,
            "wk1": 1.0, "wk2": 0.0, "mth1": 0.0}


class _TabCase(unittest.TestCase):
    def setUp(self):
        from ui.history_tab import TradingHistoryTab
        self.tab = TradingHistoryTab()
        self.addCleanup(self.tab._settings_save_timer.stop)
        for name in ("_refresh_summary", "_apply_filter"):
            p = patch.object(self.tab, name)
            p.start()
            self.addCleanup(p.stop)

    def start_fetch(self):
        """Run _start_price_fetch with the thread class stubbed; returns the
        request id the thread was given."""
        with patch("ui.history_tab.PositionPriceFetchThread") as cls, \
             patch.object(self.tab, "_track_thread", side_effect=lambda t, attr=None: t):
            self.tab._start_price_fetch()
        return cls.call_args.kwargs["request_id"]


class TestPriceResultsFollowTheRecord(_TabCase):
    """C10."""

    def test_delete_during_fetch_does_not_shift_results(self):
        a, b = _rec("A", "A", "AAA"), _rec("B", "B", "BBB")
        self.tab._open_data = [a, b]
        req = self.start_fetch()
        self.tab._open_data = [b]                        # A deleted while the fetch ran
        self.tab._on_prices_ready([_res(0, "A", "AAA", 100.0), _res(1, "B", "BBB", 200.0)], req)
        self.assertEqual((b["company"], b["ticker"], b["curr_price"]), ("B", "BBB", 200.0))
        self.assertEqual(self.tab._open_data, [b])

    def test_record_sold_during_fetch_gets_its_own_price_as_closed(self):
        a, b = _rec("A", "A", "AAA"), _rec("B", "B", "BBB")
        self.tab._open_data = [a, b]
        req = self.start_fetch()
        a.update(sell_date="2026-02-01", sell_price=80.0)   # sell confirmed: open -> closed
        self.tab._open_data = [b]
        self.tab._closed_data = [a]
        self.tab._on_prices_ready([_res(0, "A", "AAA", 100.0), _res(1, "B", "BBB", 200.0)], req)
        self.assertEqual(a["curr_price"], 100.0)
        self.assertAlmostEqual(a["curr_pl_pct"], 25.0)       # vs the sell price
        self.assertEqual(b["curr_price"], 200.0)

    def test_closed_then_open_indexing(self):
        c, o = _rec("C", "C", "CCC", sell_date="2026-02-01", sell_price=50.0), _rec("O", "O", "OOO")
        self.tab._closed_data = [c]
        self.tab._open_data = [o]
        req = self.start_fetch()
        self.tab._on_prices_ready([_res(0, "C", "CCC", 60.0), _res(1, "O", "OOO", 70.0)], req)
        self.assertEqual((c["curr_price"], o["curr_price"]), (60.0, 70.0))
        self.assertAlmostEqual(c["curr_pl_pct"], 20.0)
        self.assertEqual(o["wk1"], 1.0)

    def test_older_run_is_dropped(self):
        a = _rec("A", "A", "AAA")
        self.tab._open_data = [a]
        old = self.start_fetch()
        new = self.start_fetch()
        self.tab._on_prices_ready([_res(0, "A", "AAA", 1.0)], old)
        self.assertEqual(a["curr_price"], 0.0)
        self.tab._on_prices_ready([_res(0, "A", "AAA", 2.0)], new)
        self.assertEqual(a["curr_price"], 2.0)

    def test_record_edited_since_the_request_is_skipped(self):
        a, b = _rec("A", "A", "AAA"), _rec("B", "B", "BBB")
        self.tab._open_data = [a, b]
        req = self.start_fetch()
        a["company"] = "A renamed"
        b["ticker"] = "BBX"
        self.tab._on_prices_ready([_res(0, "A", "AAA", 1.0), _res(1, "B", "BBB", 2.0)], req)
        self.assertEqual((a["company"], a["ticker"], a["curr_price"]), ("A renamed", "AAA", 0.0))
        self.assertEqual((b["company"], b["ticker"], b["curr_price"]), ("B", "BBX", 0.0))

    def test_reloaded_lists_are_not_touched(self):
        self.tab._open_data = [_rec("A", "A", "AAA")]
        req = self.start_fetch()
        fresh = _rec("A", "A", "AAA")                     # same trade, new dict (reload)
        self.tab._open_data = [fresh]
        self.tab._on_prices_ready([_res(0, "A", "AAA", 9.0)], req)
        self.assertEqual(fresh["curr_price"], 0.0)

    def test_thread_echoes_request_id(self):
        from threads.fetch_threads import PositionPriceFetchThread
        t = PositionPriceFetchThread([""], [""], [""], [0], [0], [0], [True], [True], request_id=7)
        got = []
        t.prices_ready.connect(lambda res, rid: got.append((len(res), rid)))
        with patch("data_fetcher._get_listing_with_norm", side_effect=RuntimeError("offline")), \
             patch("data_fetcher.fetch_naver_realtime_prices", return_value={}):
            t.run()
        self.assertEqual(got, [(1, 7)])


class TestEditsApplyOnlyWhenSaved(_TabCase):
    """C11."""

    def setUp(self):
        super().setUp()
        self.rec = _rec("K", "Before", "005930")
        self.tab._open_data = [self.rec]
        self.tab._row_data = [("open", self.rec)]

    def test_company_edit(self):
        with patch("ui.history_tab.QInputDialog.getText", return_value=("After", True)), \
             patch.object(self.tab, "_save_overrides", return_value=False):
            self.tab._on_cell_double_clicked(0, 0)
        self.assertEqual(self.rec["company"], "Before")
        self.assertNotIn("is_overridden", self.rec)
        with patch("ui.history_tab.QInputDialog.getText", return_value=("After", True)), \
             patch.object(self.tab, "_save_overrides", return_value=True):
            self.tab._on_cell_double_clicked(0, 0)
        self.assertEqual((self.rec["company"], self.rec["is_overridden"]), ("After", True))

    def test_ticker_edit_failure_starts_no_lookup(self):
        with patch("ui.history_tab.QInputDialog.getText", return_value=("000660", True)), \
             patch.object(self.tab, "_save_overrides", return_value=False), \
             patch.object(self.tab, "_start_price_fetch") as fetch, \
             patch("ui.history_tab.SingleStockFetchThread") as lookup:
            self.tab._on_cell_double_clicked(0, 2)
        self.assertEqual(self.rec["ticker"], "005930")
        fetch.assert_not_called()
        lookup.assert_not_called()
        with patch("ui.history_tab.QInputDialog.getText", return_value=("000660", True)), \
             patch.object(self.tab, "_save_overrides", return_value=True), \
             patch.object(self.tab, "_start_price_fetch") as fetch, \
             patch.object(self.tab, "_track_thread", side_effect=lambda t, attr=None: t), \
             patch("ui.history_tab.SingleStockFetchThread") as lookup:
            self.tab._on_cell_double_clicked(0, 2)
        self.assertEqual(self.rec["ticker"], "000660")
        fetch.assert_called_once()
        lookup.assert_called_once_with("KOSPI", "000660")

    def test_buy_edit(self):
        col = min(self.tab._BUY_EDIT_COLS - {0, 2})
        dlg = MagicMock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.result_data = {"buy_date": "2026-03-03", "buy_price": 200.0, "qty": 5.0, "buy_amount": 1000.0}
        before = dict(self.rec)
        with patch("ui.history_tab.BuyEditDialog", return_value=dlg), \
             patch.object(self.tab, "_save_overrides", return_value=False):
            self.tab._on_cell_double_clicked(0, col)
        self.assertEqual(self.rec, before)
        self.tab._refresh_summary.assert_not_called()
        with patch("ui.history_tab.BuyEditDialog", return_value=dlg), \
             patch.object(self.tab, "_save_overrides", return_value=True) as save:
            self.tab._on_cell_double_clicked(0, col)
        self.assertEqual((self.rec["buy_price"], self.rec["qty"], self.rec["buy_amount"]), (200.0, 5.0, 1000.0))
        saved = save.call_args.args[0][0]
        self.assertIsNot(saved, self.rec)                   # a copy is saved, then applied
        self.assertEqual(saved["buy_amount"], 1000.0)

    def test_save_writes_the_generated_key_back(self):
        """A record without a key gets one from upsert_trades on the copy;
        the update carries it over to the record."""
        self.rec["orig_key"] = None

        def upsert(records):
            for r in records:
                r["orig_key"] = r.get("orig_key") or "new_key"
        with patch("ui.history_tab.QInputDialog.getText", return_value=("After", True)), \
             patch("ui.history_tab.trade_db.upsert_trades", side_effect=upsert):
            self.tab._on_cell_double_clicked(0, 0)
        self.assertEqual(self.rec["orig_key"], "new_key")

    def test_resolved_name(self):
        self.rec["ticker"] = "000660"
        with patch.object(self.tab, "_save_overrides", return_value=False), \
             patch.object(self.tab, "_start_price_fetch") as fetch:
            self.tab._on_ticker_name_resolved({"name": "SK hynix"}, "", "000660")
        self.assertEqual(self.rec["company"], "Before")
        fetch.assert_not_called()
        with patch.object(self.tab, "_save_overrides", return_value=True), \
             patch.object(self.tab, "_start_price_fetch") as fetch:
            self.tab._on_ticker_name_resolved({"name": "SK hynix"}, "", "000660")
        self.assertEqual(self.rec["company"], "SK hynix")
        fetch.assert_called_once()                        # re-asked under the new name


class TestRealtimePartialFailure(unittest.TestCase):
    """C12."""

    def _run(self, kr, us):
        from threads.realtime import RealtimePriceThread
        t = RealtimePriceThread(["005930"], ["AAPL"])
        failures, fetched = [], []
        t.failed.connect(failures.append)
        t.prices_fetched.connect(lambda p, complete: fetched.append((p, complete)))
        with patch("data_fetcher.fetch_kis_realtime_prices", **kr), \
             patch("data_fetcher.fetch_us_realtime_prices", **us), \
             patch("data_fetcher.get_usd_krw_rate", return_value=1400.0):
            t.run()
        return failures, fetched

    def test_kr_ok_us_raises(self):
        failures, fetched = self._run({"return_value": {"005930": 100.0}},
                                      {"side_effect": RuntimeError("US unavailable")})
        self.assertEqual(failures, ["US unavailable"])
        self.assertEqual(fetched, [({"005930": 100.0}, False)])

    def test_kr_ok_us_empty(self):
        failures, fetched = self._run({"return_value": {"005930": 100.0}}, {"return_value": {}})
        self.assertEqual(failures, ["no US quotes received (Yahoo)"])
        self.assertEqual(fetched, [({"005930": 100.0}, False)])

    def test_kr_empty_us_ok(self):
        failures, fetched = self._run({"return_value": {}}, {"return_value": {"AAPL": 2.0}})
        self.assertEqual(failures, ["no KR quotes received (KIS)"])
        self.assertEqual(fetched, [({"AAPL": 2800.0}, False)])

    def test_both_ok_and_a_missing_name_is_not_a_failure(self):
        from threads.realtime import RealtimePriceThread
        t = RealtimePriceThread(["005930", "000660"], ["AAPL"])
        failures, fetched = [], []
        t.failed.connect(failures.append)
        t.prices_fetched.connect(lambda p, complete: fetched.append(complete))
        with patch("data_fetcher.fetch_kis_realtime_prices", return_value={"005930": 1.0}), \
             patch("data_fetcher.fetch_us_realtime_prices", return_value={"AAPL": 1.0}), \
             patch("data_fetcher.get_usd_krw_rate", return_value=1400.0):
            t.run()
        self.assertEqual((failures, fetched), ([], [True]))

    def test_partial_answer_is_applied_but_does_not_clear_the_failure(self):
        from ui.history_tab import TradingHistoryTab
        tab = TradingHistoryTab()
        self.addCleanup(tab._settings_save_timer.stop)
        rec = _rec("K", "Samsung", "005930")
        tab._open_data = [rec]
        oks = []
        tab.price_refresh_ok.connect(lambda: oks.append(1))
        with patch.object(tab, "_refresh_summary"), patch.object(tab, "_apply_filter"):
            tab._on_realtime_prices_fetched({"005930": 123.0}, False)
            self.assertEqual((rec["curr_price"], oks), (123.0, []))
            tab._on_realtime_prices_fetched({"005930": 124.0}, True)   # next full answer clears it
        self.assertEqual((rec["curr_price"], oks), (124.0, [1]))


if __name__ == "__main__":
    unittest.main()
