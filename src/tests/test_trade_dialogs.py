"""tests/test_trade_dialogs.py — BuyEditDialog / SellEditDialog / TradeEntryDialog
input validation and the amount = price x qty fallback, headless."""
import unittest
from unittest.mock import patch, MagicMock

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ui.dialogs.trade_edit import BuyEditDialog, SellEditDialog, TradeEntryDialog  # noqa: E402


class TestBuyEditDialog(unittest.TestCase):

    def test_blank_amount_falls_back_to_price_times_qty(self):
        dlg = BuyEditDialog({"buy_date": "2026-1-5", "buy_price": 70000.0, "qty": 10.0, "buy_amount": 0.0})
        dlg.buy_amount_edit.setText("")
        dlg.on_save()
        self.assertEqual(dlg.result_data["buy_amount"], 700000.0)
        self.assertEqual(dlg.result_data["buy_date"], "2026-01-05")   # normalised, zero-padded

    def test_explicit_amount_wins(self):
        dlg = BuyEditDialog({"buy_date": "2026-01-05", "buy_price": 70000.0, "qty": 10.0, "buy_amount": 0.0})
        dlg.buy_amount_edit.setText("699,500")
        dlg.on_save()
        self.assertEqual(dlg.result_data["buy_amount"], 699500.0)

    def test_moving_buy_date_past_the_sell_date_is_rejected(self):
        """review_codex.md #5: the buy side must keep the sell side's checks."""
        rec = {"buy_date": "2021-01-01", "buy_price": 100.0, "qty": 10.0, "buy_amount": 1000.0,
               "sell_date": "2021-02-01", "sell_qty": 10.0}
        dlg = BuyEditDialog(rec)
        dlg.buy_date_edit.setText("2021-03-01")
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)

    def test_shrinking_qty_below_the_sold_qty_is_rejected(self):
        rec = {"buy_date": "2021-01-01", "buy_price": 100.0, "qty": 10.0, "buy_amount": 1000.0,
               "sell_date": "2021-02-01", "sell_qty": 10.0}
        dlg = BuyEditDialog(rec)
        dlg.buy_qty_edit.setText("5")
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)

    def test_invalid_field_blocks_save(self):
        dlg = BuyEditDialog({"buy_date": "not-a-date", "buy_price": 70000.0, "qty": 10.0})
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)


class TestSellEditDialog(unittest.TestCase):

    def test_blank_amount_falls_back_to_price_times_qty(self):
        dlg = SellEditDialog({"buy_date": "2026-01-05", "sell_date": "2026-02-05", "sell_price": 75000.0, "sell_qty": 10.0})
        dlg.sell_amount_edit.setText("")
        dlg.on_save()
        self.assertEqual(dlg.result_data["sell_amount"], 750000.0)

    def test_sell_before_buy_is_rejected(self):
        dlg = SellEditDialog({"buy_date": "2026-03-01", "sell_date": "2026-02-05", "sell_price": 75000.0, "sell_qty": 10.0})
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)

    def test_empty_sell_date_keeps_the_position_open(self):
        dlg = SellEditDialog({"buy_date": "2026-01-05"})
        dlg.on_save()
        self.assertEqual(dlg.result_data, {"sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0})

    def test_price_required_once_a_sell_date_is_entered(self):
        dlg = SellEditDialog({"buy_date": "2026-01-05"})
        dlg.sell_date_edit.setText("2026-02-05")
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)

    def test_over_selling_is_rejected(self):
        """sell_qty beyond the original holding must be blocked -- it used
        to bypass compute_pl_fields' proration and inflate realized P/L
        with proceeds from shares never owned (review_agy.md #2)."""
        dlg = SellEditDialog({"buy_date": "2026-01-05", "qty": 100.0,
                               "sell_date": "2026-02-05", "sell_price": 1500.0, "sell_qty": 150.0})
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg.on_save()
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)

    def test_selling_exactly_the_full_holding_is_allowed(self):
        dlg = SellEditDialog({"buy_date": "2026-01-05", "qty": 100.0,
                               "sell_date": "2026-02-05", "sell_price": 1500.0, "sell_qty": 100.0})
        dlg.on_save()
        self.assertIsNotNone(dlg.result_data)
        self.assertEqual(dlg.result_data["sell_qty"], 100.0)


