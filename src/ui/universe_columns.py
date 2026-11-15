"""ui/universe_columns.py — the Trading Universe table's column spec and cell
formatting helpers: COLUMNS, the COL_* indices, the group dividers, the
range-filter / momentum column maps and the change-cell formatters.

Moved out of ui/widgets.py (2026-11-15, code review #5) so StockTable
(ui/universe_table.py), its header (ui/table_headers.py) and HeatLegend
(ui/heat_legend.py) share it without importing each other. ui.widgets still
re-exports the public names.
"""
from i18n import tr
from ui.colors import heatmap_bg
from ui.table_items import ColSpec
from ui.universe_views import RANGE_FIELDS

# ---------------------------------------------------------------------------
# StockTable column spec (docs/ui.md 2.2's merged-column redesign, sourced
# from the "Trading Universe Redesign" mockup's own `cols`/`headers`
# tables) -- the one place label, min-width, flex weight, toggle group and
# heatmap scale live for every column.
#
# This replaces the prior 22-column layout (separate Name/Market/Ticker/Pf/
# MA/Del columns, Div(20)+Div(50), 52W High/High Diff/Low/Low Diff as four
# columns, 3D/5D/10D/20D/60D/120D) with the mockup's layout (14 columns): Name+Ticker merged
# into one identity cell (with an inline status badge, replacing the Pf
# button column), a single 52W range bar (replacing 4 columns), Div(20) and
# Div(50) merged into one "MA20 Div" (Div(50) dropped -- the mockup doesn't
# carry it; MA50 Div re-added as its own column on 2026-09-22 by user
# direction, so the table is 15 columns again), momentum as 3D/10D/20D/60D/120D
# (5D dropped; 10D re-added on 2026-09-19 by user direction), a new
# "Chg" day-over-day column (data/cache.py's _TD_PERIODS now has a "1d"
# entry for this), and a "Trend" mini chart. The MA-chart and Delete action
# buttons are gone too -- see StockTable's docstring for what replaced them.
#
# group ("price"/"value"/"momentum", None for the identity column) is kept
# as the column's semantic bucket; the toolbar toggle that used to hide
# groups was removed on 2026-09-19 (user direction), so it is documentation
# only now (history_table.py derives its header sections from the same
# field on its own ColSpecs).
# scale is the docs/ui.md 1.2 heatmap alpha scale, or None for non-heatmap
# columns. "trend" isn't a text cell (see TrendDelegate) so it has no scale.
# "chg" (day-over-day) uses a tighter scale than the 3D-120D columns since a
# single day's move is rarely more than a few percent for most stocks.
# (ColSpec itself is defined in ui/table_items.py.)
# ---------------------------------------------------------------------------

# User direction 2026-09-19: Price and Cap are a fixed 80 px (a seven-digit
# won price or market cap is not wall-to-wall); Chg, MA20 Div and every
# momentum column share the 3D column's width; 10D is back next to 3D.
# User direction 2026-10-02: column order is Cap, tPER, fPER, 52W Range,
# MA20 Div, MA50 Div, Price, Chg, 3D ... (Price/Chg moved next to the
# momentum block; the PER pair sits right of Cap). User direction
# 2026-10-20: Price and Chg moved again, to the left of 52W Range. _populate_row still builds cells in its own order and
# addresses them by the COL_* constants, so only this list and the
# constants below changed.
_NARROW_W, _NARROW_WEIGHT = 58, 0.62

