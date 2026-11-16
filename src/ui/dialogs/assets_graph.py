"""ui/dialogs/assets_graph.py — TotalAssetsGraphDialog — cumulative return graph for the Total Assets tab.

Split out of the former single ui/dialogs.py (2026-09-17).

2026-11-16 (user decision, items 1-7 of the review): every weekly point used
to carry two two-line value labels (92 labels for 46 weeks) and every week was
an x tick, so the values ran into each other. Now: no per-point labels; the
latest value of each series at the right end, nudged apart; the peak of Total
Assets marked once; a vertical line under the mouse with the three values of
that week in one line above the chart; month ticks; amounts in 억; and the gap
between Total Assets and KOSPI shaded (PROFIT ahead, LOSS behind).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.ticker import FixedLocator
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

from i18n import tr
from ui.colors import PROFIT, LOSS
from ui.common import make_button, button_row, fit_to_screen, create_font, FONT_BODY
from ui.theme import ACCENT, TEXT, TEXT_MUTED, TEXT_FAINT, SURFACE

logger = logging.getLogger(__name__)

# Legend / readout names of the three series.
_LBL_ASSETS = tr("총자산", "Total Assets")
_LBL_ASSETS_USD = tr("총자산 ($)", "Total Assets ($)")
_LBL_KOSPI = "KOSPI"

_SHADE_ALPHA = 0.12        # Total Assets vs KOSPI gap
_END_PAD = 0.012           # gap between the last point and its end label, share of the date span
_RIGHT_ROOM = 0.17         # room right of the last point for the end labels, share of the date span
_MIN_LABEL_GAP = 0.045     # end labels closer than this share of the y range are pushed apart


def fmt_krw(amount: float) -> str:
    """KRW amount for a label: 6.34억 (Korean UI) / KRW 633.7M (English UI)."""
    return tr(f"{amount / 1e8:,.2f}억", f"KRW {amount / 1e6:,.1f}M")


def _mpl(text: str) -> str:
    """Matplotlib reads text between two '$' as math; a dollar amount must not."""
    return text.replace("$", r"\$")


def spread_labels(values: list[float], min_gap: float) -> list[float]:
    """Label positions for `values` (any order) at least `min_gap` apart, each
    as close to its value as that allows: sorted, pushed up past the one
    below, then the whole stack shifted down by half the overshoot so it stays
    centred on the values."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    placed: list[float] = []
    for i in order:
        y = values[i]
        if placed and y < placed[-1] + min_gap:
            y = placed[-1] + min_gap
        placed.append(y)
    shift = (sum(p - values[i] for p, i in zip(placed, order)) / len(order)) if order else 0.0
    out = [0.0] * len(values)
    for p, i in zip(placed, order):
        out[i] = p - shift
    return out


def month_ticks(x0: float, x1: float) -> list[float]:
    """The first day of every month inside [x0, x1] (matplotlib date numbers)."""
    start = mdates.num2date(x0).replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    months = pd.date_range(start, mdates.num2date(x1).replace(tzinfo=None), freq="MS")
    return [float(v) for v in mdates.date2num(list(months)) if x0 <= v <= x1]


