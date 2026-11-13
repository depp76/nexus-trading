"""ui/colors.py — Single source of truth for profit/loss color semantics and
heatmap intensity (docs/ui.md 1.1, 1.2).

The whole app follows one rule: PROFIT (blue) = gain/up, LOSS (red) =
loss/down (user direction, 2026-09-22, overriding the earlier Korean market
convention of red=up/blue=down). Before this module, widgets.py used
blue for large positive change rates and history_table.py used red for
positive P/L, so the same color flipped meaning within a single Universe row
and across tabs. Any cell that previously picked a background from a
hardcoded threshold ladder should compute it via heatmap_bg() instead.
"""
from PyQt6.QtGui import QColor

PROFIT = "#3b6fc4"   # gain, up
LOSS = "#d1453b"      # loss, down
FLAT = "#75798c"      # zero / not applicable

# Danger button hue (delete/destructive actions) -- kept separate from
# PROFIT/LOSS so it doesn't flip meaning when the price-direction convention
# changes (2026-09-22: PROFIT/LOSS swapped to blue=up/red=down, but "danger"
# must stay red regardless of which one currently means "up").
DANGER = "#d1453b"
DANGER_BG = "#fdf0ef"     # danger hover / invalid-input field fill
DANGER_LINE = "#f0c4c1"   # danger button outline

# Status / judgement colors (docs/ui.md 8.3) -- a third channel next to
# PROFIT/LOSS (price direction) and the accent (selection/emphasis). OK is
# "good / done" (a cheap tPER, a fetched deposit); WARN is "look at this"
# (an expensive tPER). WARN is amber rather than red so it never reads as
# LOSS; a failure uses DANGER.
OK = "#2e7d5b"
WARN = "#b8650a"

# Badge fills for the status channel (docs/ui.md 8.3; ui_redesign_2026-11 P0-1).
# Text colors are one step darker than OK/WARN/DANGER so a small badge label
# keeps 4.5:1 contrast on its fill.
OK_BG, OK_TEXT = "#e8f3ee", "#2e7d5b"
WARN_BG, WARN_TEXT = "#fff6e8", "#8a4f08"
FAIL_BG, FAIL_TEXT = DANGER_BG, "#b3362e"
NA_BG, NA_TEXT = "#f0f1f8", "#595d6c"

# Trend Score rank badges in the Universe identity cell (ui_redesign_2026-11
# P2-1): Top on a light PROFIT tint, Bottom on a light LOSS tint.
RANK_TOP_BG, RANK_TOP_TEXT = "#e9eefa", "#2d5aa8"
RANK_BOTTOM_BG, RANK_BOTTOM_TEXT = "#fbecea", "#b3362e"

# Chart series roles (strategy charts; ui_redesign_2026-11 P0-1).
SERIES_SELECTED = "#9184d9"   # = theme.ACCENT, solid
SERIES_BENCH = "#9397ab"      # = theme.TEXT_FAINT, dashed
SERIES_TARGET = WARN          # target lines (Sharpe 1.5, -15% drawdown), dashed
DRAWDOWN_ALPHA = 0.18         # LOSS fill under the drawdown curve

# MA divergence (price / MA x 100): within +-MA_DIV_NEUTRAL_PCT of 100 is
# "neutral". The MA chart's three background bands (StockMaDialog) and the
# Universe table's MA20 Div cell both read this one number so the chart's
# overheated/depressed zones and the table's coloring agree (docs/ui.md
# MA Chart Redesign issue #6: the two used to have different thresholds).
MA_DIV_NEUTRAL_PCT = 2.0

# Moving-average lines: one accent ramp, darkest for the shortest window,
# so period order reads as lightness instead of five competing hues
# (MA Chart Redesign issue #2). Close is neutral ink (ui.theme.TEXT).
MA_RAMP = {"MA5": "#6d5fb8", "MA10": "#9184d9", "MA20": "#b5abfc", "MA50": "#d8d5f2"}

# Pre-allocated QColor instances -- avoids re-parsing the hex string on every
# cell of every row (_populate_row/fill_table_rows run per cell per refresh).
QC_PROFIT = QColor(PROFIT)
QC_LOSS = QColor(LOSS)
QC_FLAT = QColor(FLAT)


def fg_for(value: float) -> QColor:
    """Foreground color for a signed value: PROFIT above zero, LOSS below,
    FLAT at exactly zero."""
    if value > 0:
        return QC_PROFIT
    if value < 0:
        return QC_LOSS
    return QC_FLAT


def heatmap_bg(value: float, scale: float):
    """Background color for a momentum/change-rate cell (docs/ui.md 1.2).

    Replaces the per-column if-ladders that used to hardcode their own
    thresholds (Div(20): 120/110/105/102/98/93, Div(50): 130/110/107/103/98/90,
    change rate: +-30/+-15/0) with one shared formula:
    alpha = 0.04 + min(|value|/scale, 1) * 0.20, colored PROFIT above zero and
    LOSS below. `scale` is the column's own "full intensity" distance from
    zero (docs/ui.md 1.2: momentum columns share scale=10).

    Returns None for value == 0 (no highlight).
    """
    if value == 0:
        return None
    color = QColor(QC_PROFIT if value > 0 else QC_LOSS)
    alpha = 0.04 + min(abs(value) / scale, 1.0) * 0.20
    color.setAlphaF(alpha)
    return color
