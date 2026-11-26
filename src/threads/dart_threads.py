"""DART reads for the screens (docs/dart_db_plan_2026-11-26.md 13).

``FinancialsLoadThread`` reads one stock's financials, segments and check
status from dart.db for the MA chart's 재무 tab. It is a local read-only
SQLite query, but it runs off the UI thread like every data access of a
screen (a busy DB must never freeze a dialog).
"""
import logging

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class FinancialsLoadThread(QThread):
    """finished_load(data | None, error, ticker): ``company_financials``'s dict
    (None when the stock is not in dart.db), the error text on failure, and
    the ticker it was asked for."""

    finished_load = pyqtSignal(object, str, str)

    def __init__(self, ticker: str, parent=None):
        super().__init__(parent)
        self.ticker = ticker

    def run(self):
        try:
            from data_fetcher import company_financials
            data = company_financials(self.ticker)
            self.finished_load.emit(data, "", self.ticker)
        except Exception as e:
            # boundary: a QThread run() reports any failure through its signal.
            logger.exception("Loading DART financials failed for %s", self.ticker)
            self.finished_load.emit(None, f"{type(e).__name__}: {e}", self.ticker)
