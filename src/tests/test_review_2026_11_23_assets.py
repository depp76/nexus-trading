"""tests/test_review_2026_11_23_assets.py — the Trading History -> Total Assets
figure (review 2026-11-23 #2): no total valued at cost before the first quotes
answer, and a failed quote never wipes a price the lot already has. #1 (the
D+2 deposit) is in test_review_2026_11_20_history.TestDepositUnreadable."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import trade_db  # noqa: E402
from tests.test_review_2026_11_20_history import _TabMixin, lot  # noqa: E402

app = QApplication.instance() or QApplication([])


class TestLiveTotalWaitsForQuotes(_TabMixin, unittest.TestCase):

    def _tab_with_lot(self):
        trade_db.upsert_trades([lot("k1", "005930", "2026-11-02", 10, 100.0)])
        tab = self._tab()                       # load_from_db; the fetch is patched out
        self._pin_cash(tab, "500")
        return tab

    @staticmethod
    def _pin_cash(tab, deposit):
        """Deposit and withdrawal set here, not read from the machine's QSettings."""
        tab._withdrawal_edit.setText("0")
        tab._deposit_edit.setText(deposit)
        tab._settings_save_timer.stop()

    @staticmethod
    def _answer(tab, price):
        """The price fetch's answer for the one open lot (the fetch itself is patched out)."""
        rec = tab._open_data[0]
        tab._price_req = [(rec, rec["company"], rec["ticker"])]
        tab._on_prices_ready([{"index": 0, "curr_price": price, "ticker": "005930", "market": "KOSPI"}],
                             tab._price_req_id)

    def test_no_total_at_cost_before_the_quotes(self):
        tab = self._tab_with_lot()
        emitted = []
        tab.total_asset_updated.connect(emitted.append)
        tab._refresh_summary()                  # e.g. a deposit edit while the fetch runs
        self.assertEqual(emitted, [])
        self._answer(tab, 120.0)
        self.assertEqual(emitted[-1], 1200.0 + 500.0)

    def test_failed_fetch_still_releases_the_total(self):
        tab = self._tab_with_lot()
        emitted = []
        tab.total_asset_updated.connect(emitted.append)
        tab._on_prices_ready([], tab._price_req_id)     # the thread failed: empty answer
        self.assertEqual(emitted, [1000.0 + 500.0])     # at cost, as before, but sent

    def test_stale_answer_does_not_release(self):
        tab = self._tab_with_lot()
        emitted = []
        tab.total_asset_updated.connect(emitted.append)
        tab._on_prices_ready([], tab._price_req_id - 1)
        self.assertEqual(emitted, [])

    def test_empty_ledger_sends_at_once(self):
        tab = self._tab()
        emitted = []
        tab.total_asset_updated.connect(emitted.append)
        self._pin_cash(tab, "700")
        tab._refresh_summary()
        self.assertEqual(emitted[-1], 700.0)

    def test_failed_quote_keeps_the_last_price(self):
        tab = self._tab_with_lot()
        self._answer(tab, 120.0)
        self._answer(tab, 0.0)                  # next refresh: this quote failed
        self.assertEqual(tab._open_data[0]["curr_price"], 120.0)
        emitted = []
        tab.total_asset_updated.connect(emitted.append)
        tab._refresh_summary()
        self.assertEqual(emitted[-1], 1700.0)

    def test_realtime_zero_keeps_the_last_price(self):
        tab = self._tab_with_lot()
        self._answer(tab, 120.0)
        tab._on_realtime_prices_fetched({"005930": 0.0})
        self.assertEqual(tab._open_data[0]["curr_price"], 120.0)
        tab._on_realtime_prices_fetched({"005930": 130.0})
        self.assertEqual(tab._open_data[0]["curr_price"], 130.0)


if __name__ == "__main__":
    unittest.main()