# ---------------------------------------------------------------------------
# TotalAssetsGraphDialog — Cumulative Returns graph
# ---------------------------------------------------------------------------
class TotalAssetsGraphDialog(QDialog):
    def __init__(self, dates, kospi_returns, asset_returns, usd_asset_returns, totals, usd_totals, parent=None,
                 on_report=None):
        """on_report: called with no arguments by the "Report" button (the
        owning tab fetches in a thread and opens AssetsReportDialog); the
        button is hidden when it is None."""
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setWindowTitle(tr("총자산 — 누적 수익률", "Total Assets — Cumulative Return"))
        fit_to_screen(self, 900, 600)
        self.showMaximized()

        self._dates = list(dates)
        self._x = np.asarray(mdates.date2num([pd.to_datetime(d) for d in self._dates]), dtype=float)
        self._kospi = list(kospi_returns)
        self._asset = list(asset_returns)
        self._usd = list(usd_asset_returns)
        self._totals = list(totals)
        self._usd_totals = list(usd_totals)

        layout = QVBoxLayout(self)
        # Values of the week under the mouse (the latest week otherwise), one line.
        self.readout_lbl = QLabel("")
        self.readout_lbl.setFont(create_font(FONT_BODY))
        self.readout_lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.readout_lbl)

        self.fig = Figure(figsize=(9, 6), constrained_layout=True)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)
        layout.addWidget(self.canvas, 1)

        self._crosshair = None
        if len(self._x):
            self._plot_series()
            self._shade_gap()
            self._mark_peak()
            self._end_labels()
            self._crosshair = self.ax.axvline(self._x[-1], color=TEXT_FAINT, linewidth=1, visible=False)
            self.canvas.mpl_connect("motion_notify_event", self._on_motion)
            self.canvas.mpl_connect("axes_leave_event", self._on_leave)
            self.canvas.mpl_connect("figure_leave_event", self._on_leave)
        self._update_readout(None)

        self._build_buttons(layout, on_report)

    # ------------------------------------------------------------------ chart
    def _plot_series(self) -> None:
        """The three return lines, the zero line, month ticks, legend and titles."""
        ax, x = self.ax, self._x
        # Series identity, not price direction: ink for the main series,
        # accent for its USD view, muted dashed for the benchmark (red/blue
        # stay reserved for gain/loss, docs/ui.md 1.1).
        ax.plot(x, self._kospi, color=TEXT_MUTED, linewidth=1.6, linestyle="--", label=_LBL_KOSPI)
        ax.plot(x, self._asset, color=TEXT, linewidth=2.2, label=_LBL_ASSETS)
        ax.plot(x, self._usd, color=ACCENT, linewidth=2, label=_mpl(_LBL_ASSETS_USD))
        ax.axhline(0, color=TEXT_FAINT, linestyle="--", linewidth=1)

        span = (x[-1] - x[0]) or 7.0
        ax.set_xlim(x[0], x[-1] + span * _RIGHT_ROOM)
        ticks = month_ticks(x[0], x[-1])
        ax.xaxis.set_major_locator(FixedLocator(ticks or list(x)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m" if ticks else "%y.%m.%d"))

        ax.legend(fontsize=10, loc="upper left")
        ax.grid(True, linestyle=":", alpha=0.5)
        ax.set_ylabel(tr("누적 수익률 (%)", "Cumulative Return (%)"), fontsize=10)
        ax.set_title(tr("총자산과 KOSPI 누적 수익률", "Total Assets vs KOSPI Cumulative Return"),
                     fontsize=12, fontweight="bold")

    def _shade_gap(self) -> None:
        """Total Assets vs KOSPI: PROFIT where it is ahead, LOSS where behind."""
        a, k = np.asarray(self._asset, float), np.asarray(self._kospi, float)
        self.ax.fill_between(self._x, k, a, where=a >= k, color=PROFIT, alpha=_SHADE_ALPHA, interpolate=True,
                             linewidth=0)
        self.ax.fill_between(self._x, k, a, where=a < k, color=LOSS, alpha=_SHADE_ALPHA, interpolate=True,
                             linewidth=0)

    def _mark_peak(self) -> None:
        """The highest Total Assets week, once: a dot and its value above it."""
        i = int(np.argmax(self._asset))
        x, y = self._x[i], self._asset[i]
        self.ax.plot([x], [y], "o", color=TEXT, markersize=6, zorder=5)
        self.ax.annotate(tr(f"최고 {y:+.1f}% · {fmt_krw(self._totals[i])}\n{self._dates[i]}",
                            f"Peak {y:+.1f}% · {fmt_krw(self._totals[i])}\n{self._dates[i]}"),
                         (x, y), xytext=(0, 12), textcoords="offset points", ha="center", va="bottom",
                         fontsize=9, color=TEXT, zorder=6,
                         bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": "none", "alpha": 0.85})

    def end_label_texts(self) -> list[tuple[str, float, str]]:
        """(text, value, color) of the three right-end labels, latest week."""
        return [
            (tr(f"{_LBL_ASSETS} {self._asset[-1]:+.1f}% · {fmt_krw(self._totals[-1])}",
                f"{_LBL_ASSETS} {self._asset[-1]:+.1f}% · {fmt_krw(self._totals[-1])}"), self._asset[-1], TEXT),
            (f"{_LBL_ASSETS_USD} {self._usd[-1]:+.1f}% · ${self._usd_totals[-1]:,.0f}", self._usd[-1], ACCENT),
            (f"{_LBL_KOSPI} {self._kospi[-1]:+.1f}%", self._kospi[-1], TEXT_MUTED),
        ]

    def _end_labels(self) -> None:
        """The latest value of each series right of its last point, pushed
        apart when they would overlap."""
        ax, x = self.ax, self._x
        labels = self.end_label_texts()
        lo = min(min(self._asset), min(self._usd), min(self._kospi), 0.0)
        hi = max(max(self._asset), max(self._usd), max(self._kospi), 0.0)
        gap = max(hi - lo, 1.0) * _MIN_LABEL_GAP
        ys = spread_labels([v for _, v, _ in labels], gap)
        span = (x[-1] - x[0]) or 7.0
        self._end_label_artists = []
        for (text, value, color), y in zip(labels, ys):
            ax.plot([x[-1]], [value], "o", color=color, markersize=5, zorder=5)
            self._end_label_artists.append(
                ax.text(x[-1] + span * _END_PAD, y, _mpl(text), color=color, fontsize=9.5, fontweight="bold",
                        va="center", ha="left", zorder=6))

    # ------------------------------------------------------------------ hover
    def _nearest(self, xdata: float) -> int:
        return int(np.argmin(np.abs(self._x - xdata)))

    def _on_motion(self, event) -> None:
        if event.inaxes is not self.ax or event.xdata is None:
            self._on_leave(event)
            return
        i = self._nearest(event.xdata)
        self._crosshair.set_xdata([self._x[i], self._x[i]])
        self._crosshair.set_visible(True)
        self._update_readout(i)
        self.canvas.draw_idle()

    def _on_leave(self, _event) -> None:
        if self._crosshair is not None and self._crosshair.get_visible():
            self._crosshair.set_visible(False)
            self.canvas.draw_idle()
        self._update_readout(None)

    def readout_text(self, i: int | None) -> str:
        """The week `i` (None: the latest) as one line: date · Total Assets ·
        Total Assets ($) · KOSPI, each in its series color."""
        if not self._dates:
            return tr("기록 없음", "No records")
        i = len(self._dates) - 1 if i is None else i

        def part(color, text):
            return f'<span style="color:{color}">{text}</span>'
        return "&nbsp;&nbsp;·&nbsp;&nbsp;".join([
            part(TEXT_MUTED, self._dates[i]),
            part(TEXT, f"{_LBL_ASSETS} {self._asset[i]:+.1f}% ({fmt_krw(self._totals[i])})"),
            part(ACCENT, f"{_LBL_ASSETS_USD} {self._usd[i]:+.1f}% (${self._usd_totals[i]:,.0f})"),
            part(TEXT_MUTED, f"{_LBL_KOSPI} {self._kospi[i]:+.1f}%"),
        ])

    def _update_readout(self, i: int | None) -> None:
        self.readout_lbl.setText(self.readout_text(i))

    # ---------------------------------------------------------------- buttons
    def _build_buttons(self, layout: QVBoxLayout, on_report) -> None:
        self.report_btn = make_button(tr("리포트", "Report"))
        self.report_btn.setToolTip(tr("매월 초와 중순의 보유 평가액, 현금, KOSPI", "Positions, cash and KOSPI at the start and middle of every month"))
        if on_report is not None:
            self.report_btn.clicked.connect(on_report)
        else:
            self.report_btn.hide()
        # View dialog: 리포트 → 닫기, bottom right (docs/ui.md 8.17).
        layout.addLayout(button_row(self.report_btn, make_button(tr("닫기", "Close"), on_click=self.accept)))
