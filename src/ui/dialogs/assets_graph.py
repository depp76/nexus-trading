"""ui/dialogs/assets_graph.py — TotalAssetsGraphDialog — cumulative return graph for the Total Assets tab.

Split out of the former single ui/dialogs.py (2026-09-17).

2026-11-16 (user decision, items 1-7 of the review): every weekly point used
to carry two two-line value labels (92 labels for 46 weeks) and every week was
an x tick, so the values ran into each other. Now: no per-point labels; the
latest value of each series at the right end, nudged apart; the peak of Total
Assets marked once; month ticks; amounts in 억; and the gap between Total
Assets and KOSPI shaded (PROFIT ahead, LOSS behind). The same day a marker went
back on every weekly point; resting the mouse on any marker enlarges that
week's three markers and shows all three values of the week in one box next
to it, each line in its series color (no vertical line, no readout line above
the chart: user direction).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.offsetbox import AnnotationBbox, TextArea, VPacker
from matplotlib.ticker import FixedLocator
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QVBoxLayout

from i18n import tr
from ui.colors import PROFIT, LOSS
from ui.common import make_button, button_row, fit_to_screen
from ui.theme import ACCENT, TEXT, TEXT_MUTED, TEXT_FAINT, SURFACE, HDR_BG

logger = logging.getLogger(__name__)

# Legend / tooltip names of the three series.
_LBL_ASSETS = tr("총자산", "Total Assets")
_LBL_ASSETS_USD = tr("총자산 ($)", "Total Assets ($)")
_LBL_KOSPI = "KOSPI"

_SHADE_ALPHA = 0.12        # Total Assets vs KOSPI gap
_POINT_SIZE = 4.5          # the weekly data-point markers (user direction 2026-11-16)
_HOVER_SIZE = 9            # the marker under the mouse, enlarged
_HIT_PX = 10               # the mouse is "on" a marker within this many pixels of its centre
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
                 on_report=None, kospi_levels=None):
        """on_report: called with no arguments by the "Report" button (the
        owning tab fetches in a thread and opens AssetsReportDialog); the
        button is hidden when it is None. kospi_levels: the KOSPI close of
        each week, shown next to its return in the hover box (2026-11-17);
        a week without one (None / 0) shows the return only."""
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setWindowTitle(tr("총자산 — 누적 수익률", "Total Assets — Cumulative Return"))
        fit_to_screen(self, 900, 600)
        self.showMaximized()

        self._dates = list(dates)
        self._x = np.asarray(mdates.date2num([pd.to_datetime(d) for d in self._dates]), dtype=float)
        self._kospi = list(kospi_returns)
        self._kospi_levels = list(kospi_levels) if kospi_levels is not None else [None] * len(self._kospi)
        self._asset = list(asset_returns)
        self._usd = list(usd_asset_returns)
        self._totals = list(totals)
        self._usd_totals = list(usd_totals)

        layout = QVBoxLayout(self)

        self.fig = Figure(figsize=(9, 6), constrained_layout=True)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)
        layout.addWidget(self.canvas, 1)

        self._hover_markers = []       # one enlarged marker per series, on the hovered week
        self._tooltip = None           # the AnnotationBbox with the week's three values
        self.hovered = None            # (series index, week index) of the marker under the mouse, or None
        if len(self._x):
            self._plot_series()
            self._shade_gap()
            self._mark_peak()
            self._end_labels()
            self._add_hover_artists()
            self.canvas.mpl_connect("motion_notify_event", self._on_motion)
            self.canvas.mpl_connect("axes_leave_event", self._on_leave)
            self.canvas.mpl_connect("figure_leave_event", self._on_leave)

        self._build_buttons(layout, on_report)

    # ------------------------------------------------------------------ chart
    def _plot_series(self) -> None:
        """The three return lines, the zero line, month ticks, legend and titles."""
        ax, x = self.ax, self._x
        # Series identity, not price direction: ink for the main series,
        # accent for its USD view, muted dashed for the benchmark (red/blue
        # stay reserved for gain/loss, docs/ui.md 1.1).
        # A marker on every weekly record (user direction 2026-11-16): filled
        # for Total Assets, hollow for its USD view, small for KOSPI, so the
        # three series stay apart where their lines cross.
        ax.plot(x, self._kospi, color=TEXT_MUTED, linewidth=1.6, linestyle="--", label=_LBL_KOSPI,
                marker="o", markersize=_POINT_SIZE - 1.5)
        ax.plot(x, self._asset, color=TEXT, linewidth=2.2, label=_LBL_ASSETS,
                marker="o", markersize=_POINT_SIZE)
        ax.plot(x, self._usd, color=ACCENT, linewidth=2, label=_mpl(_LBL_ASSETS_USD),
                marker="o", markersize=_POINT_SIZE, markerfacecolor=SURFACE, markeredgewidth=1.4)
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
    def _series(self):
        """(name, values, color, marker face) of the three lines, in hit-test
        priority order: Total Assets first where points coincide."""
        return ((_LBL_ASSETS, self._asset, TEXT, TEXT),
                (_LBL_ASSETS_USD, self._usd, ACCENT, SURFACE),
                (_LBL_KOSPI, self._kospi, TEXT_MUTED, TEXT_MUTED))

    def _add_hover_artists(self) -> None:
        """The three enlarged markers; the value box is built on each hover."""
        for _name, values, color, face in self._series():
            (m,) = self.ax.plot([self._x[-1]], [values[-1]], "o", markersize=_HOVER_SIZE, color=color,
                                markerfacecolor=face, markeredgewidth=2, zorder=8, visible=False)
            self._hover_markers.append(m)

    def point_at(self, px: float, py: float):
        """(series index, week index) of the marker within _HIT_PX pixels of
        the display point (px, py), the nearest one; None when there is none."""
        best, best_d = None, _HIT_PX
        for s_idx, (_name, values, _c, _f) in enumerate(self._series()):
            pts = self.ax.transData.transform(np.column_stack([self._x, np.asarray(values, float)]))
            d = np.hypot(pts[:, 0] - px, pts[:, 1] - py)
            i = int(np.argmin(d))
            if d[i] < best_d - 1e-9:
                best, best_d = (s_idx, i), d[i]
        return best

    def tooltip_lines(self, i: int) -> list[tuple[str, str]]:
        """(text, color) lines of week `i`: the date, then Total Assets,
        Total Assets ($) and KOSPI with return and amount (KOSPI: its level)."""
        level = self._kospi_levels[i] if i < len(self._kospi_levels) else None
        kospi = f"{_LBL_KOSPI}  {self._kospi[i]:+.1f}%"
        if level:
            kospi += f" · {level:,.2f}"
        return [
            (self._dates[i], TEXT_MUTED),
            (f"{_LBL_ASSETS}  {self._asset[i]:+.1f}% · {fmt_krw(self._totals[i])}", TEXT),
            (f"{_LBL_ASSETS_USD}  {self._usd[i]:+.1f}% · ${self._usd_totals[i]:,.0f}", ACCENT),
            (kospi, TEXT_MUTED),
        ]

    def _hide_hover(self) -> None:
        for m in self._hover_markers:
            m.set_visible(False)
        if self._tooltip is not None:
            self._tooltip.remove()
            self._tooltip = None

    def _on_motion(self, event) -> None:
        hit = None
        if event.inaxes is self.ax and event.x is not None:
            hit = self.point_at(event.x, event.y)
        if hit == self.hovered:
            return
        self.hovered = hit
        self._hide_hover()
        if hit is None:
            self.canvas.draw_idle()
            return
        s_idx, i = hit
        for m, (_name, values, _c, _f) in zip(self._hover_markers, self._series()):
            m.set_data([self._x[i]], [values[i]])
            m.set_visible(True)
        x, y = self._x[i], self._series()[s_idx][1][i]
        # The box goes toward the middle of the chart so it stays inside it.
        (px, py), ((x0, y0), (x1, y1)) = self.ax.transData.transform((x, y)), self.ax.bbox.get_points()
        right, upper = px > (x0 + x1) / 2, py > (y0 + y1) / 2
        self.tooltip_align = (1.0 if right else 0.0, 1.0 if upper else 0.0)
        box = VPacker(children=[TextArea(_mpl(text), textprops={"color": color, "fontsize": 9,
                                                                 "fontweight": "bold" if n == 1 else "normal"})
                                for n, (text, color) in enumerate(self.tooltip_lines(i))],
                      align="left", pad=0, sep=3)
        self._tooltip = AnnotationBbox(
            box, (x, y), xybox=(-16 if right else 16, -16 if upper else 16), boxcoords="offset points",
            box_alignment=self.tooltip_align, frameon=True, zorder=9,
            bboxprops={"boxstyle": "round,pad=0.5", "fc": HDR_BG, "ec": self._series()[s_idx][2], "alpha": 0.95})
        self.ax.add_artist(self._tooltip)
        self.canvas.draw_idle()

    def _on_leave(self, _event) -> None:
        if self.hovered is not None:
            self.hovered = None
            self._hide_hover()
            self.canvas.draw_idle()

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
