"""tests/test_kis_sync.py — Trading History's KIS 조회 (2026-11-18, docs/ui.md 8.28):
KIS executed orders -> Trading History rows (ui/kis_sync.py), the execution
query's parsing and TR split (data/collectors/kis.py), the one-transaction save
(trade_db.apply_kis_import) and the tab flow with the preview dialog.
No network: the KIS calls are patched."""
import datetime as dt
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QDialog

import trade_db
from data.collectors import kis
from tests.test_trade_db import TempDBMixin
from ui.kis_sync import order_key, plan_kis_import

app = QApplication.instance() or QApplication([])


def ex(date, order_no, ticker, side, qty, price, name=None, time="090000"):
    """A parsed execution (data.collectors.kis.parse_execution shape)."""
    return {"date": date, "time": time, "order_no": order_no, "ticker": ticker, "name": name or f"N{ticker}",
            "side": side, "qty": float(qty), "amount": float(qty) * price, "price": float(price)}


def lot(key, ticker, buy_date, qty, price, name=None):
    return {"orig_key": key, "company": name or f"N{ticker}", "market": "KOSPI", "ticker": ticker,
            "buy_date": buy_date, "buy_price": price, "qty": float(qty), "buy_amount": qty * price,
            "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0}


class TestPlan(unittest.TestCase):

    def test_same_day_buys_at_one_price_share_a_row_other_prices_get_their_own(self):
        # User direction 2026-11-24: every trade on its own, never averaged.
        plan = plan_kis_import([ex("2026-11-20", "1", "005930", "buy", 10, 100.0),
                                ex("2026-11-20", "2", "005930", "buy", 30, 104.0),
                                ex("2026-11-20", "3", "005930", "buy", 5, 100.0)], {}, [], {"005930": "KOSPI"})
        rows = sorted(plan.records, key=lambda r: r["buy_price"])
        self.assertEqual([(r["buy_price"], r["qty"], r["buy_amount"]) for r in rows],
                         [(100.0, 15.0, 1500.0), (104.0, 30.0, 3120.0)])
        r = rows[0]
        self.assertIsNone(r["orig_key"])
        self.assertEqual((r["ticker"], r["market"], r["buy_date"], r["sell_date"]), ("005930", "KOSPI", "2026-11-20", ""))
        self.assertEqual(sorted(k for k, _q, _a in plan.imports), ["2026-11-20:1", "2026-11-20:2", "2026-11-20:3"])
        self.assertEqual([(row.side, row.qty) for row in plan.rows], [("buy", 15.0), ("buy", 30.0)])

    def test_sell_closes_the_oldest_lots_first_and_splits_the_last(self):
        lots = [lot("b", "000660", "2026-10-02", 5, 200.0), lot("a", "000660", "2026-10-01", 10, 100.0)]
        plan = plan_kis_import([ex("2026-11-20", "9", "000660", "sell", 12, 300.0)], {}, lots)
        by_key = {r["orig_key"]: r for r in plan.records}
        a, b = by_key["a"], by_key["b"]
        self.assertEqual((a["qty"], a["sell_qty"], a["sell_date"], a["sell_amount"]), (10.0, 10.0, "2026-11-20", 3000.0))
        self.assertEqual((b["qty"], b["sell_qty"], b["buy_amount"]), (2.0, 2.0, 400.0))     # split: 2 of 5 sold
        rest = by_key[None]
        self.assertEqual((rest["qty"], rest["buy_amount"], rest["sell_date"], rest["buy_date"]),
                         (3.0, 600.0, "", "2026-10-02"))
        self.assertEqual(lots[0]["qty"], 5)                                # inputs untouched
        self.assertEqual(plan.rows[0].note, "청산: 2026-10-01 매수분 10주, 2026-10-02 매수분 2주")

    def test_a_sell_larger_than_the_holding_is_skipped_and_not_marked(self):
        plan = plan_kis_import([ex("2026-11-20", "9", "000660", "sell", 12, 300.0)], {},
                               [lot("a", "000660", "2026-10-01", 10, 100.0)])
        self.assertEqual(plan.records, [])
        self.assertEqual(plan.imports, [])                     # comes back on the next run
        self.assertFalse(plan.rows[0].applied)
        self.assertFalse(plan.applicable)
        self.assertTrue(any("건너뛰었습니다" in w for w in plan.warnings))

    def test_same_day_buy_then_sell_and_a_sell_never_closes_a_later_buy(self):
        plan = plan_kis_import([ex("2026-11-20", "2", "035720", "sell", 5, 60.0, time="100000"),
                                ex("2026-11-20", "1", "035720", "buy", 5, 50.0, time="090000")], {}, [])
        self.assertEqual(len(plan.records), 1)
        r = plan.records[0]
        self.assertEqual((r["buy_date"], r["sell_date"], r["qty"], r["sell_amount"]), ("2026-11-20", "2026-11-20", 5.0, 300.0))
        early = plan_kis_import([ex("2026-11-19", "2", "035720", "sell", 5, 60.0),
                                 ex("2026-11-20", "1", "035720", "buy", 5, 50.0)], {}, [])
        self.assertFalse(early.rows[0].applied)                        # nothing held on the 19th

    def test_rerun_and_later_fills_bring_only_the_difference(self):
        first = [ex("2026-11-20", "1", "005930", "buy", 10, 100.0)]
        plan = plan_kis_import(first, {}, [])
        imported = {k: (q, a) for k, q, a in plan.imports}
        self.assertEqual(plan_kis_import(first, imported, plan.records).rows, [])     # nothing new
        grown = [dict(first[0], qty=15.0, amount=10 * 100.0 + 5 * 110.0, price=103.33)]
        delta = plan_kis_import(grown, imported, plan.records)
        self.assertEqual(len(delta.records), 1)
        self.assertEqual((delta.records[0]["qty"], delta.records[0]["buy_amount"]), (5.0, 550.0))
        self.assertEqual(delta.imports, [(order_key(grown[0]), 15.0, 1550.0)])

    def test_costs_go_into_the_amounts_like_the_kiwoom_records(self):
        # Kiwoom 2026-02-09 Samsung sells (verified against the statement on
        # 2026-11-24): 171 @167,600 and 269 @167,700 close five lots first in,
        # first out; one fee (10,690) and tax (147,539) for the day.
        lots = [lot("a", "005930", "2025-10-13", 55, 91600.0), lot("b", "005930", "2025-11-11", 50, 104600.0),
                lot("c", "005930", "2025-12-09", 85, 108700.0), lot("d", "005930", "2025-12-30", 250, 118900.0)]
        execs = [ex("2026-02-09", "1", "005930", "sell", 171, 167600.0),
                 ex("2026-02-09", "2", "005930", "sell", 269, 167700.0)]
        costs = [{"date": "2026-02-09", "ticker": "005930", "buy_amount": 0.0,
                  "sell_amount": 171 * 167600.0 + 269 * 167700.0, "fee": 10690.0, "tax": 147539.0}]
        plan = plan_kis_import(execs, {}, lots, costs=costs)
        closed = [r for r in plan.records if r["sell_date"]]
        self.assertAlmostEqual(sum(r["sell_amount"] for r in closed), 73_612_671.0, places=4)
        self.assertEqual(sorted((r["buy_date"], r["sell_price"], r["sell_qty"]) for r in closed),
                         [("2025-10-13", 167600.0, 55.0), ("2025-11-11", 167600.0, 50.0),
                          ("2025-12-09", 167600.0, 66.0), ("2025-12-09", 167700.0, 19.0),
                          ("2025-12-30", 167700.0, 250.0)])
        self.assertEqual([(row.price, row.applied) for row in plan.rows], [(167600.0, True), (167700.0, True)])

    def test_a_buy_gets_its_fee_added_and_a_shared_row_splits_by_amount(self):
        execs = [ex("2026-11-20", "1", "005930", "buy", 10, 1000.0), ex("2026-11-20", "2", "005930", "sell", 10, 1000.0)]
        costs = [{"date": "2026-11-20", "ticker": "005930", "buy_amount": 10000.0, "sell_amount": 10000.0,
                  "fee": 4.0, "tax": 20.0}]
        r = plan_kis_import(execs, {}, [], costs=costs).records[0]
        self.assertEqual((r["buy_amount"], r["sell_amount"]), (10002.0, 10000.0 - 2.0 - 20.0))
        self.assertEqual((r["buy_price"], r["sell_price"]), (1000.0, 1000.0))

    def test_fills_without_costs_yet_are_held_with_the_later_fills_of_that_stock(self):
        execs = [ex("2026-11-20", "1", "005930", "buy", 10, 100.0), ex("2026-11-21", "2", "005930", "sell", 10, 110.0),
                 ex("2026-11-20", "3", "000660", "buy", 1, 500.0)]
        costs = [{"date": "2026-11-21", "ticker": "005930", "buy_amount": 0.0, "sell_amount": 1100.0, "fee": 1.0, "tax": 2.0},
                 {"date": "2026-11-20", "ticker": "000660", "buy_amount": 500.0, "sell_amount": 0.0, "fee": 1.0, "tax": 0.0}]
        plan = plan_kis_import(execs, {}, [], costs=costs)
        self.assertEqual([(r["ticker"], r["buy_amount"]) for r in plan.records], [("000660", 501.0)])
        self.assertEqual([k for k, _q, _a in plan.imports], ["2026-11-20:3"])
        self.assertEqual(sorted(k for k, _d in plan.skipped), ["2026-11-20:1", "2026-11-21:2"])
        held = [row for row in plan.rows if not row.applied]
        self.assertEqual(len(held), 2)
        self.assertTrue(all(row.note.startswith("보류") for row in held))

    def test_costs_reported_for_less_than_the_fills_are_held(self):
        execs = [ex("2026-11-20", "1", "005930", "buy", 10, 100.0)]
        partial = [{"date": "2026-11-20", "ticker": "005930", "buy_amount": 600.0, "sell_amount": 0.0, "fee": 1.0, "tax": 0.0}]
        self.assertFalse(plan_kis_import(execs, {}, [], costs=partial).applicable)
        full = [dict(partial[0], buy_amount=1000.0)]
        self.assertEqual(plan_kis_import(execs, {}, [], costs=full).records[0]["buy_amount"], 1001.0)

    def test_holdings_mismatch_is_reported(self):
        plan = plan_kis_import([ex("2026-11-20", "1", "005930", "buy", 10, 100.0)], {}, [],
                               holdings=[{"ticker": "005930", "name": "삼성전자", "qty": 12.0}])
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("KIS 잔고 12주", plan.warnings[0])
        ok = plan_kis_import([ex("2026-11-20", "1", "005930", "buy", 10, 100.0)], {}, [],
                             holdings=[{"ticker": "005930", "name": "삼성전자", "qty": 10.0}])
        self.assertEqual(ok.warnings, [])


class TestKisCollector(unittest.TestCase):

    def test_parse_execution(self):
        row = {"ord_dt": "20261120", "ord_tmd": "093015", "odno": "0000123", "pdno": "005930", "prdt_name": "삼성전자",
               "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "10", "tot_ccld_amt": "1000000", "avg_prvs": "100000"}
        e = kis.parse_execution(row)
        self.assertEqual((e["date"], e["side"], e["qty"], e["amount"], e["price"], e["order_no"]),
                         ("2026-11-20", "buy", 10.0, 1_000_000.0, 100_000.0, "0000123"))
        self.assertEqual(kis.parse_execution(dict(row, sll_buy_dvsn_cd="01"))["side"], "sell")
        self.assertIsNone(kis.parse_execution(dict(row, tot_ccld_qty="0")))       # unfilled / cancelled
        self.assertIsNone(kis.parse_execution(dict(row, sll_buy_dvsn_cd="")))

    def test_parse_trade_cost(self):
        row = {"trad_dt": "20261120", "pdno": "005930", "buy_amt": "0", "sll_amt": "1,000,000",
               "fee": "145", "tl_tax": "2000"}
        self.assertEqual(kis.parse_trade_cost(row),
                         {"date": "2026-11-20", "ticker": "005930", "buy_amount": 0.0, "sell_amount": 1_000_000.0,
                          "fee": 145.0, "tax": 2000.0})
        self.assertIsNone(kis.parse_trade_cost(dict(row, sll_amt="0")))
        self.assertIsNone(kis.parse_trade_cost(dict(row, trad_dt="")))

    def test_range_split_over_the_two_trs(self):
        today = dt.date(2026, 11, 18)
        calls = []
        with patch.object(kis, "_get_kis_account", return_value=("12345678", "01")), \
                patch.object(kis, "_fetch_ccld_range", side_effect=lambda tr, c, p, s, e: calls.append((tr, s, e)) or []):
            kis.fetch_kis_executions(dt.date(2026, 5, 1), today=today)
            recent = today - dt.timedelta(days=kis._CCLD_RECENT_DAYS)
            self.assertEqual(calls, [("CTSC9215R", dt.date(2026, 5, 1), recent - dt.timedelta(days=1)),
                                     ("TTTC0081R", recent, today)])
            calls.clear()
            kis.fetch_kis_executions(today - dt.timedelta(days=3), today=today)
            self.assertEqual([c[0] for c in calls], ["TTTC0081R"])


class TestSaveAndTab(TempDBMixin, unittest.TestCase):

    def test_apply_saves_trades_and_marks_together(self):
        trade_db.upsert_trade(lot(None, "000660", "2026-10-01", 10, 100.0))
        open_lots = trade_db.get_open_trades()
        plan = plan_kis_import([ex("2026-11-20", "9", "000660", "sell", 4, 300.0),
                                ex("2026-11-21", "3", "005930", "buy", 2, 50.0)], {}, open_lots)
        trade_db.apply_kis_import(plan.records, plan.imports)
        rows = trade_db.load_all_trades()
        self.assertEqual(sorted((r["ticker"], r["qty"], r["sell_qty"]) for r in rows),
                         [("000660", 4.0, 4.0), ("000660", 6.0, 0.0), ("005930", 2.0, 0.0)])
        self.assertEqual(set(trade_db.load_kis_imports()), {"2026-11-20:9", "2026-11-21:3"})
        self.assertEqual(trade_db.last_kis_import_date(), "2026-11-21")

    def test_a_failed_save_leaves_neither(self):
        bad = [dict(lot(None, "005930", "2026-11-20", 1, 1.0), buy_price=float("nan"))]
        with self.assertRaises(ValueError):
            trade_db.apply_kis_import(bad, [("2026-11-20:1", 1.0, 1.0)])
        self.assertEqual(trade_db.load_kis_imports(), {})
        self.assertEqual(trade_db.load_all_trades(), [])

    def _tab(self):
        from ui.history_tab import TradingHistoryTab
        with patch.object(TradingHistoryTab, "_start_price_fetch", lambda self: None):
            tab = TradingHistoryTab()
            tab.load_from_db()
        self.addCleanup(tab._settings_save_timer.stop)
        patcher = patch.object(TradingHistoryTab, "_start_price_fetch", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tab

    def _result(self, executions, deposit=1_000_000.0, holdings=None):
        return {"deposit": deposit, "holdings": holdings or [], "executions": executions,
                "markets": {e["ticker"]: "KOSPI" for e in executions}, "exec_error": ""}

    def test_tab_applies_after_the_preview_is_accepted(self):
        tab = self._tab()
        msgs = []
        tab.status_message.connect(msgs.append)
        execs = [ex("2026-11-20", "1", "005930", "buy", 3, 100.0, name="삼성전자")]
        with patch("ui.history_tab.KisSyncDialog.exec", return_value=QDialog.DialogCode.Accepted):
            tab._on_kis_account_fetched(self._result(execs, holdings=[{"ticker": "005930", "qty": 3.0}]), "")
        self.assertEqual(tab._deposit_edit.text(), "1,000,000")
        self.assertEqual([(r["company"], r["qty"]) for r in tab._open_data], [("삼성전자", 3.0)])
        self.assertIn("체결 1건 반영", msgs[-1])
        self.assertEqual(tab._kis_dialog.table.rowCount(), 1)
        # the same fetch again: nothing new, no dialog
        tab._kis_dialog = None
        tab._on_kis_account_fetched(self._result(execs, holdings=[{"ticker": "005930", "qty": 3.0}]), "")
        self.assertIsNone(tab._kis_dialog)
        self.assertIn("새 체결 없음", msgs[-1])
        self.assertEqual(tab._kis_query_start(), dt.date(2026, 11, 20))

    def test_tab_cancel_saves_nothing_and_first_query_looks_back(self):
        tab = self._tab()
        self.assertEqual(tab._kis_query_start(), dt.date.today() - dt.timedelta(days=89))
        with patch("ui.history_tab.KisSyncDialog.exec", return_value=QDialog.DialogCode.Rejected):
            tab._on_kis_account_fetched(self._result([ex("2026-11-20", "1", "005930", "buy", 3, 100.0)]), "")
        self.assertEqual(trade_db.load_all_trades(), [])
        self.assertEqual(trade_db.load_kis_imports(), {})

    def test_execution_failure_still_fills_the_deposit(self):
        tab = self._tab()
        with patch("ui.history_tab.QMessageBox.warning") as warn:
            tab._on_kis_account_fetched(dict(self._result([], deposit=5.0), exec_error="boom"), "")
        self.assertEqual(tab._deposit_edit.text(), "5")
        self.assertIn("boom", warn.call_args.args[2])

    def test_auto_sync_saves_without_the_preview_and_leaves_the_deposit(self):
        tab = self._tab()
        tab._deposit_edit.setText("7")
        msgs = []
        tab.status_message.connect(msgs.append)
        execs = [ex("2026-11-20", "1", "005930", "buy", 3, 100.0, name="삼성전자")]
        costs = [{"date": "2026-11-20", "ticker": "005930", "buy_amount": 300.0, "sell_amount": 0.0, "fee": 1.0, "tax": 0.0}]
        with patch("ui.history_tab.KisSyncDialog.exec") as dlg_exec:
            tab._on_kis_account_fetched(dict(self._result(execs), costs=costs, auto=True), "")
        dlg_exec.assert_not_called()
        self.assertEqual(tab._deposit_edit.text(), "7")
        self.assertEqual([(r["company"], r["buy_amount"]) for r in tab._open_data], [("삼성전자", 301.0)])
        self.assertIn("KIS 자동 반영: 체결 1건", msgs[-1])

    def test_auto_sync_tick_is_rate_limited_and_stays_out_of_an_open_preview(self):
        tab = self._tab()
        started = []
        with patch("ui.history_tab.KisAccountThread") as thread_cls:
            thread_cls.return_value.isRunning.return_value = True
            thread_cls.side_effect = lambda *a, **k: started.append(k) or thread_cls.return_value
            tab._kis_preview_open = True
            tab.auto_kis_sync_tick()
            self.assertEqual(started, [])
            tab._kis_preview_open = False
            tab.auto_kis_sync_tick()
            self.assertEqual(started, [{"auto": True}])
            thread_cls.return_value.isRunning.return_value = False
            tab.auto_kis_sync_tick()                         # within the interval
            self.assertEqual(len(started), 1)
            tab._kis_auto_last -= 31 * 60
            tab.auto_kis_sync_tick()
            self.assertEqual(len(started), 2)

    def test_button_label(self):
        tab = self._tab()
        self.assertEqual(tab._fetch_dep_btn.text(), "KIS 조회")


if __name__ == "__main__":
    unittest.main()
