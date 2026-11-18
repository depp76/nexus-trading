"""ui/ma_chart.py — StockMaLauncherMixin: open a StockMaDialog for a ticker
from a background StockMaThread, keeping the dialogs alive until closed.

Trading Universe and the former Strategy/Auto Trading tab each carried their
own copy of this (thread start, finished slot, WA_DeleteOnClose dialog, a list
of open dialogs pruned of already-deleted ones); UniverseTab is the only user
now. Requires ui.common.ThreadOwnerMixin on the same class for _track_thread().
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

from i18n import tr
from threads.fetch_threads import StockMaThread
from ui.dialogs import StockMaDialog


class StockMaLauncherMixin:

    def _on_stock_ma_status(self, msg: str):
        """Hook for tabs that surface progress text (Universe emits it to
        the footer); no-op by default."""

    def _show_stock_ma(self, ticker: str, name: str, market: str, change_mode: str = "pct"):
        if not ticker:
            return
        self._on_stock_ma_status(tr(f"{name} ({ticker}) 이평 차트를 불러오는 중...",
                                    f"Loading MA20 & MA50 for {name} ({ticker})..."))
        # The thread echoes market/change_mode back in its finished signal,
        # so the slot gets this request's context even with several in flight.
        # The chart's strategy lines follow the variant pinned in the tab, if any.
        ctx_fn = getattr(self, "_signal_context", None)
        variant = (ctx_fn().default_variant or None) if ctx_fn is not None else None
        thread = self._track_thread(StockMaThread(ticker, name, market, change_mode, variant))
        thread.finished.connect(self._on_stock_ma_loaded)
        thread.start()

    def _on_stock_ma_loaded(self, ticker, name, df, error, investor_data, market, change_mode, overlays=None):
        self._on_stock_ma_status(tr(f"{name} ({ticker}) 이평 차트를 불러왔습니다.",
                                    f"MA chart loaded for {name} ({ticker})."))
        if error and df is None:
            QMessageBox.warning(self, "Trading Universe", tr(f"{ticker} 데이터를 불러오지 못했습니다:\n{error}",
                                                             f"Failed to load data for {ticker}:\n{error}"))
            return
        ctx_fn = getattr(self, "_signal_context", None)
        dlg = StockMaDialog(ticker, name, market, df, investor_data=investor_data, parent=None, change_mode=change_mode,
                            signal_ctx=ctx_fn() if ctx_fn is not None else None, overlays=overlays)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        alive = []
        for d in getattr(self, "_open_dialogs", []):
            try:
                if d.isVisible():
                    alive.append(d)
            except RuntimeError:
                continue  # the C++ side is gone (WA_DeleteOnClose); drop the wrapper
        alive.append(dlg)
        self._open_dialogs = alive
        dlg.show()
