"""ui/dialogs/index_ma.py — IndexMaDialog — 2x2 chart of Close/MA60/MA200 for the major indices.

Split out of the former single ui/dialogs.py (2026-09-17). 2026-11-18
(docs/ma60_unification_2026-11-18.md 1.2-4): the 10/20/50-day lines became
the strategy's 60-day line and the 200-day reference, read from the strict
SMA columns of data.market.fetch_index_mas (warm-up loaded ahead, the last
year shown); a line missing on part of the shown year is labelled
"데이터 부족" instead of being filled in."""
import logging

import numpy as np

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout,
)

import matplotlib.dates as mdates
import mplcursors
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

logger = logging.getLogger(__name__)

from data_fetcher import INDEX_TICKERS
from i18n import tr
from ui.common import make_button, button_row, fit_to_screen
from ui.colors import MA_RAMP
from ui.theme import TEXT, TEXT_MUTED, HDR_BG, LINE

# (window, legend label) of the MA lines; columns are "SMA<window>".
_INDEX_MAS = ((60, tr("60일 이평", "60-Day MA")), (200, tr("200일 이평", "200-Day MA")))


def missing_mas(df) -> list:
    """The windows whose SMA column is missing on any shown day (not enough
    history before the first shown day)."""
    out = []
    for w, _label in _INDEX_MAS:
        col = df.get_column(f"SMA{w}").to_numpy() if f"SMA{w}" in df.columns else None
        if col is None or not np.isfinite(np.asarray(col, dtype=float)).all():
            out.append(w)
    return out


# ---------------------------------------------------------------------------
# IndexMaDialog — Index MA20 chart dialog (2x2 subplots)
# ---------------------------------------------------------------------------
class IndexMaDialog(QDialog):
    """2x2 subplot dialog showing Close, the 60-day and the 200-day MA for each major index."""

    def __init__(self, results, parent=None):
        """
        results: dict {label: (DataFrame | None, error_str)}
        """
        super().__init__(parent)
        self.setWindowTitle(tr("주요 지수 — 이동평균", "Major Indices — Moving Averages"))
        fit_to_screen(self, 1100, 620)

        layout = QVBoxLayout(self)

        fig = Figure(figsize=(15, 6), constrained_layout=True)
        labels = [lbl for lbl in INDEX_TICKERS.keys() if lbl not in ("Dow Jones", "US10YT", "JP10YT", "KR3YT", "VIX", "VKOSPI", "WTI")]   # KOSPI, KOSDAQ, S&P500, NASDAQ, NASDAQ 100

        cols = 3 if len(labels) > 4 else 2
        for idx, label in enumerate(labels):
            ax = fig.add_subplot(2, cols, idx + 1)
            df, err = results.get(label, (None, tr("데이터 없음", "No data")))

            if df is not None and not df.is_empty() and all(f"SMA{w}" in df.columns for w, _l in _INDEX_MAS):
                dates = df.get_column("Date").to_numpy()
                # Same vocabulary as StockMaDialog: ink close, one accent ramp
                # for the MAs (shorter = lighter), no per-point markers.
                lines = [ax.plot(dates, df.get_column("Close").to_numpy(), color=TEXT,
                                 linewidth=1.5, label=tr("종가", "Close"))[0]]
                short = missing_mas(df)
                for w, ma_label in _INDEX_MAS:
                    if w in short:
                        ma_label += tr(" (데이터 부족)", " (not enough data)")
                    lines.append(ax.plot(dates, df.get_column(f"SMA{w}").to_numpy(), color=MA_RAMP[f"MA{w}"],
                                         linewidth=1.3, label=ma_label)[0])
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
                fig.autofmt_xdate(rotation=25)
                ax.legend(fontsize=8)
                ax.grid(True, linestyle=":", alpha=0.5)

                cursor = mplcursors.cursor(lines, hover=2)
                @cursor.connect("add")
                def on_add(sel):
                    date_str = mdates.num2date(sel.target[0]).strftime("%Y-%m-%d")
                    val = sel.target[1]
                    lbl = sel.artist.get_label()
                    sel.annotation.set_text(f"{lbl}\n{date_str}: {val:,.0f}")
                    sel.annotation.get_bbox_patch().set(fc=HDR_BG, alpha=0.95, edgecolor=LINE)
            else:
                ax.text(0.5, 0.5, tr(f"불러오지 못했습니다\n{err or ''}", f"Failed to load\n{err or ''}"),
                        ha="center", va="center", transform=ax.transAxes,
                        color=TEXT_MUTED, fontsize=10)

            ax.set_title(label, fontsize=11, fontweight="bold")

        canvas = FigureCanvas(fig)
        layout.addWidget(canvas)

        layout.addLayout(button_row(make_button(tr("닫기", "Close"), on_click=self.accept)))
