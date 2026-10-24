"""ui/dialogs/stock_ma.py — StockMaDialog — per-stock price/MA chart with
volume, MA-divergence and RSI panels and an investor-trend (or futures-curve)
side table.

Split out of the former single ui/dialogs.py (2026-09-17); restructured into
build/plot/interaction methods on 2026-09-19; redesigned the same day per the
docs/ui.md "MA Chart Redesign" mockup (12 issues, phases A-D):

  A  colors: volume/RSI use the app-wide PROFIT/LOSS rule (they were the
     only US-style green-up/red-down in the app); the four MAs are one accent
     ramp (ui.colors.MA_RAMP, darkest = shortest window) instead of five hues;
     Close is neutral ink.
  B  no legend: each line carries a value label at its right edge
     (_place_end_labels, pushed apart to avoid overlap); no per-point markers.
  C  no twin axes: RSI is its own panel, the duplicate Div lines on the price
     axis are gone (the divergence panel is the one place), Equal Weight is
     rebased onto the price axis; divergence bands are three (overheated /
     neutral / depressed) around ui.colors.MA_DIV_NEUTRAL_PCT, the same
     constant the Universe table reads, with 100 centred.
  D  period buttons (1M..3Y/All); a crosshair shared by every panel.

2026-10-15 (user direction): the exclusive view segment (Short / Long /
Close only / Divergence / Equal Weight) became independent, multi-select
series toggles -- Close, MA5, MA10, MA20, MA50, Divergence [, Equal Weight].
Divergence shows/hides the divergence panel (the separate panel button is
gone); with no price line selected the price panel is hidden too. The
crosshair carries its values on the chart: a dot + value badge on every
visible series and a date badge on the bottom axis; the readout row above
the chart was removed.

Layout
------
::

    +-- splitter ------------------------------------------------------+
    | name  ticker · market  price  chg%   [1M 3M 6M 1Y 3Y All] as-of  |
    | Series [Close MA5 MA10 MA20 MA50 Divergence EW]     [Volume] [RSI] |
    |   FigureCanvas: price (+MA ramp)      | right panel (optional)   |
    |                 volume                |   investor / futures tbl |
    |                 divergence            |   "Company Info ↗"        |
    |                 rsi                   |                          |
    |   horizontal scrollbar                |                          |
    +------------------------------------------------------------------+
    [Close]

Simple charts (bond yields, VIX, WTI) keep only the price panel and the
period buttons; bonds also get the Y-axis +/- buttons.
"""
import logging

import numpy as np
import pandas as pd
import polars as pl
import matplotlib.dates as mdates
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.collections import PolyCollection
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QScrollBar, QButtonGroup,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, QWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics

from ui.common import create_font, BTN_H, FONT_TITLE, FONT_BODY, FONT_SMALL, FONT_CAPTION
from ui.colors import PROFIT, LOSS, MA_RAMP, MA_DIV_NEUTRAL_PCT
from ui.theme import ACCENT, TEXT, TEXT_MUTED, TEXT_FAINT, LINE, SURFACE

logger = logging.getLogger(__name__)

_STOCK_MARKETS = ("KOSPI", "KOSDAQ", "NASDAQ 100", "S&P500")
_SIMPLE_CHART_TICKERS = ("CL=F", "^VIX", "VKOSPI")
_SCROLL_MAX = 10000
_TICK_PT = FONT_SMALL          # issue #12: nothing on the chart below 9pt

# Segment buttons share one look: a checkable neutral button; the checked one
# takes the theme's QPushButton:checked accent.
_SEG_H = BTN_H

