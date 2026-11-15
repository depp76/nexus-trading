"""threads/realtime.py — RealtimePriceThread (Phase 3-1 split)

Split out from: main.py L176-198 (2026-08-29 feat/3-1-modularize)
"""
import logging

from PyQt6.QtCore import QThread, pyqtSignal

from i18n import tr

logger = logging.getLogger(__name__)


class RealtimePriceThread(QThread):
    # (prices, complete): every quote that came back, and False when this
    # request also emitted `failed`. A partial result (KR quotes in, the US
    # call failed) is still applied, but must not clear the failure the
    # same request reported (review_codex.md C12).
    prices_fetched = pyqtSignal(dict, bool)
    status_message = pyqtSignal(str)  # short progress text for MainWindow's status bar
    # Emitted before prices_fetched when a market asked for gave nothing
    # usable: an exception, or no quote at all for that market's tickers
    # (docs/ui.md 8.8). Single tickers missing from an answer are not a
    # failure (a halted or delisted name has no quote).
    failed = pyqtSignal(str)

    def __init__(self, kr_tickers, us_tickers):
        super().__init__()
        self.kr_tickers = kr_tickers
        self.us_tickers = us_tickers

    def run(self):
        prices = {}
        error = ""
        try:
            kr_prices = us_prices = {}
            if self.kr_tickers:
                self.status_message.emit(tr(f"국내 시세 받는 중... (KIS, {len(self.kr_tickers)}종목)",
                                             f"Fetching KR quotes... (KIS, {len(self.kr_tickers)} tickers)"))
                from data_fetcher import fetch_kis_realtime_prices
                kr_prices = fetch_kis_realtime_prices(self.kr_tickers)
                prices.update(kr_prices)
            if self.us_tickers:
                self.status_message.emit(tr("Yahoo Finance 응답 기다리는 중...", "Waiting for Yahoo Finance response..."))
                from data_fetcher import fetch_us_realtime_prices, get_usd_krw_rate
                fx_rate = get_usd_krw_rate()
                us_prices = fetch_us_realtime_prices(self.us_tickers)
                for ticker, price in us_prices.items():
                    prices[ticker] = price * fx_rate
            if (self.kr_tickers or self.us_tickers) and not prices:
                error = tr("시세를 받지 못함 (KIS/Yahoo)", "no quotes received (KIS/Yahoo)")
            elif self.kr_tickers and not kr_prices:
                error = tr("국내 시세를 받지 못함 (KIS)", "no KR quotes received (KIS)")
            elif self.us_tickers and not us_prices:
                error = tr("미국 시세를 받지 못함 (Yahoo)", "no US quotes received (Yahoo)")
        except Exception as e:
            # Before this, an exception here ended the thread silently.
            logger.warning("RealtimePriceThread failed: %s", e, exc_info=True)
            error = str(e) or type(e).__name__
        if error:
            self.failed.emit(error)
        self.prices_fetched.emit(prices, not error)
