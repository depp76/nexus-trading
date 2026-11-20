"""ui/dialogs/trade_history.py — StockTradeHistoryDialog — detailed trade history for a single company.

Split out of the former single ui/dialogs.py (2026-09-17). The ~240-line
__init__ was broken into builder methods and the two grid delegates moved to
module level on 2026-11-15 (code review #5; no behaviour change)."""
from i18n import tr
import logging

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView, QStyledItemDelegate,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPen

from ui.colors import QC_PROFIT, QC_LOSS
from ui.common import setup_table, make_button, button_row, fit_to_screen
from ui.theme import LINE_GROUP, TEXT_MUTED, TEXT_FAINT


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cell factories
# ---------------------------------------------------------------------------
def _si(text):
    it = QTableWidgetItem(str(text))
    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return it


def _ni(val, fmt="{:,.0f}"):
    it = QTableWidgetItem(fmt.format(val))
    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return it


def _bold_label(text, bold_font, align=Qt.AlignmentFlag.AlignCenter):
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    it.setFont(bold_font)
    return it


def _bold_ni(val, bold_font, fmt="{:,.0f}"):
    it = _ni(val, fmt)
    it.setFont(bold_font)
    return it


def _color_pl(it, val):
    """PROFIT/LOSS foreground by sign; zero keeps the default."""
    if val > 0:
        it.setForeground(QC_PROFIT)
    elif val < 0:
        it.setForeground(QC_LOSS)
    return it


# ---------------------------------------------------------------------------
# Grid delegates: the built-in grid is hidden and redrawn per row so the
# footer rows (Total, and Weight for open positions) get an outline instead
# of cell borders.
# ---------------------------------------------------------------------------
class _ClosedDelegate(QStyledItemDelegate):
    """Closed position: normal grid lines for data rows, an outline border
    around the Total row."""

    def __init__(self, parent, total_row, n_cols):
        super().__init__(parent)
        self._total_row = total_row
        self._n_cols = n_cols
        self._grid_clr = QColor(LINE_GROUP)   # the app's one table line color (docs/ui.md 8.2)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        r = option.rect
        row = index.row()
        col = index.column()
        if row != self._total_row:
            # Normal grid lines for data rows
            painter.save()
            painter.setPen(QPen(self._grid_clr, 1))
            painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
            if col < self._n_cols - 1:
                painter.drawLine(r.right(), r.top(), r.right(), r.bottom())
            painter.restore()
        else:
            # Outline border around the entire Total row
            painter.save()
            painter.setPen(QPen(self._grid_clr, 1))
            # painter.drawLine(r.left(), r.top(), r.right(), r.top())        # top
            painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())  # bottom
            if col == 0:
                painter.drawLine(r.left() + 1, r.top(), r.left() + 1, r.bottom())  # left
            if col == self._n_cols - 1:
                painter.drawLine(r.right(), r.top(), r.right(), r.bottom()) # right
            painter.restore()


class _PartialGridDelegate(QStyledItemDelegate):
    """Open position: normal grid lines for data rows, an outline border
    around the Total row, nothing on the Weight row."""

    def __init__(self, parent, footer_rows, total_row, n_cols):
        super().__init__(parent)
        self._footer_rows = footer_rows
        self._total_row = total_row
        self._n_cols = n_cols
        self._grid_clr = QColor(LINE_GROUP)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        r = option.rect
        row = index.row()
        col = index.column()
        if row not in self._footer_rows:
            # Draw normal grid lines for data rows
            painter.save()
            painter.setPen(QPen(self._grid_clr, 1))
            painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
            if col < self._n_cols - 1:
                painter.drawLine(r.right(), r.top(), r.right(), r.bottom())
            painter.restore()
        elif row == self._total_row:
            # Draw outline border around the entire Total row
            painter.save()
            painter.setPen(QPen(self._grid_clr, 1))
            # painter.drawLine(r.left(), r.top(), r.right(), r.top())       # top
            painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom()) # bottom
            if col == 0:
                painter.drawLine(r.left() + 1, r.top(), r.left() + 1, r.bottom()) # left edge
            if col == self._n_cols - 1:
                painter.drawLine(r.right(), r.top(), r.right(), r.bottom()) # right edge
            painter.restore()


