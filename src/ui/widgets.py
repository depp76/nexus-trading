"""ui/widgets.py — re-export module for the table widgets that used to live here.

Split out of main.py (2026-08-29 feat/3-1-modularize); split again on
2026-11-15 (code review #5: the file had grown to ~1,450 lines) into:
  ui/table_items.py      ColSpec, NumericItem
  ui/universe_columns.py COLUMNS, COL_*, GROUP_START_COLS, GROUP_LINE_W
  ui/heat_legend.py      HeatLegend
  ui/filter_popups.py    FilterPopup, SignalScanPopup
  ui/table_headers.py    FilterableHeader, GroupedHeaderView
  ui/universe_table.py   StockTable
Existing `from ui.widgets import X` imports keep working through the names
below; new code imports from those modules directly.
"""
from ui.table_items import ColSpec, NumericItem
from ui.universe_columns import (
    COLUMNS, COL_IDENTITY, COL_CAP, COL_TPER, COL_FPER, COL_PRICE, COL_CHG, COL_RANGE52W,
    COL_MA20DIV, COL_MA60DIV, COL_D3, COL_D10, COL_D20, COL_D60, COL_D120, COL_TREND,
    GROUP_START_COLS, GROUP_LINE_W,
)
from ui.heat_legend import HeatLegend
from ui.filter_popups import FilterPopup, SignalScanPopup
from ui.table_headers import FilterableHeader, GroupedHeaderView
from ui.universe_table import StockTable

__all__ = [
    "ColSpec", "NumericItem",
    "COLUMNS", "COL_IDENTITY", "COL_CAP", "COL_TPER", "COL_FPER", "COL_PRICE", "COL_CHG", "COL_RANGE52W",
    "COL_MA20DIV", "COL_MA60DIV", "COL_D3", "COL_D10", "COL_D20", "COL_D60", "COL_D120", "COL_TREND",
    "GROUP_START_COLS", "GROUP_LINE_W",
    "HeatLegend", "FilterPopup", "SignalScanPopup", "FilterableHeader", "GroupedHeaderView", "StockTable",
]
