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

PROFIT = "#a5c9ff"   # gain, up (Soft Slate: light blue on the dark surface, docs/ui.md 8.16)
LOSS = "#ffb1a8"     # loss, down (light red)
FLAT = "#aeb8c6"     # zero / not applicable

# Danger button hue (delete/destructive actions) -- kept separate from
# PROFIT/LOSS so it doesn't flip meaning when the price-direction convention
# changes (2026-09-22: PROFIT/LOSS swapped to blue=up/red=down, but "danger"
# must stay red regardless of which one currently means "up").
DANGER = "#ff9e94"
DANGER_BG = "#5a3f47"     # danger hover / invalid-input field fill
DANGER_LINE = "#a0646a"   # danger button outline

# Status / judgement colors (docs/ui.md 8.3) -- a third channel next to
# PROFIT/LOSS (price direction) and the accent (selection/emphasis). OK is
# "good / done" (a cheap tPER, a fetched deposit); WARN is "look at this"
# (an expensive tPER). WARN is amber rather than red so it never reads as
# LOSS; a failure uses DANGER.
OK = "#8fd6b0"
WARN = "#f2b866"

# Badge fills for the status channel (docs/ui.md 8.3; ui_redesign_2026-11 P0-1).
# Dark fills with text one step lighter than OK/WARN/DANGER so a small badge
# label keeps 4.5:1 contrast on its fill.
OK_BG, OK_TEXT = "#2f5446", "#bdefd3"
WARN_BG, WARN_TEXT = "#5a4a2e", "#ffdda6"
FAIL_BG, FAIL_TEXT = "#5e3b42", "#ffc7c0"
NA_BG, NA_TEXT = "#4a5666", "#dce1e9"

# Chart series roles (strategy charts; ui_redesign_2026-11 P0-1).
SERIES_SELECTED = "#c4b8f1"   # = theme.ACCENT, solid
SERIES_BENCH = "#afb9c6"      # = theme.TEXT_FAINT, dashed
SERIES_TARGET = WARN          # target lines (the -15% drawdown), dashed
DRAWDOWN_ALPHA = 0.25         # LOSS fill under the drawdown curve
# Several runs on one chart (docs/ui.md 8.14): one hue per selected run, in
# selection order, the accent first. Pure blue/red are left out so a series
# colour never reads as PROFIT/LOSS; BM1 keeps SERIES_BENCH.
SERIES_PALETTE = ("#c4b8f1", "#6fd3c4", "#f5a863", "#a6d96a", "#f08cc8", "#e3c86a", "#9fb8d0", "#d2a894")

# MA divergence (price / MA x 100): within +-MA_DIV_NEUTRAL_PCT of 100 is
# "neutral". The MA chart's three background bands (StockMaDialog) and the
# Universe table's MA20 Div cell both read this one number so the chart's
# overheated/depressed zones and the table's coloring agree (docs/ui.md
# MA Chart Redesign issue #6: the two used to have different thresholds).
MA_DIV_NEUTRAL_PCT = 2.0

# Moving-average lines: one accent ramp, brightest for the shortest window
# on the dark surface, so period order reads as lightness instead of five
# competing hues (MA Chart Redesign issue #2; Soft Slate keeps every step
# >= 3:1 against the chart face). Close is neutral ink (ui.theme.TEXT).
# 2026-11-15: the old ramp started at #ece6ff, almost the close line's white
# (CIE dE 11.7 to TEXT), so MA5 and Close could not be told apart. The
# ramp is now one saturated lavender hue (HLS 252 deg) stepped by contrast
# on the chart face (6.2 / 4.9 / 3.9 / 3.1:1, all >= 3:1): MA5 is dE 32 from
# Close and neighbouring steps are dE ~16 apart (MA20/MA50 were 8).
#
# 2026-11-18 (docs/ma60_unification_2026-11-18.md): the chart draws MA20 /
# MA50 / MA60 / MA200 (MA10 gone). The ramp has no room below MA50's old
# 3.1:1 step, so the four lines take the four steps, shorter = lighter. MA5
# keeps its colour (the MA20 divergence line uses it) and the MA50
# divergence line keeps its old colour through MA_DIV50_COLOR.
# Later the same day MA50 left the chart menu (spec v07): three lines on the ramp's first,
# second and last steps (6.2 / 4.9 / 3.1:1), so neighbours stay well apart.
MA_RAMP = {"MA5": "#d1c5ff", "MA20": "#d1c5ff", "MA60": "#bcabff", "MA200": "#977cff"}
MA_DIV50_COLOR = "#a993ff"     # was MA_RAMP["MA20"] before 2026-11-18

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