# ---------------------------------------------------------------------------
# StockTradeHistoryDialog — Detailed trade history for a single stock
# ---------------------------------------------------------------------------
class StockTradeHistoryDialog(QDialog):
    def __init__(self, company, matches, total_pl, total_buy, total_sell, parent=None, is_open_position=False):
        super().__init__(parent)
        title = (tr(f"[{company}] 보유 현황", f"[{company}] Current Holdings") if is_open_position
                 else tr(f"[{company}] 상세 거래 내역", f"[{company}] Detailed Trading History"))
        self.setWindowTitle(title)
        fit_to_screen(self, 1000, 600)

        layout = QVBoxLayout(self)

        tbl = self._new_table(len(matches), is_open_position)
        tbl.setUpdatesEnabled(False)
        try:
            self._fill_data_rows(tbl, matches, is_open_position)
        finally:
            tbl.setUpdatesEnabled(True)

        # ---Total row (always present for both open and closed) ---
        total_row = len(matches)
        # With no matching trades row 0 is the total row and has no item yet
        # (it used to raise AttributeError and the dialog never opened).
        first = tbl.item(0, 0) if matches else None
        bold_font = first.font() if first is not None else tbl.font()
        bold_font.setBold(True)
        if is_open_position:
            self._fill_open_total_row(tbl, total_row, bold_font, total_pl, total_buy, total_sell)
        else:
            self._fill_closed_total_row(tbl, total_row, bold_font, total_pl, total_buy, total_sell)

        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        if not is_open_position:
            # Closed position: draw outline border around Total row via delegate.
            tbl.setShowGrid(False)
            tbl._closed_delegate = _ClosedDelegate(tbl, total_row, tbl.columnCount())
            tbl.setItemDelegate(tbl._closed_delegate)
        else:
            weight_row = total_row + 1
            self._fill_weight_row(tbl, weight_row, matches, bold_font)
            # Remove borders from footer rows (Total + Weight):
            # hide the built-in grid and re-draw only for data rows via delegate.
            tbl.setShowGrid(False)
            tbl._partial_grid_delegate = _PartialGridDelegate(tbl, {total_row, weight_row}, total_row,
                                                              tbl.columnCount())
            tbl.setItemDelegate(tbl._partial_grid_delegate)

        layout.addWidget(tbl)

        layout.addLayout(button_row(make_button(tr("닫기", "Close"), on_click=self.accept)))

        # Resizable like every other dialog (docs/ui.md 8.5); it used to be
        # pinned at 1000x600 with the scroll bar switched on or off by a
        # height estimate for that fixed size.
        tbl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        fit_to_screen(self, 1000, 600)

    # ------------------------------------------------------------- builders
    @staticmethod
    def _columns(is_open_position):
        if is_open_position:
            return [tr("매수일", "Buy Date"), tr("매수가", "Buy Price"), tr("매수 수량", "Buy Qty"), tr("매수 금액", "Buy Amount"),
                    tr("상태", "Status"), tr("현재가", "Current Price"), tr("현재 수량", "Current Qty"),
                    tr("평가 금액", "Current Amount"), tr("손익", "P/L"), tr("손익(%)", "P/L(%)")]
        return [tr("매수일", "Buy Date"), tr("매수가", "Buy Price"), tr("매수 수량", "Buy Qty"), tr("매수 금액", "Buy Amount"),
                tr("일수", "Days"), tr("매도일", "Sell Date"), tr("매도가", "Sell Price"), tr("매도 수량", "Sell Qty"),
                tr("매도 금액", "Sell Amount"), tr("손익", "P/L"), tr("손익(%)", "P/L(%)")]

    def _new_table(self, n_matches, is_open_position) -> QTableWidget:
        tbl = QTableWidget()
        cols = self._columns(is_open_position)
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        # open: data rows + total row + weight row; closed: data rows + total row
        tbl.setRowCount(n_matches + (2 if is_open_position else 1))
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        # App-wide table defaults (docs/ui.md 8.2 / 8.6): no row-number
        # column (it used to number the data rows and blank the footer rows),
        # fixed row height, table font.
        setup_table(tbl, zebra=False)
        return tbl

    @staticmethod
    def _fill_data_rows(tbl, matches, is_open_position):
        for r, rec in enumerate(matches):
            tbl.setItem(r, 0, _si(rec.get("buy_date", "")))
            tbl.setItem(r, 1, _ni(rec.get("buy_price", 0)))
            tbl.setItem(r, 2, _ni(rec.get("qty", 0)))
            tbl.setItem(r, 3, _ni(rec.get("buy_amount", 0)))

            if is_open_position:
                # Open: col4=Status, col5=CurrPrice, col6=CurrQty, col7=CurrAmt, col8=P/L, col9=P/L%
                tbl.setItem(r, 4, _si(tr("보유 중", "Open")))
                curr_price = rec.get("curr_price", 0)
                qty = rec.get("qty", 0)
                tbl.setItem(r, 5, _ni(curr_price))
                tbl.setItem(r, 6, _ni(qty))
                tbl.setItem(r, 7, _ni(curr_price * qty))
                pl_col, pct_col = 8, 9
                pl_val = rec.get("curr_pl", 0.0)
                pl_pct_val = rec.get("curr_pl_pct", 0.0)
            else:
                # Closed: col4=Days, col5=SellDate, col6=SellPrice, col7=SellQty, col8=SellAmt, col9=P/L, col10=P/L%
                d = int(rec.get("days_held", 0) or 0)
                days_it = QTableWidgetItem(str(d) if d > 0 else "-")
                days_it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                days_it.setForeground(QColor(TEXT_FAINT))
                tbl.setItem(r, 4, days_it)
                tbl.setItem(r, 5, _si(rec.get("sell_date", "")))
                tbl.setItem(r, 6, _ni(rec.get("sell_price", 0)))
                tbl.setItem(r, 7, _ni(rec.get("sell_qty", 0)))
                tbl.setItem(r, 8, _ni(rec.get("sell_amount", 0)))
                pl_col, pct_col = 9, 10
                pl_val = rec.get("pl", 0.0)
                pl_pct_val = rec.get("pl_pct", 0.0)

            tbl.setItem(r, pl_col, _color_pl(_ni(pl_val), pl_val))
            tbl.setItem(r, pct_col, _color_pl(_ni(pl_pct_val, "{:+.1f}%"), pl_pct_val))

    @staticmethod
    def _total_pct_item(bold_font, total_pl, total_buy):
        """The Total row's P/L % cell: bold and colored, blank without a buy total."""
        if total_buy > 0:
            total_pct = (total_pl / total_buy) * 100
            return _color_pl(_bold_ni(total_pct, bold_font, "{:+.1f}%"), total_pct)
        it = QTableWidgetItem("")
        it.setFont(bold_font)
        return it

    def _fill_open_total_row(self, tbl, total_row, bold_font, total_pl, total_buy, total_sell):
        # Span cols 0-2 for "Total" label
        tbl.setSpan(total_row, 0, 1, 3)
        tbl.setItem(total_row, 0, _bold_label(tr("합계", "Total"), bold_font))
        # Buy Amount (col 3)
        tbl.setItem(total_row, 3, _bold_ni(total_buy, bold_font))
        # Status col blank
        tbl.setItem(total_row, 4, QTableWidgetItem(""))
        # Current Price / Current Qty blank
        tbl.setItem(total_row, 5, QTableWidgetItem(""))
        tbl.setItem(total_row, 6, QTableWidgetItem(""))
        # Current Amount (col 7)
        tbl.setItem(total_row, 7, _bold_ni(total_sell, bold_font))
        # P/L (col 8)
        tbl.setItem(total_row, 8, _color_pl(_bold_ni(total_pl, bold_font), total_pl))
        # P/L % (col 9)
        tbl.setItem(total_row, 9, self._total_pct_item(bold_font, total_pl, total_buy))
        tbl.setVerticalHeaderItem(total_row, QTableWidgetItem(""))  # hide row number

    def _fill_closed_total_row(self, tbl, total_row, bold_font, total_pl, total_buy, total_sell):
        # Closed: 11 cols - span 0-3 for "Total", col4(Days) blank, col5-8 sell section, col9=P/L, col10=P/L%
        tbl.setSpan(total_row, 0, 1, 4)
        tbl.setItem(total_row, 0, _bold_label(tr("합계", "Total"), bold_font))
        tbl.setItem(total_row, 4, QTableWidgetItem(""))  # Days blank
        tbl.setSpan(total_row, 5, 1, 3)
        tbl.setItem(total_row, 5, QTableWidgetItem(""))
        tbl.setItem(total_row, 8, _bold_ni(total_sell, bold_font))
        tbl.setItem(total_row, 9, _color_pl(_bold_ni(total_pl, bold_font), total_pl))
        tbl.setItem(total_row, 10, self._total_pct_item(bold_font, total_pl, total_buy))
        tbl.setVerticalHeaderItem(total_row, QTableWidgetItem(""))  # hide row number

    @staticmethod
    def _fill_weight_row(tbl, weight_row, matches, bold_font):
        """Weight row: weight % in col 7 (Current Amount), all other cols blank."""
        total_w = sum(rec.get("position_w", 0.0) for rec in matches)
        for c in range(10):
            tbl.setItem(weight_row, c, QTableWidgetItem(""))
        w_it = _bold_ni(total_w, bold_font, "{:.1f}%")
        w_it.setForeground(QColor(TEXT_MUTED))
        tbl.setItem(weight_row, 7, w_it)
        tbl.setVerticalHeaderItem(weight_row, QTableWidgetItem(""))  # hide row number
