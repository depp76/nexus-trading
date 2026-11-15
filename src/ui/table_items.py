"""ui/table_items.py — table building blocks shared by several tabs:
ColSpec (a column's label / min width / flex weight / group / heatmap scale)
and NumericItem (a QTableWidgetItem whose sort key is independent of its text).

Moved out of ui/widgets.py (2026-11-15, code review #5); used by the Universe
table (ui/universe_columns.py), the History grid (ui/history_table.py), Total
Assets and the dialogs' numeric tables. ui.widgets still re-exports both.
"""
from collections import namedtuple

from PyQt6.QtWidgets import QTableWidgetItem

# One column's spec. The Universe table's COLUMNS (ui/universe_columns.py)
# documents each field; history_table.py and assets_tab.py build their own.
ColSpec = namedtuple("ColSpec", ["key", "label", "min_width", "weight", "group", "scale"])


class NumericItem(QTableWidgetItem):
    """QTableWidgetItem with a numeric sort key independent of its display
    text.

    Found while rebuilding this table's cells for the docs/ui.md redesign:
    QTableWidgetItem aliases Qt.ItemDataRole.EditRole and
    Qt.ItemDataRole.DisplayRole onto the same storage by default (a
    documented but easy-to-miss Qt behavior for QStandardItem-family
    classes). The pattern this file (and history_table.py's ni()/pi(),
    assets_tab.py's _pct_item() etc.) used everywhere --
    `setData(EditRole, 1849000.0)` then `setText("1,849,000")` -- means
    whichever call runs last simply overwrites the other; the formatted
    string silently becomes the sort key too, so a comma-formatted numeric
    column sorts as plain text ("1,849,000" sorts before "999,000"). This
    subclass keeps the two independent by overriding __lt__ instead of
    relying on either role for ordering.
    """

    def __init__(self, text: str, sort_key):
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other):
        other_key = getattr(other, "_sort_key", None)
        if other_key is None:
            return super().__lt__(other)
        return self._sort_key < other_key
