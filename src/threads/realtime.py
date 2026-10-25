"""threads/realtime.py — RealtimePriceThread (Phase 3-1 split)

Split out from: main.py L176-198 (2026-08-29 feat/3-1-modularize)
"""
import logging

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class RealtimePriceThread(QThread):
    prices_fetched = pyqtSignal(dict)
    status_message = pyqtSignal(str)  # short progress text for MainWindow's status bar
    # Emitted before prices_fetched when nothing usable came back: an
    # exception, or no quote for the tickers asked for (docs/ui.md 8.8).
    failed = pyqtSignal(str)

    def __init__(self, kr_tickers, us_tickers):
        super().__init__()
        self.kr_tickers = kr_tickers
        self.us_tickers = us_tickers

    def run(self):
        prices = {}
        error = ""
        try:
            if self.kr_tickers:
                self.status_message.emit(f"Fetching KR quotes... (KIS, {len(self.kr_tickers)} tickers)")
                from data_fetcher import fetch_kis_realtime_prices
                prices.update(fetch_kis_realtime_prices(self.kr_tickers))
            if self.us_tickers:
                self.status_message.emit("Waiting for Yahoo Finance response...")
                from data_fetcher import fetch_us_realtime_prices, get_usd_krw_rate
                fx_rate = get_usd_krw_rate()
                us_prices = fetch_us_realtime_prices(self.us_tickers)
                for ticker, price in us_prices.items():
                    prices[ticker] = price * fx_rate
            if (self.kr_tickers or self.us_tickers) and not prices:
                error = "no quotes received (KIS/Yahoo)"
        except Exception as e:
            # Before this, an exception here ended the thread silently.
            logger.warning("RealtimePriceThread failed: %s", e, exc_info=True)
            error = str(e) or type(e).__name__
        if error:
            self.failed.emit(error)
        self.prices_fetched.emit(prices)
