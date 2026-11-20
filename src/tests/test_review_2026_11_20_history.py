"""tests/test_review_2026_11_20_history.py — regression tests for the
2026-11-20 reviews (review_codex.md #1-#4, review_agy.md C2/H6/H17/H18/M2/M4/
M7/M12/M13) in the Trading History / KIS sync area. No network: every KIS and
Yahoo call is patched; QSettings writes go to a temporary INI file."""
import datetime as dt
import json
import math
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QSettings, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

import i18n  # noqa: E402
import trade_db  # noqa: E402
from data.collectors import kis  # noqa: E402
from tests.test_trade_db import TempDBMixin  # noqa: E402
from ui.kis_sync import plan_kis_import  # noqa: E402

app = QApplication.instance() or QApplication([])


def ex(date, order_no, ticker, side, qty, price, name=None, time="090000"):
    """A parsed execution (data.collectors.kis.parse_execution shape)."""
    return {"date": date, "time": time, "order_no": order_no, "ticker": ticker, "name": name or f"N{ticker}",
            "side": side, "qty": float(qty), "amount": float(qty) * price, "price": float(price)}


def lot(key, ticker, buy_date, qty, price, name=None, market="KOSPI"):
    return {"orig_key": key, "company": name or f"N{ticker}", "market": market, "ticker": ticker,
            "buy_date": buy_date, "buy_price": price, "qty": float(qty), "buy_amount": qty * price,
            "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0}


class _TabMixin(TempDBMixin):
    """A TradingHistoryTab on a temp DB, prices never fetched, QSettings in a temp INI."""

    def _tab(self):
        from ui.history_tab import TradingHistoryTab
        patcher = patch.object(TradingHistoryTab, "_start_price_fetch", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        tab = TradingHistoryTab()
        self.addCleanup(tab._settings_save_timer.stop)
        ini = tempfile.NamedTemporaryFile(suffix=".ini", delete=False)
        ini.close()
        self.addCleanup(os.unlink, ini.name)
        tab._settings = QSettings(ini.name, QSettings.Format.IniFormat)
        tab.load_from_db()
        return tab

    @staticmethod
    def _result(executions, deposit=1_000_000.0, holdings=None, **extra):
        return {"deposit": deposit, "holdings": holdings or [], "executions": executions,
                "markets": {e["ticker"]: "KOSPI" for e in executions}, "exec_error": "", **extra}


# ── review_codex.md #1: a skipped sell keeps the next query range open ──────
class TestSkippedSellIsQueriedAgain(_TabMixin, unittest.TestCase):

    def test_skipped_sell_comes_back_once_the_buy_is_added(self):
        tab = self._tab()
        execs = [ex("2026-11-10", "1", "000660", "sell", 1, 300.0, name="A"),
                 ex("2026-11-11", "2", "005930", "buy", 2, 100.0, name="B")]
        with patch("ui.history_tab.KisSyncDialog.exec", return_value=QDialog.DialogCode.Accepted):
            tab._on_kis_account_fetched(self._result(execs), "")
        self.assertEqual(set(trade_db.load_kis_imports()), {"2026-11-11:2"})
        self.assertEqual(trade_db.last_kis_import_date(), "2026-11-11")
        # The skipped 11-10 sell keeps the query start on its day.
        self.assertEqual(tab._kis_query_start(), dt.date(2026, 11, 10))

        # The user adds the missing buy, fetches again (KIS returns both orders).
        trade_db.upsert_trade(lot(None, "000660", "2026-11-01", 1, 100.0, name="A"))
        tab.load_from_db()
        with patch("ui.history_tab.KisSyncDialog.exec", return_value=QDialog.DialogCode.Accepted):
            tab._on_kis_account_fetched(self._result(execs), "")
        rows = trade_db.load_all_trades()
        self.assertEqual(sorted((r["ticker"], r["qty"], r["sell_date"]) for r in rows),
                         [("000660", 1.0, "2026-11-10"), ("005930", 2.0, "")])     # B not duplicated
        self.assertIsNone(trade_db.earliest_kis_pending_date())                   # pending cleared
        self.assertEqual(tab._kis_query_start(), dt.date(2026, 11, 11))

    def test_plan_lists_the_skipped_order_keys(self):
        plan = plan_kis_import([ex("2026-11-10", "7", "000660", "sell", 1, 300.0)], {}, [])
        self.assertEqual(plan.skipped, [("2026-11-10:7", "2026-11-10")])


# ── review_codex.md #2: FIFO across a later-imported older buy ──────────────
class TestFifoWithOlderImportedBuy(unittest.TestCase):

    def test_the_oldest_buy_is_closed_first(self):
        table_lot = lot("k10", "005930", "2026-11-10", 1, 200.0)
        plan = plan_kis_import([ex("2026-11-01", "1", "005930", "buy", 1, 100.0),
                                ex("2026-11-15", "2", "005930", "sell", 1, 300.0)], {}, [table_lot])
        closed = [r for r in plan.records if r["sell_date"]]
        self.assertEqual(len(closed), 1)
        c = closed[0]
        self.assertEqual((c["buy_date"], c["buy_amount"], c["sell_amount"]), ("2026-11-01", 100.0, 300.0))
        self.assertEqual(c["sell_amount"] - c["buy_amount"], 200.0)                # realized P/L
        self.assertNotIn("k10", [r["orig_key"] for r in plan.records])           # the 11-10 lot stays open
        self.assertEqual(table_lot["sell_date"], "")


# ── review_codex.md #3: an unreadable deposit keeps the field ──────────────
class TestDepositUnreadable(_TabMixin, unittest.TestCase):

    def _reply(self, output2):
        r = MagicMock()
        r.json.return_value = {"rt_cd": "0", "output1": [], "output2": output2}
        r.headers = {}
        return r

    def _balance(self, output2):
        with patch.object(kis, "_get_kis_account", return_value=("12345678", "01")), \
                patch.object(kis, "_kis_get", return_value=self._reply(output2)):
            return kis.fetch_account_balance()

    def test_collector(self):
        self.assertTrue(math.isnan(self._balance([])["deposit"]))
        self.assertTrue(math.isnan(self._balance([{"dnca_tot_amt": "abc"}])["deposit"]))
        self.assertEqual(self._balance([{"dnca_tot_amt": "0"}])["deposit"], 0.0)
        self.assertEqual(self._balance([{"dnca_tot_amt": "0", "prvs_rcdl_excc_amt": "1,500"}])["deposit"], 1500.0)

    def test_tab_keeps_the_existing_deposit(self):
        tab = self._tab()
        tab._deposit_edit.setText("500,000")
        msgs = []
        tab.status_message.connect(msgs.append)
        tab._on_kis_account_fetched(self._result([], deposit=float("nan")), "")
        self.assertEqual(tab._deposit_edit.text(), "500,000")
        self.assertIn("읽지 못해", msgs[-1])
        tab._on_kis_account_fetched(self._result([], deposit=0.0), "")
        self.assertEqual(tab._deposit_edit.text(), "0")                           # a real 0 is applied


# ── review_codex.md #4: ledger-only symbols and balance paging ──────────────
class TestHoldingsReconciliation(unittest.TestCase):

    def test_ledger_only_kr_symbol_is_reported_and_us_is_not(self):
        lots = [lot("a", "005930", "2026-10-01", 1, 100.0, name="삼성전자"),
                lot("u", "AAPL", "2026-10-01", 1, 100.0, market="US")]
        plan = plan_kis_import([], {}, lots, holdings=[])
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("삼성전자(005930)", plan.warnings[0])
        self.assertIn("KIS 잔고 0주", plan.warnings[0])

    def test_incomplete_paging_withholds_missing_symbols_only(self):
        lots = [lot("a", "005930", "2026-10-01", 1, 100.0), lot("b", "000660", "2026-10-01", 1, 100.0)]
        plan = plan_kis_import([], {}, lots, holdings=[{"ticker": "000660", "qty": 3.0}], holdings_complete=False)
        self.assertEqual(len(plan.warnings), 1)                 # 000660 differs; 005930 is withheld
        self.assertIn("000660", plan.warnings[0])

    def _page(self, tickers, cont):
        r = MagicMock()
        r.json.return_value = {"rt_cd": "0", "output1": [{"pdno": t, "prdt_name": t, "hldg_qty": "1"} for t in tickers],
                               "output2": [{"dnca_tot_amt": "10"}], "ctx_area_fk100": "F", "ctx_area_nk100": "N"}
        r.headers = {"tr_cont": cont}
        return r

    def test_balance_reads_every_page(self):
        calls = []

        def fake_get(path, tr_id, params, timeout, tr_cont=""):
            calls.append((params["CTX_AREA_FK100"], tr_cont))
            return self._page(["000001"], "M") if len(calls) == 1 else self._page(["000002"], "D")

        with patch.object(kis, "_get_kis_account", return_value=("12345678", "01")), \
                patch.object(kis, "_kis_get", side_effect=fake_get), patch.object(kis.time, "sleep"):
            bal = kis.fetch_account_balance()
        self.assertEqual([h["ticker"] for h in bal["holdings"]], ["000001", "000002"])
        self.assertTrue(bal["holdings_complete"])
        self.assertEqual(calls, [("", ""), ("F", "N")])

    def test_balance_paging_cut_off_is_flagged(self):
        with patch.object(kis, "_get_kis_account", return_value=("12345678", "01")), \
                patch.object(kis, "_kis_get", side_effect=lambda *a, **k: self._page(["000001"], "M")), \
                patch.object(kis, "_BALANCE_MAX_PAGES", 2), patch.object(kis.time, "sleep"):
            bal = kis.fetch_account_balance()
        self.assertFalse(bal["holdings_complete"])
        self.assertEqual(len(bal["holdings"]), 2)


# ── review_agy M12-1: one order on several rows ─────────────────────────────
class TestDuplicateOrderRows(unittest.TestCase):

    def test_duplicated_order_is_imported_once(self):
        e = ex("2026-11-20", "1", "005930", "buy", 10, 100.0)
        plan = plan_kis_import([e, dict(e)], {}, [])
        self.assertEqual(len(plan.records), 1)
        self.assertEqual(plan.records[0]["qty"], 10.0)
        self.assertEqual(plan.imports, [("2026-11-20:1", 10.0, 1000.0)])
        grown = dict(e, qty=15.0, amount=1500.0)
        self.assertEqual(plan_kis_import([e, grown], {}, []).records[0]["qty"], 15.0)   # largest fill wins


# ── review_agy M2: trade_db ─────────────────────────────────────────────────
class TestTradeDb(TempDBMixin, unittest.TestCase):

    def test_wal_is_set_by_init_db(self):
        conn = sqlite3.connect(trade_db._DB_FILE)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_kis_imports_reject_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            trade_db.apply_kis_import([lot(None, "005930", "2026-11-20", 1, 1.0)], [("2026-11-20:1", float("inf"), 1.0)])
        self.assertEqual(trade_db.load_all_trades(), [])
        self.assertEqual(trade_db.load_kis_imports(), {})

    def test_bad_legacy_snapshot_row_is_skipped(self):
        with open(trade_db._ASSET_JSON, "w", encoding="utf-8") as f:
            json.dump([{"date": "2026-01-02", "total": "n/a"}, {"date": "2026-01-09", "total": 100.0},
                       {"date": "2026-01-16", "total": "inf"}], f)
        conn = trade_db._connect()
        try:
            conn.execute("DELETE FROM migrations")
            conn.commit()
            trade_db._migrate_asset_records_json(conn)
        finally:
            conn.close()
        self.assertEqual(trade_db.load_asset_records(), [{"date": "2026-01-09", "total": 100.0, "manual": True}])


# ── review_agy M4: KIS collector ────────────────────────────────────────────
class TestKisCollector(unittest.TestCase):

    def setUp(self):
        self._keys = dict(kis._KIS_KEYS_CACHE)
        self._token = dict(kis._KIS_TOKEN_CACHE)
        kis._KIS_KEYS_CACHE.clear()

    def tearDown(self):
        kis._KIS_KEYS_CACHE.clear()
        kis._KIS_KEYS_CACHE.update(self._keys)
        kis._KIS_TOKEN_CACHE.update(self._token)

    def test_keys_are_read_once_and_cached_as_a_pair(self):
        with patch.object(kis, "_read_kis_keys", return_value=("k", "s")) as read:
            self.assertEqual(kis._get_kis_keys(), ("k", "s"))
            self.assertEqual(kis._get_kis_keys(), ("k", "s"))
        read.assert_called_once()

    def _resp(self, status, body):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = body
        return r

    def test_refused_token_is_dropped_and_the_call_retried_once(self):
        kis._KIS_TOKEN_CACHE.update(token="old", expires=1e18)
        tokens = iter(["old", "new"])
        replies = [self._resp(500, {"rt_cd": "1", "msg_cd": "EGW00123"}), self._resp(200, {"rt_cd": "0"})]
        with patch.object(kis, "_kis_headers", side_effect=lambda tr_id: {"authorization": f"Bearer {next(tokens)}"}), \
                patch.object(kis._KIS_SESSION, "get", side_effect=replies) as get, \
                patch.object(kis, "_load_kis_token_cache", return_value={"token": "old"}), \
                patch.object(kis, "_save_kis_token_cache") as save:
            r = kis._kis_get("/x", "TR", {}, timeout=1)
        self.assertEqual(r.json(), {"rt_cd": "0"})
        self.assertEqual(get.call_count, 2)
        self.assertIsNone(kis._KIS_TOKEN_CACHE["token"])
        save.assert_called_once_with({})

    def test_401_is_retried_only_once(self):
        with patch.object(kis, "_kis_headers", return_value={"authorization": "Bearer t"}), \
                patch.object(kis._KIS_SESSION, "get", return_value=self._resp(401, {})) as get, \
                patch.object(kis, "_load_kis_token_cache", return_value={}), \
                patch.object(kis, "_save_kis_token_cache"):
            r = kis._kis_get("/x", "TR", {}, timeout=1)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(get.call_count, 2)

    def test_binary_ws_frame_is_skipped(self):
        import websocket
        frames = iter([b"\x00\x01", "0|H0STCNT0|001|005930^093000^70100"])
        ws = MagicMock()
        ws.recv.side_effect = lambda: next(frames)
        prices = {}
        kis._kis_ws_collect_ticks(ws, websocket, "key", ["005930"], 1.0, prices)
        self.assertEqual(prices, {"005930": 70100.0})


# ── review_agy M7: realtime thread and reload ───────────────────────────────
class TestRealtime(_TabMixin, unittest.TestCase):

    def test_kr_failure_still_fetches_us(self):
        from threads.realtime import RealtimePriceThread
        t = RealtimePriceThread(["005930"], ["AAPL"])
        failures, fetched = [], []
        t.failed.connect(failures.append)
        t.prices_fetched.connect(lambda p, complete: fetched.append((p, complete)))
        with patch("data_fetcher.fetch_kis_realtime_prices", side_effect=OSError("KIS down")), \
                patch("data_fetcher.fetch_us_realtime_prices", return_value={"AAPL": 2.0}), \
                patch("data_fetcher.get_usd_krw_rate", return_value=1400.0):
            t.run()
        self.assertEqual(failures, ["KIS down"])
        self.assertEqual(fetched, [({"AAPL": 2800.0}, False)])

    def test_reload_retires_the_running_realtime_thread(self):
        tab = self._tab()
        running = MagicMock()
        running.isRunning.return_value = True
        tab._rt_price_thread = running
        tab.load_from_db()
        self.assertIsNone(tab._rt_price_thread)
        running.blockSignals.assert_called_with(True)


class _Emitter(QObject):
    prices = pyqtSignal(dict, bool)
    kis = pyqtSignal(object, str)


# ── review_agy H6: replies from retired threads ─────────────────────────────
class TestStaleSignals(_TabMixin, unittest.TestCase):

    def test_reply_from_a_thread_that_is_not_current_is_dropped(self):
        tab = self._tab()
        rec = lot("k", "005930", "2026-10-01", 1, 100.0)
        tab._open_data = [rec]
        # An exception inside a signal-invoked slot aborts PyQt6; the redraw is not under test.
        tab._refresh_summary = tab._apply_filter = lambda *a: None
        tab._rt_price_thread = object()                 # the current one is someone else
        stale = _Emitter()
        stale.prices.connect(tab._on_realtime_prices_fetched)
        stale.prices.emit({"005930": 999.0}, True)
        self.assertNotIn("curr_price", rec)
        tab._rt_price_thread = stale                    # now it is the current thread
        stale.prices.emit({"005930": 999.0}, True)
        self.assertEqual(rec["curr_price"], 999.0)

    def test_stale_kis_reply_is_dropped(self):
        tab = self._tab()
        tab._deposit_edit.setText("7")
        tab._deposit_thread = object()
        stale = _Emitter()
        stale.kis.connect(tab._on_kis_account_fetched)
        stale.kis.emit(self._result([], deposit=5.0), "")
        self.assertEqual(tab._deposit_edit.text(), "7")


# ── review_agy H17: debounced settings flushed ──────────────────────────────
class TestSettingsFlush(_TabMixin, unittest.TestCase):

    def test_export_state_writes_the_pending_edit(self):
        tab = self._tab()
        tab._principal_edit.setText("1,234")
        tab._on_deposit_changed()
        self.assertTrue(tab._settings_save_timer.isActive())
        tab.export_state()
        self.assertFalse(tab._settings_save_timer.isActive())
        self.assertEqual(tab._settings.value("trading_history/principal"), "1,234")

    def test_nothing_written_without_a_pending_edit(self):
        tab = self._tab()
        tab.flush_pending_settings()
        self.assertIsNone(tab._settings.value("trading_history/principal"))


# ── review_agy H18 / M12-6: trade edit dialogs ──────────────────────────────
class TestTradeDialogs(unittest.TestCase):

    def test_sell_price_alone_is_rejected(self):
        from ui.dialogs.trade_edit import SellEditDialog
        dlg = SellEditDialog({"buy_date": "2026-01-05", "qty": 10.0})
        dlg.sell_price_edit.setText("75,000")
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)
        self.assertTrue(dlg.sell_date_edit.toolTip())             # the date field is flagged

    def test_clearing_every_sell_field_reopens(self):
        from ui.dialogs.trade_edit import SellEditDialog
        dlg = SellEditDialog({"buy_date": "2026-01-05", "qty": 10.0, "sell_date": "2026-02-05",
                              "sell_price": 1.0, "sell_qty": 10.0, "sell_amount": 10.0})
        for e in (dlg.sell_date_edit, dlg.sell_price_edit, dlg.sell_qty_edit, dlg.sell_amount_edit):
            e.setText("")
        dlg.on_save()
        self.assertEqual(dlg.result_data, {"sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0})

    def test_non_finite_amounts_are_rejected(self):
        from ui.dialogs.trade_edit import BuyEditDialog, SellEditDialog
        buy = BuyEditDialog({"buy_date": "2026-01-05", "buy_price": 1.0, "qty": 1.0})
        buy.buy_amount_edit.setText("inf")
        sell = SellEditDialog({"buy_date": "2026-01-05", "qty": 1.0, "sell_date": "2026-02-05",
                               "sell_price": 1.0, "sell_qty": 1.0})
        sell.sell_amount_edit.setText("nan")
        for dlg in (buy, sell):
            with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
                dlg.on_save()
            warn.assert_called_once()
            self.assertIsNone(dlg.result_data)


# ── review_agy C2: TradeEntryDialog while the ticker check runs ─────────────
class _Owner(QDialog):
    def __init__(self):
        super().__init__()
        self.tracked = []

    def _track_thread(self, thread, attr=None):
        self.tracked.append(thread)
        return thread


class TestTradeEntryValidation(unittest.TestCase):

    def test_escape_is_ignored_and_the_owner_keeps_the_thread(self):
        from ui.dialogs.trade_edit import TradeEntryDialog
        owner = _Owner()
        dlg = TradeEntryDialog(owner)
        dlg.ticker_edit.setText("005930")
        dlg.buy_price_edit.setText("100")
        dlg.qty_edit.setText("1")
        thread = MagicMock()
        with patch("ui.dialogs.trade_edit.TickerValidateThread", return_value=thread):
            dlg.on_save()
        self.assertEqual(owner.tracked, [thread])
        dlg.setResult(QDialog.DialogCode.Accepted)            # a real reject() would set Rejected
        dlg.reject()                                          # Esc while pending
        self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
        self.assertIsNotNone(dlg._pending)
        QApplication.restoreOverrideCursor()                  # the test never delivers the reply


# ── review_agy M12-4 / M12-7: history tab numbers ───────────────────────────
class TestHistoryNumbers(_TabMixin, unittest.TestCase):

    def test_unpriced_open_lot_adds_no_loss_to_the_total(self):
        tab = self._tab()
        tab._open_data = [dict(lot("a", "005930", "2026-10-01", 2, 100.0), curr_price=0.0),
                          dict(lot("b", "005930", "2026-10-02", 1, 100.0), curr_price=150.0)]
        _m, total_pl, total_buy, total_sell = tab._stock_history_rows("N005930", True)
        self.assertEqual((total_pl, total_buy, total_sell), (50.0, 300.0, 150.0))

    def test_cutting_a_closed_lots_sell_qty_splits_it(self):
        rec = dict(lot(None, "005930", "2026-10-01", 10, 100.0), sell_date="2026-11-01",
                   sell_price=120.0, sell_qty=10.0, sell_amount=1200.0)
        trade_db.upsert_trade(rec)
        tab = self._tab()
        closed = tab._closed_data[0]
        dlg = MagicMock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.result_data = {"sell_date": "2026-11-01", "sell_price": 120.0, "sell_qty": 4.0, "sell_amount": 480.0}
        with patch("ui.history_tab.SellEditDialog", return_value=dlg):
            tab._edit_sell_fields("closed", closed)
        rows = sorted((r["qty"], r["sell_qty"], r["buy_amount"]) for r in trade_db.load_all_trades())
        self.assertEqual(rows, [(4.0, 4.0, 400.0), (6.0, 0.0, 600.0)])
        self.assertEqual([r["qty"] for r in tab._open_data], [6.0])


# ── review_agy M13: labels ──────────────────────────────────────────────────
class TestLabels(unittest.TestCase):

    def test_loading_item_in_english(self):
        from ui.history_table import loading_item
        prev = i18n.language()
        i18n.set_language("en")
        try:
            self.assertEqual(loading_item().text(), "Loading")
        finally:
            i18n.set_language(prev)

    def test_monthly_summary_says_pl_not_realized(self):
        from ui.history_table import _monthly_summary_label
        html = _monthly_summary_label({"pl": 1.0, "buy_date": "2026-11", "trade_count": 1}).text()
        self.assertIn("손익", html)
        self.assertNotIn("실현 손익", html)


if __name__ == "__main__":
    unittest.main()
