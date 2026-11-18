"""ui/dialogs/stock_ma_consts.py — module constants shared by the MA chart
dialog (ui/dialogs/stock_ma.py) and its mixins (stock_ma_plot.py,
stock_ma_hover.py, stock_ma_view.py).

Moved out of ui/dialogs/stock_ma.py (2026-11-15); ui.dialogs.stock_ma
re-exports every name, so `from ui.dialogs.stock_ma import _PERIODS` keeps
working.
"""
from i18n import tr
from ui.colors import SERIES_PALETTE
from ui.common import BTN_H, FONT_BODY


_STOCK_MARKETS = ("KOSPI", "KOSDAQ", "NASDAQ 100", "S&P500")
_SIMPLE_CHART_TICKERS = ("CL=F", "^VIX", "VKOSPI")
_SCROLL_MAX = 10000
_TICK_PT = FONT_BODY          # issue #12: nothing on the chart below 9pt

# Segment buttons share one look: a checkable neutral button; the checked one
# takes the theme's QPushButton:checked accent.
_SEG_H = BTN_H

_PERIODS = [("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12), ("3Y", 36), ("All", None)]
# Visible period-button labels (docs/ui.md 8.11); the keys above stay internal.
_PERIOD_LABELS = {"1M": tr("1개월", "1M"), "3M": tr("3개월", "3M"), "6M": tr("6개월", "6M"),
                  "1Y": tr("1년", "1Y"), "3Y": tr("3년", "3Y"), "All": tr("전체", "All")}
_DEFAULT_PERIOD = "1Y"
# Figure margins before the first measurement (fractions, the old fixed
# values); _fit_margins then sizes them in pixels from the labels.
_DEFAULT_MARGINS = (0.075, 0.90)
_MARGIN_PAD_PX = 6
_MIN_RIGHT_PX = 12
# Fixed panel slots (user direction 2026-11-15: toggling a panel or a series
# must not move the price chart). Every panel the instrument has keeps its
# slot; a panel switched off leaves its slot empty.
_SLOT_RATIOS = {"price": 3, "vol": 1, "div": 1, "rsi": 1}
# Independent series toggles (user direction 2026-10-15), key -> label.
# Price-panel lines use their _lines key; "Div" is the divergence panel.
_SERIES = {
    "Close": tr("종가", "Close"), "MA20": "MA20", "MA50": "MA50", "MA60": "MA60", "MA200": "MA200",
    "Div": tr("이격도", "Divergence"), "EW": tr("동일가중", "Equal Weight"),
    "IDX": tr("시장지수", "Index"),
}
# The moving averages the chart draws and toggles. MA5 was dropped on
# 2026-11-16 (user direction); its MA_RAMP color stays for the MA20
# divergence line. 2026-11-18 (docs/ma60_unification_2026-11-18.md 1.2):
# MA10 dropped; MA60 (the A rows' L1/L2 line) and MA200 (long reference)
# added; MA50 stays for the C rows and the Universe MA50 Div.
_MA_KEYS = ("MA20", "MA50", "MA60", "MA200")
# Each line's column: the strict "SMA<w>" (min_samples=window, warm-up
# history loaded ahead) from data.indicators.add_chart_mas, not the MA<w>
# columns the divergence ratios use.
_MA_COLUMNS = {k: "S" + k for k in _MA_KEYS}
# "IDX" is the stock's market index ("MarketIndex" column), rebased onto the
# price axis at the left edge of the view (user direction 2026-11-17).
_PRICE_SERIES = ("Close", *_MA_KEYS, "EW", "IDX")
_INDEX_COLOR = SERIES_PALETTE[1]     # teal: apart from the violet MA ramp
# What the dialog opens with: close price plus the divergence panel, no moving
# averages (user direction 2026-09-29: close only on the price panel).
_DEFAULT_SERIES = frozenset({"Close", "Div"})
