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
    | name  ticker · market  price  chg%                          as-of |
    | Series [Close MA5 .. EW]  [1M 3M 6M 1Y 3Y All]  [Volume] [RSI]     |
    |   (2026-11-15: one wrapping row; a group moves down when narrow)    |
    |   FigureCanvas: price (+MA ramp)      | right panel (optional)   |
    |                 volume                |   investor / futures tbl |
    |                 divergence            |   "Company Info ↗"        |
    |                 rsi                   |                          |
    |   horizontal scrollbar                |                          |
    +------------------------------------------------------------------+
    [Close]

Simple charts (bond yields, VIX, WTI) keep only the price panel and the
period buttons; bonds also get the Y-axis +/- buttons.

2026-11-15 (code review #5): the right panel (side tables, Trend Score block,
Company Info, alignment) moved to ui/dialogs/stock_ma_side.py and the pure
volume-bar / number helpers to ui/dialogs/stock_ma_helpers.py.

2026-11-15 (later): the rest of the chart code moved into three mixins that
StockMaDialog inherits; the dialog keeps construction, the header / button
rows, number formatting and the right-panel alignment:

  stock_ma_plot.py   _PlotMixin   panels, fixed slots, margins, end labels,
                                  series / panel toggles
  stock_ma_hover.py  _HoverMixin  crosshair, hover badges, date badge
  stock_ma_view.py   _ViewMixin   pan / zoom / scrollbar / period, Y autofit,
                                  bond Y zoom
  stock_ma_consts.py the module constants (re-exported here)