COLUMNS = [
    ColSpec("identity",  tr("종목 / 코드", "Name / Ticker"), 150, 2.4,  None,       None),
    ColSpec("cap",       tr("시총", "Cap"),    80, None, "price",    None),
    ColSpec("tper",      "tPER",           54, 0.55, "value",    None),
    ColSpec("fper",      "fPER",           54, 0.55, "value",    None),
    ColSpec("price",     tr("가격", "Price"),  80, None, "price",    None),
    ColSpec("chg",       tr("등락", "Chg"),    _NARROW_W, _NARROW_WEIGHT, "price",    5),
    ColSpec("range52w",  tr("52주 범위", "52W Range"), 112, 1.5,  "price",    None),
    ColSpec("ma20div",   "MA20 Div",       _NARROW_W, _NARROW_WEIGHT, "momentum", 20),
    ColSpec("ma50div",   "MA50 Div",       _NARROW_W, _NARROW_WEIGHT, "momentum", 20),
    ColSpec("d3",        "3D",             _NARROW_W, _NARROW_WEIGHT, "momentum", 10),
    ColSpec("d10",       "10D",            _NARROW_W, _NARROW_WEIGHT, "momentum", 10),
    ColSpec("d20",       "20D",            _NARROW_W, _NARROW_WEIGHT, "momentum", 10),
    ColSpec("d60",       "60D",            _NARROW_W, _NARROW_WEIGHT, "momentum", 10),
    ColSpec("d120",      "120D",           _NARROW_W, _NARROW_WEIGHT, "momentum", 10),
    ColSpec("trend",     tr("추세", "Trend"),  80, 1.0,  "momentum", None),
]

# Column indices as constants -- COLUMNS' own order is the single source of
# truth, but spelling out "self.item(row, 1)" everywhere a specific column
# is meant makes future reordering a silent bug. Kept in sync with COLUMNS
# by the assertion below (fails loudly at import time if they drift).
COL_IDENTITY, COL_CAP, COL_TPER, COL_FPER, COL_PRICE, COL_CHG, COL_RANGE52W, \
    COL_MA20DIV, COL_MA50DIV, COL_D3, COL_D10, COL_D20, COL_D60, COL_D120, COL_TREND = range(len(COLUMNS))
assert [c.key for c in COLUMNS] == [
    "identity", "cap", "tper", "fper", "price", "chg", "range52w", "ma20div", "ma50div",
    "d3", "d10", "d20", "d60", "d120", "trend",
]

# Column groups (user direction 2026-10-02; the [Price, Chg] pair moved left
# of 52W Range on 2026-10-20): [Cap, tPER, fPER], [Price, Chg], [52W Range,
# MA20 Div, MA50 Div], [3D..120D], [Trend]. A thin gray
# (GROUP_LINE_W px, LINE_GROUP = the table's outer-frame gray) vertical
# divider sits on the boundary on the left of each group's first column
# (the previous column's last pixel, where the grid line is): that also
# covers the left of Cap and the right of 120D (= the left of Trend).
GROUP_START_COLS = (COL_CAP, COL_PRICE, COL_RANGE52W, COL_D3, COL_TREND)
GROUP_LINE_W = 1

# Columns that take a range filter (ui.universe_views.RANGE_FIELDS) -> column index.
_RANGE_FILTER_COLS = {f.key: next(i for i, c in enumerate(COLUMNS) if c.key == f.key) for f in RANGE_FIELDS}

# Momentum columns paired with the changes{} dict key each one reads.
_MOMENTUM_COLS = [(COL_D3, "3d"), (COL_D10, "10d"), (COL_D20, "20d"), (COL_D60, "60d"), (COL_D120, "120d")]

# Index, yield and commodity rows sit in the same table as equities (user
# direction 2026-09-19, superseding docs/ui.md 2.4's separate Market rail) so
# their price and change rates read alongside stocks. Their change_mode sets
# the unit: 'pct' (stocks, indices), 'bp' (yields), 'abs' (VIX/WTI levels).
_BP_HEAT_SCALE = 25.0   # 25 bp is "full" heat, in place of a pct column's scale


def _market_label(market: str) -> str:
    """A row's market as shown in its identity meta: the index rows'
    "Index" value in the UI language, market names as-is."""
    return tr("지수", "Index") if market == "Index" else market


def _fmt_change(v: float, mode: str) -> str:
    if mode == 'bp':
        return f"{v:+.0f}bp"
    if mode == 'abs':
        return f"{v:+.2f}"
    return f"{v:+.1f}%"


def _change_bg(v: float, pct_scale, mode: str):
    """Heatmap tint for a change cell; abs-mode levels have no comparable
    scale, so they only get the PROFIT/LOSS foreground."""
    if mode == 'abs' or pct_scale is None:
        return None
    return heatmap_bg(v, _BP_HEAT_SCALE if mode == 'bp' else pct_scale)