class TestTradeEntryDialog(unittest.TestCase):

    def _dlg(self):
        dlg = TradeEntryDialog()
        dlg.ticker_edit.setText("005930")
        dlg.buy_date_edit.setText("2026-01-05")
        dlg.buy_price_edit.setText("70,000")
        dlg.qty_edit.setText("10")
        return dlg

    def test_missing_ticker_is_rejected_before_validation(self):
        dlg = self._dlg()
        dlg.ticker_edit.setText("")
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn, \
             patch("ui.dialogs.trade_edit.TickerValidateThread") as thread_cls:
            dlg.on_save()
        warn.assert_called_once()
        thread_cls.assert_not_called()

    def test_save_validates_the_ticker_off_thread_then_fills_the_record(self):
        dlg = self._dlg()
        with patch("ui.dialogs.trade_edit.TickerValidateThread") as thread_cls:
            thread_cls.return_value = MagicMock()
            dlg.on_save()
            thread_cls.assert_called_once_with("KOSPI", "005930")
            self.assertTrue(thread_cls.return_value.start.called)
            self.assertFalse(dlg.save_btn.isEnabled())   # locked while the lookup runs

        dlg._on_ticker_validated(True, "Samsung Electronics", "")
        self.assertTrue(dlg.save_btn.isEnabled())
        r = dlg.result_data
        self.assertEqual((r["ticker"], r["company"], r["market"]), ("005930", "Samsung Electronics", "KOSPI"))
        self.assertEqual(r["buy_amount"], 700000.0)      # blank amount -> price x qty
        self.assertEqual(r["sell_date"], "")

    def test_unknown_ticker_keeps_the_dialog_open(self):
        dlg = self._dlg()
        with patch("ui.dialogs.trade_edit.TickerValidateThread"):
            dlg.on_save()
        with patch("ui.dialogs.trade_edit.QMessageBox.warning") as warn:
            dlg._on_ticker_validated(False, "", "not found")
        warn.assert_called_once()
        self.assertIsNone(dlg.result_data)
        self.assertTrue(dlg.save_btn.isEnabled())
        self.assertTrue(dlg.ticker_edit.isEnabled())

    def test_reply_builds_the_record_from_the_inputs_it_validated(self):
        """review_codex.md #3: inputs changed while the check runs must not
        reach the record (the reply is for the ticker that was sent)."""
        dlg = self._dlg()
        with patch("ui.dialogs.trade_edit.TickerValidateThread"):
            dlg.on_save()
        self.assertFalse(dlg.ticker_edit.isEnabled())   # inputs locked while pending
        dlg.ticker_edit.setText("000660")
        dlg.buy_price_edit.setText("1")
        dlg.qty_edit.setText("999")
        dlg.buy_date_edit.setText("2020-01-01")
        dlg._on_ticker_validated(True, "Samsung Electronics", "")
        r = dlg.result_data
        self.assertEqual((r["ticker"], r["company"]), ("005930", "Samsung Electronics"))
        self.assertEqual((r["buy_date"], r["buy_price"], r["qty"]), ("2026-01-05", 70000.0, 10.0))

    def test_stray_reply_without_a_pending_request_is_ignored(self):
        dlg = self._dlg()
        dlg._on_ticker_validated(True, "Samsung Electronics", "")
        self.assertIsNone(dlg.result_data)


class TestEditDialogsKeepPrecision(unittest.TestCase):
    """review_codex.md #4: opening an edit dialog and saving unchanged must
    round-trip decimals (they used to be pre-filled as ",.0f"/",.2f")."""

    def test_sell_edit_unchanged_save_round_trips(self):
        rec = {"buy_date": "2026-01-05", "qty": 10.0, "sell_date": "2026-02-05",
               "sell_price": 123.45, "sell_qty": 10.0, "sell_amount": 1234.5}
        dlg = SellEditDialog(rec)
        dlg.on_save()
        self.assertEqual(dlg.result_data, {"sell_date": "2026-02-05", "sell_price": 123.45,
                                           "sell_qty": 10.0, "sell_amount": 1234.5})

    def test_buy_edit_unchanged_save_round_trips(self):
        rec = {"buy_date": "2026-01-05", "buy_price": 123.4567, "qty": 2.5, "buy_amount": 308.64175}
        dlg = BuyEditDialog(rec)
        dlg.on_save()
        self.assertEqual(dlg.result_data, {"buy_date": "2026-01-05", "buy_price": 123.4567,
                                           "qty": 2.5, "buy_amount": 308.64175})

    def test_sell_price_accepts_typed_decimals(self):
        dlg = SellEditDialog({"buy_date": "2026-01-05"})
        dlg.sell_price_edit.textEdited.emit("1234.")
        dlg.sell_price_edit.setText(dlg.sell_price_edit.text() + "5")
        dlg.sell_price_edit.textEdited.emit(dlg.sell_price_edit.text())
        self.assertEqual(dlg.sell_price_edit.text(), "1,234.5")


if __name__ == "__main__":
    unittest.main()