"""
import logging

import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QButtonGroup,
    QSplitter, QWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from i18n import tr
from ui.dialogs.stock_ma_consts import (  # noqa: F401 -- re-exported for callers of ui.dialogs.stock_ma
    _STOCK_MARKETS, _SIMPLE_CHART_TICKERS, _SCROLL_MAX, _TICK_PT, _SEG_H, _PERIODS, _PERIOD_LABELS,
    _DEFAULT_PERIOD, _DEFAULT_MARGINS, _MARGIN_PAD_PX, _MIN_RIGHT_PX, _SLOT_RATIOS, _SERIES,
    _PRICE_SERIES, _DEFAULT_SERIES,
)
from ui.dialogs.stock_ma_hover import _HoverMixin
from ui.dialogs.stock_ma_plot import _PlotMixin
from ui.dialogs.stock_ma_side import StockMaSidePanel
from ui.dialogs.stock_ma_view import _ViewMixin
from ui.common import FlowLayout, create_font, FONT_HEADING, FONT_BODY, FONT_CAPTION, fit_to_screen
from ui.colors import PROFIT, LOSS, MA_RAMP
from ui.theme import TEXT_MUTED, TEXT_FAINT

logger = logging.getLogger(__name__)


def _segment_button(text: str, group: QButtonGroup = None, checked: bool = False, width: int = None) -> QPushButton:
    btn = QPushButton(text)
    btn.setCheckable(True)
    btn.setChecked(checked)
    btn.setFixedHeight(_SEG_H)
    btn.setFont(create_font(FONT_BODY, style_name="Semilight"))
    if width:
        btn.setFixedWidth(width)
    if group is not None:
        group.addButton(btn)
    return btn


class StockMaDialog(_PlotMixin, _HoverMixin, _ViewMixin, QDialog):
    """Price + moving averages for one instrument, with volume, divergence and
    RSI panels (docs/ui.md MA Chart Redesign). Plotting, crosshair and
    pan/zoom live in the mixins (see the module docstring)."""

    def __init__(self, ticker, name, market, df, investor_data=None, parent=None, change_mode='pct',
                 trend_rec=None):
        super().__init__(parent)
        # The last Trend Score run (scoring.Recommendation) when the dialog was
        # opened from Trading Universe; KR stocks get its breakdown in the
        # right panel (ui.trend_score_section, docs/ui.md 8.10).
        self._trend_rec = trend_rec
        self._ticker = ticker
        self._name = name
        self._market = market
        self._df = df
        self._investor_data = investor_data or []
        self._change_mode = change_mode

        self._setup_window()
        self._is_simple_chart = ticker in _SIMPLE_CHART_TICKERS or change_mode == 'bp'
        self._has_data = (
            df is not None and not df.is_empty() and "Close" in df.columns
            and (self._is_simple_chart or all(c in df.columns for c in MA_RAMP))
        )
        self._currency, self._unit_suffix, self._fmt_str = self._format_config()
        self._init_chart_state()

        splitter, chart_v = self._build_splitter()
        self._build_chart_column(chart_v)

        # No Close button (user direction 2026-11-15): the title bar and Esc close it.
        splitter.splitterMoved.connect(lambda *_: self._align_right_panel())

    # ---------------------------------------------------------- construction
    def _setup_window(self) -> None:
        """Window flags, title and the maximized initial size."""
        name, ticker = self._name, self._ticker
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setWindowTitle(tr(f"{name} ({ticker}) — 이동평균", f"{name} ({ticker}) — Moving Averages"))
        fit_to_screen(self, 1200, 720)
        self.showMaximized()

    def _init_chart_state(self) -> None:
        """Artists / state the interaction handlers refer to."""
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

    def _build_splitter(self) -> tuple[QSplitter, QVBoxLayout]:
        """The root splitter: the chart column on the left and, for stocks or
        when there is investor / futures data, the right panel. Returns the
        splitter and the (still empty) chart column layout."""
        root = QVBoxLayout(self)
        root.setSpacing(6)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        chart_widget = QWidget()
        chart_v = QVBoxLayout(chart_widget)
        chart_v.setContentsMargins(0, 0, 0, 0)
        chart_v.setSpacing(4)
        splitter.addWidget(chart_widget)

        is_stock = self._market in _STOCK_MARKETS
        self._right_layout = None
        self._right_panel = None
        self._right_title = None      # first line of the right panel, level with the button row
        self._right_table = None      # starts level with the chart's top edge
        self._right_gap = None        # spacer between the two, sized by _align_right_panel
        self._side = None             # StockMaSidePanel when the dialog has a right panel
        if self._investor_data or is_stock:
            panel = self._build_right_panel(is_stock)
            splitter.addWidget(panel)
            splitter.setStretchFactor(0, 10)
            splitter.setStretchFactor(1, 0)          # the panel keeps its content width; the chart flexes
            splitter.setCollapsible(1, False)
        return splitter, chart_v

    def _build_chart_column(self, chart_v: QVBoxLayout) -> None:
        """Header and button rows, the figure (plotted, or a notice without
        data), the bond Y-zoom row and the horizontal scrollbar."""
        chart_v.addLayout(self._build_header_row())
        chart_v.addWidget(self._build_view_row())

        self._fig = Figure(figsize=(9.5, 7.5))
        self._canvas = FigureCanvas(self._fig)
        if self._has_data:
            self._plot()
        else:
            self._draw_no_data_notice()

        if self._change_mode == 'bp':
            chart_v.addLayout(self._build_y_zoom_row())
        chart_v.addWidget(self._canvas, 1)
        if self.scrollbar is not None:
            chart_v.addWidget(self.scrollbar)

    def _draw_no_data_notice(self) -> None:
        ax = self._fig.add_subplot(111)
        ax.text(0.5, 0.5, tr("상세 데이터를 불러오지 못했습니다.", "Failed to load detailed data."),
                ha="center", va="center",
                transform=ax.transAxes, color=TEXT_MUTED, fontsize=FONT_BODY)
        ax.set_axis_off()

    # ------------------------------------------------------------- top rows
    def _build_header_row(self) -> QHBoxLayout:
        """Name · ticker/market · last price · change% (the as-of date moved
        to the button row, 2026-11-15)."""
        row = QHBoxLayout()
        row.setSpacing(10)

        name_lbl = QLabel(self._name)
        name_lbl.setFont(create_font(FONT_HEADING, QFont.Weight.Bold))
        row.addWidget(name_lbl)

        meta_lbl = QLabel(f"{self._ticker} · {self._market}")
        meta_lbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
        meta_lbl.setStyleSheet(f"color:{TEXT_FAINT};")
        row.addWidget(meta_lbl)

        last, chg = self._last_close_and_change()
        self._price_lbl = QLabel(self._fmt_price(last) if last is not None else "—")
        self._price_lbl.setFont(create_font(FONT_HEADING, QFont.Weight.Bold))
        row.addWidget(self._price_lbl)
        self._chg_lbl = QLabel(self._fmt_change(chg) if chg is not None else "")
        self._chg_lbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
        if chg is not None:
            self._chg_lbl.setStyleSheet(f"color:{PROFIT if chg > 0 else LOSS if chg < 0 else TEXT_MUTED};")
        row.addWidget(self._chg_lbl)
        row.addStretch()
        return row

    @staticmethod
    def _button_group_widget() -> tuple[QWidget, QHBoxLayout]:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        return w, lay

    def _build_view_row(self) -> QWidget:
        """Series toggles | period segment | panel toggles, as three groups in
        a FlowLayout (user direction 2026-11-15): one line when the chart is
        wide enough, otherwise a group moves to the next line instead of
        raising the dialog's minimum width. A group never splits."""
        row_w = QWidget()
        row = FlowLayout(row_w, h_spacing=18, v_spacing=6)
        # Clear of the splitter: the as-of date ends the row and the right
        # panel's TREND SCORE title starts level with it.
        row.setContentsMargins(0, 0, 24, 0)
        series_w, series = self._button_group_widget()
        self._series_buttons = {}
        keys = [] if self._is_simple_chart else ["Close", *MA_RAMP.keys(), "Div"]
        if self._has_data and "EqualWeight" in self._df.columns and not self._is_simple_chart:
            keys.append("EW")
        if keys:
            cap = QLabel(tr("표시", "Series"))
            cap.setFont(create_font(FONT_CAPTION, QFont.Weight.Bold))
            cap.setStyleSheet(f"color:{TEXT_FAINT};")
            series.addWidget(cap)
        for key in keys:
            btn = _segment_button(_SERIES[key], checked=(key in _DEFAULT_SERIES))
            btn.clicked.connect(lambda checked, k=key: self._set_series(k, checked))
            series.addWidget(btn)
            self._series_buttons[key] = btn
        if keys:
            row.addWidget(series_w)

        period_w, period = self._button_group_widget()
        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        self._period_buttons = {}
        for label, months in _PERIODS:
            btn = _segment_button(_PERIOD_LABELS[label], self._period_group, checked=(label == _DEFAULT_PERIOD))
            btn.clicked.connect(lambda _c, m=months, lbl=label: self._on_period_clicked(lbl, m))
            period.addWidget(btn)
            self._period_buttons[label] = btn
        # One width for the segment, from the widest label (a fixed 52 px cut
        # the first digit of "3개월"/"6개월").
        seg_w = max(b.sizeHint().width() for b in self._period_buttons.values())
        for b in self._period_buttons.values():
            b.setFixedWidth(seg_w)
        row.addWidget(period_w)

        # The divergence panel has no button here: the Divergence series
        # toggle drives it.
        panels_w, panels = self._button_group_widget()
        self._panel_buttons = {}
        for key, label in (("vol", tr("거래량", "Volume")), ("rsi", "RSI")):
            btn = _segment_button(label)
            btn.clicked.connect(lambda checked, k=key: self._set_panel(k, checked))
            panels.addWidget(btn)
            self._panel_buttons[key] = btn
        row.addWidget(panels_w)

        # As-of date at the right end of the (last) button line: an Ignored
        # policy makes FlowLayout stretch it to the line's end, and it never
        # gets narrower than its text.
        as_of = ""
        if self._has_data:
            last_day = pd.to_datetime(self._df.get_column('Date')[-1]).strftime('%Y-%m-%d')
            as_of = tr(f"{last_day} 기준", f"as of {last_day}")
        self._as_of_lbl = QLabel(as_of)
        self._as_of_lbl.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        self._as_of_lbl.setStyleSheet(f"color:{TEXT_FAINT};")
        self._as_of_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._as_of_lbl.setMinimumWidth(self._as_of_lbl.sizeHint().width())
        self._as_of_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if as_of:
            row.addWidget(self._as_of_lbl)
        return row_w

    def _build_right_panel(self, is_stock: bool) -> QWidget:
        """The right panel (ui/dialogs/stock_ma_side.py); its pieces stay
        reachable under the attribute names the dialog has always used."""
        self._side = StockMaSidePanel(self._ticker, self._market, self._investor_data, self._trend_rec, is_stock)
        self._right_panel = self._side.widget
        self._right_layout = self._side.layout
        self._right_title = self._side.title
        self._right_table = self._side.table
        self._right_gap = self._side.gap
        self.trend_section = self._side.trend_section
        return self._side.widget

    def _align_right_panel(self) -> None:
        """Right panel's first line level with the first button line, its
        table's top level with the chart's top edge (StockMaSidePanel.align)."""
        if self._side is None or not self._side.widget.isVisible():
            return
        buttons = list(self._series_buttons.values()) or list(self._period_buttons.values())
        self._side.align(buttons, self._canvas)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._align_right_panel)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._align_right_panel)

    def _build_y_zoom_row(self) -> QHBoxLayout:
        """'Zoom Y +/−' buttons (bond yields only; the X range is handled by scroll-zoom)."""
        row = QHBoxLayout()
        zoom_in_btn = QPushButton(tr("Y 확대", "Zoom Y +"))
        zoom_out_btn = QPushButton(tr("Y 축소", "Zoom Y −"))
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