_PERIODS = [("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12), ("3Y", 36), ("All", None)]
_DEFAULT_PERIOD = "1Y"
# Independent series toggles (user direction 2026-10-15), key -> label.
# Price-panel lines use their _lines key; "Div" is the divergence panel.
_SERIES = {
    "Close": "Close", "MA5": "MA5", "MA10": "MA10", "MA20": "MA20", "MA50": "MA50",
    "Div": "Divergence", "EW": "Equal Weight",
}
_PRICE_SERIES = ("Close", "MA5", "MA10", "MA20", "MA50", "EW")
# What the dialog opens with: close price plus the divergence panel, no moving
# averages (user direction 2026-09-29: close only on the price panel).
_DEFAULT_SERIES = frozenset({"Close", "Div"})


def _make_bars(x, y_bottom, y_top, w):
    """Vectorised (N, 4, 2) rectangle polygons for volume bars; NaN rows dropped."""
    x = np.asarray(x, dtype=float)
    y_bottom = np.asarray(y_bottom, dtype=float)
    y_top = np.asarray(y_top, dtype=float)
    valid = ~(np.isnan(y_bottom) | np.isnan(y_top))
    x, y_bottom, y_top = x[valid], y_bottom[valid], y_top[valid]
    if len(x) == 0:
        return []
    w2 = w / 2
    polys = np.empty((len(x), 4, 2))
    polys[:, 0] = np.stack([x - w2, y_bottom], axis=1)
    polys[:, 1] = np.stack([x - w2, y_top], axis=1)
    polys[:, 2] = np.stack([x + w2, y_top], axis=1)
    polys[:, 3] = np.stack([x + w2, y_bottom], axis=1)
    return polys


def _compact(v: float) -> str:
    """1,849,000 -> '1.85M', 71,200 -> '71.2k' -- for the narrow end labels."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:,.2f}" if a < 100 else f"{v:,.0f}"


def _segment_button(text: str, group: QButtonGroup = None, checked: bool = False, width: int = None) -> QPushButton:
    btn = QPushButton(text)
    btn.setCheckable(True)
    btn.setChecked(checked)
    btn.setFixedHeight(_SEG_H)
    btn.setFont(create_font(FONT_SMALL, style_name="Semilight"))
    if width:
        btn.setFixedWidth(width)
    if group is not None:
        group.addButton(btn)
    return btn


class StockMaDialog(QDialog):
    """Price + moving averages for one instrument, with volume, divergence and
    RSI panels (docs/ui.md MA Chart Redesign)."""

    def __init__(self, ticker, name, market, df, investor_data=None, parent=None, change_mode='pct'):
        super().__init__(parent)
        self._ticker = ticker
        self._name = name
        self._market = market
        self._df = df
        self._investor_data = investor_data or []
        self._change_mode = change_mode

        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setWindowTitle(f"{name} ({ticker}) — Moving Averages")
        self.resize(1200, 720)
        self.showMaximized()

        self._is_simple_chart = ticker in _SIMPLE_CHART_TICKERS or change_mode == 'bp'
        self._has_data = (
            df is not None and not df.is_empty() and "Close" in df.columns
            and (self._is_simple_chart or all(c in df.columns for c in MA_RAMP))
        )
        self._currency, self._unit_suffix, self._fmt_str = self._format_config()

        # Artists / state the interaction handlers refer to.
        self._lines = {}          # "Close" | "MA5" | ... | "EW" -> Line2D
        self._end_labels = {}     # same keys -> Annotation
        self._crosshairs = []     # one axvline per panel
        self._hover = {}          # series key -> (marker Line2D, Annotation) at the cursor
        self._date_badges = {}    # panel key -> Annotation with the cursor date
        self._panel_axes = {}     # "price" | "vol" | "div" | "rsi" -> Axes
        self._panel_on = {"vol": False, "div": False, "rsi": False}
        self._series_on = {k: (k in _DEFAULT_SERIES) for k in _PRICE_SERIES}
        self._x = None
        self._pan_start = None
        self._cursor_idx = None
        self.scrollbar = None

        root = QVBoxLayout(self)
        root.setSpacing(6)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        chart_widget = QWidget()
        chart_v = QVBoxLayout(chart_widget)
        chart_v.setContentsMargins(0, 0, 0, 0)
        chart_v.setSpacing(4)
        splitter.addWidget(chart_widget)

        is_stock = market in _STOCK_MARKETS
        if self._investor_data or is_stock:
            panel = self._build_right_panel(is_stock)
            splitter.addWidget(panel)
            splitter.setStretchFactor(0, 10)
            splitter.setStretchFactor(1, 0)          # the panel keeps its content width; the chart flexes
            splitter.setCollapsible(1, False)

        chart_v.addLayout(self._build_header_row())
        chart_v.addLayout(self._build_view_row())

        self._fig = Figure(figsize=(9.5, 7.5))
        self._canvas = FigureCanvas(self._fig)
        if self._has_data:
            self._plot()
        else:
            ax = self._fig.add_subplot(111)
            ax.text(0.5, 0.5, "Failed to load detailed data.", ha="center", va="center",
                    transform=ax.transAxes, color=TEXT_MUTED, fontsize=FONT_BODY)
            ax.set_axis_off()

        if change_mode == 'bp':
            chart_v.addLayout(self._build_y_zoom_row())
        chart_v.addWidget(self._canvas, 1)
        if self.scrollbar is not None:
            chart_v.addWidget(self.scrollbar)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    # ------------------------------------------------------------- top rows
    def _build_header_row(self) -> QHBoxLayout:
        """Name · ticker/market · last price · change% | period segment | as-of."""
        row = QHBoxLayout()
        row.setSpacing(10)

        name_lbl = QLabel(self._name)
        name_lbl.setFont(create_font(FONT_TITLE - 2, QFont.Weight.Bold))
        row.addWidget(name_lbl)

        meta_lbl = QLabel(f"{self._ticker} · {self._market}")
        meta_lbl.setFont(create_font(FONT_SMALL, style_name="Semilight"))
        meta_lbl.setStyleSheet(f"color:{TEXT_FAINT};")
        row.addWidget(meta_lbl)

        last, chg = self._last_close_and_change()
        self._price_lbl = QLabel(self._fmt_price(last) if last is not None else "—")
        self._price_lbl.setFont(create_font(FONT_TITLE - 2, QFont.Weight.Bold))
        row.addWidget(self._price_lbl)
        self._chg_lbl = QLabel(self._fmt_change(chg) if chg is not None else "")
        self._chg_lbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
        if chg is not None:
            self._chg_lbl.setStyleSheet(f"color:{PROFIT if chg > 0 else LOSS if chg < 0 else TEXT_MUTED};")
        row.addWidget(self._chg_lbl)
        row.addStretch()

        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        self._period_buttons = {}
        for label, months in _PERIODS:
            btn = _segment_button(label, self._period_group, checked=(label == _DEFAULT_PERIOD), width=44)
            btn.clicked.connect(lambda _c, m=months, lbl=label: self._on_period_clicked(lbl, m))
            row.addWidget(btn)
            self._period_buttons[label] = btn

        as_of = ""
        if self._has_data:
            as_of = f"as of {pd.to_datetime(self._df.get_column('Date')[-1]).strftime('%Y-%m-%d')}"
        as_of_lbl = QLabel(as_of)
        as_of_lbl.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        as_of_lbl.setStyleSheet(f"color:{TEXT_FAINT};")
        row.addSpacing(6)
        row.addWidget(as_of_lbl)
        return row

    def _build_view_row(self) -> QHBoxLayout:
        """SHOW series toggles (independent, multi-select) | panel toggles."""
        row = QHBoxLayout()
        row.setSpacing(6)
        self._series_buttons = {}
        keys = [] if self._is_simple_chart else ["Close", *MA_RAMP.keys(), "Div"]
        if self._has_data and "EqualWeight" in self._df.columns and not self._is_simple_chart:
            keys.append("EW")
        if keys:
            cap = QLabel("Series")
            cap.setFont(create_font(FONT_CAPTION, QFont.Weight.Bold))
            cap.setStyleSheet(f"color:{TEXT_FAINT};")
            row.addWidget(cap)
        for key in keys:
            btn = _segment_button(_SERIES[key], checked=(key in _DEFAULT_SERIES))
            btn.clicked.connect(lambda checked, k=key: self._set_series(k, checked))
            row.addWidget(btn)
            self._series_buttons[key] = btn
        row.addStretch()

        # The divergence panel has no button here: the Divergence series
        # toggle drives it.
        self._panel_buttons = {}
        for key, label in (("vol", "Volume"), ("rsi", "RSI")):
            btn = _segment_button(label)
            btn.clicked.connect(lambda checked, k=key: self._set_panel(k, checked))
            row.addWidget(btn)
            self._panel_buttons[key] = btn
        return row

    def _build_right_panel(self, is_stock: bool) -> QWidget:
        """Investor-trend table (KR stocks), futures curve (WTI) and the
        'Company Info' link button."""
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)

        inv = self._investor_data
        is_wti_futures = bool(inv) and self._ticker == "CL=F" and 'Contract' in inv[0]
        if is_wti_futures:
            v.addWidget(self._section_label("Futures Price (Last 8 Months)"))
            v.addWidget(self._build_futures_table(inv))
        elif inv:
            v.addWidget(self._section_label(f"Investor Net Buying (Last {len(inv)} Days)"))
            v.addWidget(self._build_investor_table(inv))

        if is_stock:
            fin_btn = QPushButton("Company Info ↗")
            fin_btn.clicked.connect(self._open_company_page)
            v.addSpacing(10)
            v.addWidget(fin_btn)

        if not inv:
            v.addStretch()
        return panel

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lbl

    @staticmethod
    def _new_side_table(headers: list) -> QTableWidget:
        table = QTableWidget()
        table.setFont(create_font(FONT_SMALL, style_name="Semilight"))
        table.horizontalHeader().setFont(create_font(FONT_SMALL, QFont.Weight.Bold))
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        return table

    @staticmethod
    def _cell(text: str, align=Qt.AlignmentFlag.AlignCenter, color: str = None) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setTextAlignment(align)
        if color:
            it.setForeground(QColor(color))
        return it

    # Horizontal room a cell needs beyond its text: ui/theme.py's
    # QTableWidget::item padding (8px a side), the style's own text margin
    # (~3px a side), plus slack for hinting/DPI differences between the
    # metrics measured here and the glyphs painted later. 22px was exactly
    # the sum with no slack, and a 7-digit close (1,849,000) still elided
    # on a real screen.
    _SIDE_CELL_PAD = 36

    @classmethod
    def _fit_side_table(cls, table: QTableWidget) -> None:
        """Size every column to its widest header/cell text and pin the
        table's minimum width to the total, so the splitter cannot squeeze
        the side panel below what its numbers need. Net-purchase figures
        are share counts (7-8 digits, e.g. -1,234,567) -- under the old
        Stretch mode a narrow panel elided them to '1,23...'."""
        # Measure with the fonts the widget will actually paint with: the
        # global stylesheet re-resolves them at polish time.
        table.ensurePolished()
        table.horizontalHeader().ensurePolished()
        head_fm = QFontMetrics(table.horizontalHeader().font())
        cell_fm = QFontMetrics(table.font())
        total = 0
        for c in range(table.columnCount()):
            header = table.horizontalHeaderItem(c)
            w = head_fm.horizontalAdvance(header.text()) if header else 0
            for r in range(table.rowCount()):
                it = table.item(r, c)
                if it is not None:
                    w = max(w, cell_fm.horizontalAdvance(it.text()))
            w += cls._SIDE_CELL_PAD
            table.setColumnWidth(c, w)
            total += w
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        scrollbar_w = table.style().pixelMetric(table.style().PixelMetric.PM_ScrollBarExtent)
        table.setMinimumWidth(total + scrollbar_w + 2 * table.frameWidth())

    def _build_futures_table(self, rows: list) -> QTableWidget:
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        table = self._new_side_table(["Name", "Code", "Price"])
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            table.setItem(i, 0, self._cell(row['Contract']))
            table.setItem(i, 1, self._cell(row['Symbol']))
            table.setItem(i, 2, self._cell(f"${row.get('Close', 0):,.2f}", right))
        self._fit_side_table(table)
        return table

    def _build_investor_table(self, rows: list) -> QTableWidget:
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        has_details = any(r.get('InvestmentTrust', 0) != 0 or r.get('PrivateEquity', 0) != 0 for r in rows)
        headers = ["Date", "Close", "Foreigner", "Institution", "Retail"]
        keys = ["Foreigner", "Institution", "Retail"]
        if has_details:
            headers.extend(["Inv. Trust", "Private Eq."])
            keys.extend(["InvestmentTrust", "PrivateEquity"])

        table = self._new_side_table(headers)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            table.setItem(i, 0, self._cell(row['Date']))
            table.setItem(i, 1, self._cell(f"{row.get('Close', 0):,}", right))
            for j, key in enumerate(keys, start=2):
                val = row.get(key, 0)
                color = PROFIT if val > 0 else (LOSS if val < 0 else None)
                table.setItem(i, j, self._cell(f"{val:,}", right, color))
        self._fit_side_table(table)
        return table

    def _open_company_page(self, *_):
        import webbrowser
        if self._market in ("KOSPI", "KOSDAQ"):
            url = f"https://finance.naver.com/item/main.naver?code={str(self._ticker).zfill(6)}"
        else:
            url = f"https://www.google.com/finance?q={str(self._ticker).replace('.', '-')}"
        webbrowser.open(url)

    def _build_y_zoom_row(self) -> QHBoxLayout:
        """'Zoom Y +/−' buttons (bond yields only; the X range is handled by scroll-zoom)."""
        row = QHBoxLayout()
        zoom_in_btn = QPushButton("Zoom Y +")
        zoom_out_btn = QPushButton("Zoom Y −")
        zoom_in_btn.setFixedSize(70, _SEG_H)
        zoom_out_btn.setFixedSize(70, _SEG_H)
        row.addStretch()
        row.addWidget(zoom_out_btn)
        row.addWidget(zoom_in_btn)
        row.addSpacing(20)
        zoom_in_btn.clicked.connect(self._zoom_y_in)
        zoom_out_btn.clicked.connect(self._zoom_y_out)
        return row

    # ------------------------------------------------------------ formatting
    def _format_config(self) -> tuple:
        """(currency prefix, unit suffix, number format) for prices."""
        if self._change_mode == 'bp':                 # bond yield in %
            return "", "%", "{:.2f}"
        if self._change_mode == 'abs' and self._market == 'Index':   # VIX or WTI
            return ("$" if self._ticker == 'CL=F' else ""), "", "{:,.2f}"
        if self._market == "Index":
            return "", "", "{:,.0f}"
        if self._market in ("KOSPI", "KOSDAQ"):
            return "", "", "{:,.0f}"
        return "$", "", "{:,.2f}"

    def _fmt_price(self, v: float) -> str:
        return f"{self._currency}{self._fmt_str.format(v)}{self._unit_suffix}"

    def _fmt_change(self, chg: float) -> str:
        if self._change_mode == 'bp':
            return f"{chg * 100:+.0f}bp"
        return f"{chg:+.2f}%"

    def _last_close_and_change(self):
        """(last close, change vs the previous close) -- % for stocks, the raw
        yield difference for bonds (formatted as bp by _fmt_change)."""
        if not self._has_data:
            return None, None
        closes = self._df.get_column("Close").drop_nulls()
        if len(closes) == 0:
            return None, None
        last = float(closes[-1])
        if len(closes) < 2:
            return last, None
        prev = float(closes[-2])
        if self._change_mode == 'bp':
            return last, last - prev
        return last, ((last - prev) / prev * 100.0) if prev else None

    def _col(self, name: str):
        return self._df.get_column(name).to_numpy() if name in self._df.columns else None

    # -------------------------------------------------------------- plotting
    def _plot(self) -> None:
        df = self._df
        dates = df.get_column("Date").to_numpy()
        self._x = mdates.date2num(dates)
        self._dates = dates

        has_vol = "Volume" in df.columns and float(df.get_column("Volume").max() or 0) > 0
        has_div = not self._is_simple_chart and ("MA20_Div" in df.columns or "MA50_Div" in df.columns)
        has_rsi = "RSI14" in df.columns

        gs = self._fig.add_gridspec(4, 1)
        ax_price = self._fig.add_subplot(gs[0])
        self._panel_axes["price"] = ax_price
        if has_vol:
            self._panel_axes["vol"] = self._fig.add_subplot(gs[1], sharex=ax_price)
        if has_div:
            self._panel_axes["div"] = self._fig.add_subplot(gs[2], sharex=ax_price)
        if has_rsi and not self._is_simple_chart:
            self._panel_axes["rsi"] = self._fig.add_subplot(gs[3], sharex=ax_price)

        self._plot_price_panel()
        if "vol" in self._panel_axes:
            self._plot_volume_panel()
        if "div" in self._panel_axes:
            self._plot_divergence_panel()
        if "rsi" in self._panel_axes:
            self._plot_rsi_panel()

        for key in ("vol", "div", "rsi"):
            available = key in self._panel_axes
            default_on = available and (key == "vol" or (key == "div" and "Div" in _DEFAULT_SERIES))
            self._panel_on[key] = default_on
            btn = self._panel_buttons.get(key) or self._series_buttons.get("Div" if key == "div" else "")
            if btn is not None:
                btn.setVisible(available)
                btn.setChecked(default_on)

        for ax in self._panel_axes.values():
            ax.tick_params(axis="both", labelsize=_TICK_PT, colors=TEXT_MUTED, length=3)
            ax.grid(True, linestyle=":", color=LINE, alpha=0.9)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                ax.spines[spine].set_color(LINE)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%y.%m"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=9))
            self._crosshairs.append(ax.axvline(self._x[-1], color=ACCENT, linewidth=1, linestyle=(0, (3, 3)),
                                               visible=False, zorder=6))

        self._install_pan_zoom()
        self._install_crosshair()
        if self._is_simple_chart:
            self._series_on = {k: (k == "Close") for k in _PRICE_SERIES}
        self._apply_series_visibility()
        self._apply_layout()
        self._apply_period(_DEFAULT_PERIOD)

    def _plot_price_panel(self) -> None:
        """Close (ink) + MA ramp; Equal Weight rebased onto the same axis."""
        ax = self._panel_axes["price"]
        x = self._x
        self._lines["Close"], = ax.plot(x, self._col("Close"), color=TEXT, linewidth=1.7, label="Close", zorder=5)
        if not self._is_simple_chart:
            for key, color in MA_RAMP.items():
                arr = self._col(key)
                if arr is not None:
                    self._lines[key], = ax.plot(x, arr, color=color, linewidth=1.3, label=key, zorder=4)
        ew = self._col("EqualWeight")
        if ew is not None and not self._is_simple_chart:
            close = self._col("Close")
            both = ~(np.isnan(ew) | np.isnan(close))
            if both.any():
                first = int(np.argmax(both))
                rebased = ew * (close[first] / ew[first])
                self._lines["EW"], = ax.plot(x, rebased, color=TEXT_MUTED, linewidth=1.2, linestyle="--",
                                             label="EW", zorder=3, visible=False)
        ax.set_ylabel(f"Price{(' (' + self._unit_suffix + ')') if self._unit_suffix else ''}",
                      fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.yaxis.set_major_formatter(lambda v, _pos: self._fmt_str.format(v))
        ax.tick_params(labelbottom=False)

    def _plot_volume_panel(self) -> None:
        """Up/down volume bars in PROFIT/LOSS (issue #1)."""
        ax = self._panel_axes["vol"]
        df = self._df
        up = df.filter(pl.col("Close") >= pl.col("Open"))
        down = df.filter(pl.col("Close") < pl.col("Open"))
        width = 0.62
        for part, color, alpha in ((up, PROFIT, 0.55), (down, LOSS, 0.5)):
            if part.height:
                xs = mdates.date2num(part.get_column("Date").to_numpy())
                ax.add_collection(PolyCollection(
                    _make_bars(xs, [0] * part.height, part.get_column("Volume").to_numpy(), width),
                    facecolors=color, alpha=alpha, linewidths=0))
        ax.set_ylabel("Volume", fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.yaxis.set_major_formatter(lambda v, _pos: _compact(v) if v else "0")
        ax.tick_params(labelbottom=False)

    def _plot_divergence_panel(self) -> None:
        """Div(20)/Div(50) = price / MA x 100 over three bands centred on 100
        (issue #6): above 100+MA_DIV_NEUTRAL_PCT overheated (PROFIT tint),
        below 100-MA_DIV_NEUTRAL_PCT depressed (LOSS tint), neutral between."""
        ax = self._panel_axes["div"]
        n = MA_DIV_NEUTRAL_PCT
        self._div_bands = [
            ax.axhspan(100 + n, 10_000, facecolor=PROFIT, alpha=0.09, linewidth=0),
            ax.axhspan(100 - n, 100 + n, facecolor=TEXT_FAINT, alpha=0.07, linewidth=0),
            ax.axhspan(-10_000, 100 - n, facecolor=LOSS, alpha=0.08, linewidth=0),
        ]
        ax.axhline(100, color=TEXT_FAINT, linewidth=1, linestyle=(0, (4, 3)))
        if "MA50_Div" in self._df.columns:
            self._lines["Div(50)"], = ax.plot(self._x, self._col("MA50_Div"), color=MA_RAMP["MA20"], linewidth=1.4)
        if "MA20_Div" in self._df.columns:
            self._lines["Div(20)"], = ax.plot(self._x, self._col("MA20_Div"), color=MA_RAMP["MA5"], linewidth=1.4)
        ax.set_ylabel("Div %", fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.tick_params(labelbottom=False)

    def _plot_rsi_panel(self) -> None:
        """RSI14 in its own panel (issue #4); overbought = PROFIT, oversold = LOSS."""
        ax = self._panel_axes["rsi"]
        rsi = self._col("RSI14")
        self._lines["RSI"], = ax.plot(self._x, rsi, color=ACCENT, linewidth=1.3)
        ax.axhline(70, color=PROFIT, linestyle=":", alpha=0.6, linewidth=1)
        ax.axhline(30, color=LOSS, linestyle=":", alpha=0.6, linewidth=1)
        ax.fill_between(self._x, 70, 100, where=(rsi >= 70), facecolor=PROFIT, alpha=0.1, linewidth=0)
        ax.fill_between(self._x, 0, 30, where=(rsi <= 30), facecolor=LOSS, alpha=0.1, linewidth=0)
        ax.set_ylim(0, 100)
        ax.set_yticks([30, 50, 70])
        ax.set_ylabel("RSI 14", fontsize=_TICK_PT, color=TEXT_MUTED)
        ax.tick_params(labelbottom=False)

    # --------------------------------------------------------- layout / view
    def _visible_panels(self) -> list:
        """[(panel, height ratio)]; the price panel drops out when no price
        line is selected and another panel is on."""
        others = [k for k in ("vol", "div", "rsi") if self._panel_on[k] and k in self._panel_axes]
        order = [("price", 3)] if (self._price_series_visible() or not others) else []
        for key in others:
            # Divergence is enlarged when it replaces the price panel.
            order.append((key, 3 if key == "div" and not order else 1))
        return order

    def _apply_layout(self) -> None:
        """Re-grid the visible panels (issue #11: the ratio is no longer a
        fixed 3:1:1 -- panels can be hidden, the price panel too when no
        price line is selected)."""
        panels = self._visible_panels()
        gs = self._fig.add_gridspec(len(panels), 1, height_ratios=[r for _, r in panels],
                                    hspace=0.08, left=0.075, right=0.90, top=0.97, bottom=0.07)
        shown = set()
        for i, (key, _r) in enumerate(panels):
            ax = self._panel_axes[key]
            ax.set_subplotspec(gs[i])
            ax.set_visible(True)
            ax.tick_params(labelbottom=(i == len(panels) - 1))
            shown.add(key)
        for key, ax in self._panel_axes.items():
            if key not in shown:
                ax.set_visible(False)

    def _set_panel(self, key: str, on: bool) -> None:
        self._panel_on[key] = bool(on)
        self._refresh_after_toggle()

    def _set_series(self, key: str, on: bool) -> None:
        """Toggle one series independently of the others (user direction
        2026-10-15): a price-panel line, or "Div" for the divergence panel."""
        btn = self._series_buttons.get(key)
        if btn is not None and btn.isChecked() != bool(on):
            btn.setChecked(bool(on))
        if key == "Div":
            self._panel_on["div"] = bool(on)
        else:
            self._series_on[key] = bool(on)
            self._apply_series_visibility()
        self._refresh_after_toggle()

    def _apply_series_visibility(self) -> None:
        for name, line in self._lines.items():
            if name in self._series_on:
                line.set_visible(self._series_on[name])

    def _refresh_after_toggle(self) -> None:
        if self._x is None:
            return
        self._apply_layout()
        self._autofit_y()
        self._place_end_labels()
        self._update_hover(self._cursor_idx)
        self._canvas.draw_idle()

    # ------------------------------------------------------------ end labels
    def _price_series_visible(self) -> list:
        """[(key, Line2D)] of the price-panel lines currently shown, Close first."""
        out = []
        for key in ("Close", *MA_RAMP.keys(), "EW"):
            line = self._lines.get(key)
            if line is not None and line.get_visible():
                out.append((key, line))
        return out

    def _place_end_labels(self) -> None:
        """A value label at each visible price-panel line's right edge (issue
        #3, replacing the legend), pushed apart vertically so they never
        overlap (same rule as the mockup: keep anchor order, min pitch)."""
        ax = self._panel_axes["price"]
        for ann in self._end_labels.values():
            ann.remove()
        self._end_labels.clear()

        x_right = ax.get_xlim()[1]
        idx = int(np.searchsorted(self._x, x_right, side="right") - 1)
        idx = max(0, min(len(self._x) - 1, idx))
        entries = []
        if not ax.get_visible():
            return
        for key, line in self._price_series_visible():
            y = line.get_ydata()[idx]
            if y is None or not np.isfinite(y):
                continue
            if key == "Close":
                text, bg, fg = self._fmt_str.format(y), TEXT, SURFACE
            elif key == "EW":
                text, bg, fg = f"EW {_compact(y)}", TEXT_MUTED, SURFACE
            else:
                bg = MA_RAMP[key]
                text, fg = f"{key} {_compact(y)}", (SURFACE if key in ("MA5", "MA10") else TEXT)
            entries.append((key, float(y), text, bg, fg))
        if not entries:
            return

        # Collision push in display pixels, then back to an offset in points.
        dpi = self._fig.dpi
        pitch_px = 1.6 * _TICK_PT * dpi / 72
        anchored = sorted(((ax.transData.transform((x_right, y))[1], key, y, text, bg, fg)
                           for key, y, text, bg, fg in entries), key=lambda t: t[0])
        placed = []
        for disp_y, key, y, text, bg, fg in anchored:
            if placed and disp_y - placed[-1][0] < pitch_px:
                disp_y = placed[-1][0] + pitch_px
            placed.append([disp_y, key, y, text, bg, fg])
        top_px = ax.transAxes.transform((0, 1))[1]
        overflow = placed[-1][0] - top_px + pitch_px / 2
        if overflow > 0:
            for p in placed:
                p[0] -= overflow

        for disp_y, key, y, text, bg, fg in placed:
            anchor_px = ax.transData.transform((x_right, y))[1]
            dy_pt = (disp_y - anchor_px) * 72 / dpi
            self._end_labels[key] = ax.annotate(
                text, xy=(x_right, y), xytext=(5, dy_pt), textcoords="offset points",
                fontsize=_TICK_PT, color=fg, ha="left", va="center", annotation_clip=False, zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", facecolor=bg, edgecolor="none"),
            )

    # ----------------------------------------------------------- crosshair
    def _install_crosshair(self) -> None:
        canvas = self._canvas
        canvas.mpl_connect("motion_notify_event", self._on_motion)
        canvas.mpl_connect("axes_leave_event", self._on_leave)
        canvas.mpl_connect("figure_leave_event", self._on_leave)
        canvas.mpl_connect("resize_event", lambda _e: self._place_end_labels())

    def _index_at(self, xdata: float) -> int:
        i = int(np.searchsorted(self._x, xdata))
        if i <= 0:
            return 0
        if i >= len(self._x):
            return len(self._x) - 1
        return i if abs(self._x[i] - xdata) < abs(self._x[i - 1] - xdata) else i - 1

    def _set_cursor(self, idx) -> None:
        """Move the shared crosshair to bar `idx` (None hides it) together
        with its value and date badges."""
        self._cursor_idx = idx
        for line in self._crosshairs:
            if idx is None:
                line.set_visible(False)
            else:
                line.set_xdata([self._x[idx], self._x[idx]])
                line.set_visible(True)
        self._update_hover(idx)
        self._canvas.draw_idle()

    def _hover_text(self, key: str, v: float) -> str:
        if key in ("Div(20)", "Div(50)"):
            return f"{key} {v:.1f}"
        if key == "RSI":
            return f"RSI {v:.0f}"
        if key == "Vol":
            return f"Vol {_compact(v)}"
        prefix = "" if key == "Close" else f"{key} "
        return prefix + self._fmt_str.format(v)

    def _hover_series(self, idx: int) -> list:
        """[(key, axes, y, color)] for every visible series at bar `idx`."""
        out = []
        for key, line in self._lines.items():
            if not (line.get_visible() and line.axes.get_visible()):
                continue
            y = line.get_ydata()[idx]
            if y is not None and np.isfinite(y):
                out.append((key, line.axes, float(y), line.get_color()))
        ax_vol = self._panel_axes.get("vol")
        vol = self._col("Volume")
        if ax_vol is not None and ax_vol.get_visible() and vol is not None:
            v = float(vol[idx])
            if np.isfinite(v):
                opens = self._col("Open")
                up = opens is None or self._col("Close")[idx] >= opens[idx]
                out.append(("Vol", ax_vol, v, PROFIT if up else LOSS))
        return out

    def _update_hover(self, idx) -> None:
        """A dot on every visible series at the cursor bar with its value next
        to it (user direction 2026-10-15), pushed apart per panel like the end
        labels; flipped to the left of the crosshair near the right edge."""
        shown = set()
        if idx is not None and self._x is not None:
            x = self._x[idx]
            xmin, xmax = self._panel_axes["price"].get_xlim()
            left = x > xmin + (xmax - xmin) * 0.8
            dpi = self._fig.dpi
            pitch_px = 1.6 * _TICK_PT * dpi / 72
            by_ax = {}
            for key, ax, y, color in self._hover_series(idx):
                by_ax.setdefault(ax, []).append((ax.transData.transform((x, y))[1], key, y, color))
            for ax, entries in by_ax.items():
                entries.sort(key=lambda t: t[0])
                placed = []
                for disp_y, key, y, color in entries:
                    if placed and disp_y - placed[-1][0] < pitch_px:
                        disp_y = placed[-1][0] + pitch_px
                    placed.append((disp_y, key, y, color))
                for disp_y, key, y, color in placed:
                    marker, ann = self._hover_artist(key, ax)
                    marker.set_data([x], [y])
                    marker.set_markerfacecolor(color)
                    anchor_px = ax.transData.transform((x, y))[1]
                    ann.xy = (x, y)
                    ann.set_position((-8 if left else 8, (disp_y - anchor_px) * 72 / dpi))
                    ann.set_horizontalalignment("right" if left else "left")
                    ann.set_text(self._hover_text(key, y))
                    r, g, b = to_rgb(color)
                    ann.set_color(TEXT if 0.299 * r + 0.587 * g + 0.114 * b > 0.6 else SURFACE)
                    ann.get_bbox_patch().set_facecolor(color)
                    marker.set_visible(True)
                    ann.set_visible(True)
                    shown.add(key)
        for key, (marker, ann) in self._hover.items():
            if key not in shown:
                marker.set_visible(False)
                ann.set_visible(False)
        self._update_date_badge(idx)

    def _update_date_badge(self, idx) -> None:
        """The cursor bar's date on the x axis of the bottom visible panel
        (user direction 2026-10-15, replacing the readout row's date)."""
        bottom = None
        if idx is not None and self._x is not None:
            visible = [k for k, _r in self._visible_panels()]
            bottom = visible[-1] if visible else None
        for key, ann in self._date_badges.items():
            ann.set_visible(key == bottom)
        if bottom is None:
            return
        if bottom not in self._date_badges:
            self._date_badges[bottom] = self._panel_axes[bottom].annotate(
                "", xy=(0, 0), xycoords=("data", "axes fraction"), xytext=(0, -3), textcoords="offset points",
                fontsize=_TICK_PT, color=SURFACE, ha="center", va="top", annotation_clip=False, zorder=10,
                bbox=dict(boxstyle="round,pad=0.3", facecolor=ACCENT, edgecolor="none"))
        ann = self._date_badges[bottom]
        ann.xy = (self._x[idx], 0)
        ann.set_text(pd.to_datetime(self._dates[idx]).strftime("%Y-%m-%d"))
        ann.set_visible(True)

    def _hover_artist(self, key: str, ax) -> tuple:
        """The (marker, label) pair for `key`, created on first use."""
        if key not in self._hover:
            marker, = ax.plot([np.nan], [np.nan], marker="o", markersize=6, linestyle="none",
                              markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=8, visible=False)
            ann = ax.annotate("", xy=(0, 0), xytext=(8, 0), textcoords="offset points",
                              fontsize=_TICK_PT, va="center", annotation_clip=False, zorder=9, visible=False,
                              bbox=dict(boxstyle="round,pad=0.25", edgecolor="none", alpha=0.92))
            self._hover[key] = (marker, ann)
        return self._hover[key]

    def _on_leave(self, _event) -> None:
        if self._cursor_idx is not None:
            self._set_cursor(None)

    # ----------------------------------------------------------- pan / zoom
    def _install_pan_zoom(self) -> None:
        """Mouse-wheel zoom, left-drag pan and a horizontal scrollbar, all
        clamped to the data range (no right padding so future dates never appear)."""
        abs_min, abs_max = float(self._x[0]), float(self._x[-1])
        self._x_lo = abs_min - (abs_max - abs_min) * 0.02
        self._x_hi = abs_max

        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_change)

        canvas = self._canvas
        canvas.mpl_connect('scroll_event', self._on_scroll)
        canvas.mpl_connect('button_press_event', self._on_press)
        canvas.mpl_connect('button_release_event', self._on_release)

    def _chart_axes(self) -> list:
        return [ax for ax in self._panel_axes.values() if ax.get_visible()]

    def _clamp_xlim(self, new_xmin: float, new_xmax: float) -> tuple:
        w = new_xmax - new_xmin
        if w > (self._x_hi - self._x_lo):
            return self._x_lo, self._x_hi
        if new_xmin < self._x_lo:
            new_xmin = self._x_lo
            new_xmax = new_xmin + w
        if new_xmax > self._x_hi:
            new_xmax = self._x_hi
            new_xmin = new_xmax - w
        return new_xmin, new_xmax

    def _set_xlim(self, xmin: float, xmax: float) -> None:
        self._panel_axes["price"].set_xlim(*self._clamp_xlim(xmin, xmax))
        self._autofit_y()
        self._place_end_labels()
        self._update_scrollbar()
        self._canvas.draw_idle()

    def _visible_mask(self):
        xmin, xmax = self._panel_axes["price"].get_xlim()
        return (self._x >= xmin) & (self._x <= xmax)

    def _autofit_y(self) -> None:
        """Fit each panel's Y range to the bars in view (with the period
        buttons a 1M window on a 5Y Y range would be a flat line)."""
        mask = self._visible_mask()
        if not mask.any():
            return

        def span(arrays, pad):
            arrays = [np.asarray(a, dtype=float)[mask] for a in arrays if a is not None]
            if not arrays:
                return None
            vals = np.concatenate(arrays)
            vals = vals[np.isfinite(vals)]
            if len(vals) == 0:
                return None
            lo, hi = float(vals.min()), float(vals.max())
            margin = (hi - lo) * pad or abs(hi) * 0.01 or 1.0
            return lo - margin, hi + margin

        price_arrays = [line.get_ydata() for _k, line in self._price_series_visible()]
        rng = span(price_arrays, 0.04)
        if rng:
            self._panel_axes["price"].set_ylim(*rng)

        if "vol" in self._panel_axes:
            vol = self._col("Volume")
            if vol is not None:
                v = np.asarray(vol, dtype=float)[mask]
                v = v[np.isfinite(v)]
                if len(v) and v.max() > 0:
                    self._panel_axes["vol"].set_ylim(0, float(v.max()) * 1.1)

        if "div" in self._panel_axes:
            rng = span([self._col("MA20_Div"), self._col("MA50_Div")], 0.0)
            if rng:
                # Symmetric about 100 so the baseline sits mid-panel (issue #6).
                r = max(abs(rng[1] - 100), abs(100 - rng[0])) + MA_DIV_NEUTRAL_PCT
                self._panel_axes["div"].set_ylim(100 - r, 100 + r)

    def _update_scrollbar(self) -> None:
        x_min, x_max = self._panel_axes["price"].get_xlim()
        denom = (self._x_hi - self._x_lo) - (x_max - x_min)
        val = 0 if denom <= 0 else int((x_min - self._x_lo) / denom * _SCROLL_MAX)
        self.scrollbar.blockSignals(True)
        self.scrollbar.setRange(0, _SCROLL_MAX)
        self.scrollbar.setValue(max(0, min(_SCROLL_MAX, val)))
        self.scrollbar.blockSignals(False)

    def _on_scrollbar_change(self, val: int) -> None:
        x_min, x_max = self._panel_axes["price"].get_xlim()
        w = x_max - x_min
        new_xmin = self._x_lo + (val / _SCROLL_MAX) * ((self._x_hi - self._x_lo) - w)
        self._set_xlim(new_xmin, new_xmin + w)

    def _on_period_clicked(self, label: str, months) -> None:
        self._apply_period(label, months)

    def _apply_period(self, label: str, months=None) -> None:
        if label in self._period_buttons and not self._period_buttons[label].isChecked():
            self._period_buttons[label].setChecked(True)
        if months is None:
            months = dict(_PERIODS).get(label)
        if months is None:
            self._set_xlim(self._x_lo, self._x_hi)
            return
        end = pd.to_datetime(self._dates[-1])
        start = mdates.date2num(end - pd.DateOffset(months=months))
        self._set_xlim(max(start, self._x_lo), self._x_hi)

    def _on_scroll(self, event) -> None:
        if event.inaxes not in self._chart_axes() or event.xdata is None:
            return
        scale = 1.15
        if event.button == 'up':
            factor = 1 / scale        # zoom in
        elif event.button == 'down':
            factor = scale            # zoom out
        else:
            return
        x_min, x_max = self._panel_axes["price"].get_xlim()
        x_focus = event.xdata
        self._set_xlim(x_focus - (x_focus - x_min) * factor, x_focus + (x_max - x_focus) * factor)

    def _on_press(self, event) -> None:
        if event.button != 1 or event.inaxes not in self._chart_axes():
            return
        self._pan_start = (event.x, self._panel_axes["price"].get_xlim())

    def _on_motion(self, event) -> None:
        if self._pan_start is not None:
            xpress, xlim = self._pan_start
            bbox = self._panel_axes["price"].get_window_extent()
            if bbox.width == 0:
                return
            dx_data = (event.x - xpress) * ((xlim[1] - xlim[0]) / bbox.width)
            self._set_xlim(xlim[0] - dx_data, xlim[1] - dx_data)
            return
        if event.inaxes not in self._chart_axes() or event.xdata is None:
            if self._cursor_idx is not None:
                self._set_cursor(None)
            return
        idx = self._index_at(event.xdata)
        if idx != self._cursor_idx:
            self._set_cursor(idx)

    def _on_release(self, _event) -> None:
        self._pan_start = None

    def _zoom_y_in(self, *_):
        self._zoom_y(0.8)

    def _zoom_y_out(self, *_):
        self._zoom_y(1.2)

    def _zoom_y(self, factor: float) -> None:
        """Scale the price panel's Y range around the midpoint of the visible closes."""
        ax = self._panel_axes["price"]
        ymin, ymax = ax.get_ylim()
        try:
            close = np.asarray(self._col("Close"), dtype=float)[self._visible_mask()]
            close = close[np.isfinite(close)]
            ymid = (close.min() + close.max()) / 2 if len(close) else (ymin + ymax) / 2
        except Exception:
            ymid = (ymin + ymax) / 2
        ydiff = (ymax - ymin) / 2
        ax.set_ylim(ymid - ydiff * factor, ymid + ydiff * factor)
        self._place_end_labels()
        self._canvas.draw_idle()
