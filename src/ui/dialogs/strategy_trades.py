"""ui/dialogs/strategy_trades.py — the closed trades of one Strategy › Trend
Following run (docs/ui.md 8.13).

`StrategyTradesView` is the list itself -- search box, exit-reason filter, a
sortable table and a summary line -- and is what both the result screen's
매매 내역 tab and `StrategyTradesDialog` (the larger, resizable popup) show, so
the two read the same: market, quantity, the amounts on both sides, net P&L and
costs in KRW next to the dates, prices, return and holding days. Prices and
amounts are KRW (US names converted at the day's USD/KRW, spec 9-25).
Read-only: nothing here changes the run.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView,
)

from ui.colors import fg_for
from ui.common import make_button, setup_table, install_table_keys, make_footer_label
from ui.strategy_widgets import reason_label, fmt_price, trade_summary
from ui.widgets import NumericItem

_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter

# (header, minimum width) -- the column order of the table. `fit_columns` widens
# a column to its contents; the name and reason columns are capped (tooltips
# carry the full text).
COLUMNS = [
    ("진입일", 84), ("청산일", 84), ("종목명", 120), ("티커", 60), ("시장", 56), ("수량", 50),
    ("매입가", 70), ("매도가", 70), ("매입금액", 84), ("매도금액", 84), ("손익(원)", 80),
    ("수익률", 60), ("보유일", 50), ("비용(원)", 64), ("청산 사유", 90),
]
COL_NAME, COL_TICKER, COL_REASON = 2, 3, 14
_MAX_WIDTH = {COL_NAME: 200, COL_REASON: 140}
_PAD = 14                                   # cell padding beyond the text width
ALL_REASONS = "전체 사유"
EMPTY_TEXT = "이 변형은 기간 중 청산한 매매가 없습니다"


def _text(text: str, align=_LEFT, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(align)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _num(text: str, key: float, color=None) -> NumericItem:
    item = NumericItem(text, key)
    item.setTextAlignment(_RIGHT)
    if color is not None:
        item.setForeground(QBrush(QColor(color)))
    return item


class StrategyTradesView(QWidget):
    """Search + reason filter + table + summary line for a list of
    backtest.Trade objects. `on_activate(row, col)` runs on a double-click or
    Enter; `extra_widgets` go at the right end of the filter bar."""

    def __init__(self, parent=None, *, empty_text: str = EMPTY_TEXT, on_activate=None, extra_widgets=()):
        super().__init__(parent)
        self._trades: list = []
        self._names: dict = {}
        self._markets: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("종목명 또는 티커 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search)
        self.reason_combo = QComboBox()
        self.reason_combo.addItem(ALL_REASONS, "")
        self.reason_combo.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.reason_combo)
        bar.addStretch()
        for w in extra_widgets:
            bar.addWidget(w)
        root.addLayout(bar)

        self.table = setup_table(QTableWidget(0, len(COLUMNS)), empty_text=empty_text)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setHorizontalHeaderLabels([h for h, _ in COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicator(1, Qt.SortOrder.DescendingOrder)     # newest exit first
        if on_activate is not None:
            self.table.cellDoubleClicked.connect(on_activate)
        install_table_keys(self.table, on_activate=on_activate)   # Ctrl+C copies the selected rows as TSV
        root.addWidget(self.table, 1)

        self.summary_lbl = make_footer_label("")
        root.addWidget(self.summary_lbl)
        self.fit_columns()

    # ------------------------------------------------------------------ data
    def set_trades(self, trades, names: dict | None = None, markets: dict | None = None):
        """Show `trades`; the search text, the reason filter (when that reason
        still occurs) and the sort column carry over."""
        self._trades = list(trades or [])
        self._names = dict(names or {})
        self._markets = dict(markets or {})
        reason = self.reason_combo.currentData() or ""
        self.reason_combo.blockSignals(True)
        try:
            self.reason_combo.clear()
            self.reason_combo.addItem(ALL_REASONS, "")
            for code in sorted({t.reason for t in self._trades}, key=reason_label):
                self.reason_combo.addItem(reason_label(code), code)
            self.reason_combo.setCurrentIndex(max(self.reason_combo.findData(reason), 0))
        finally:
            self.reason_combo.blockSignals(False)
        header = self.table.horizontalHeader()
        col, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self._fill()
        self.table.setSortingEnabled(True)
        self.table.sortItems(col, order)
        self.fit_columns()
        self._apply_filter()

    def trades(self) -> list:
        return list(self._trades)

    def _fill(self):
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self._trades))
            for i, t in enumerate(self._trades):
                cost = t.cost.total
                buy_amt = t.entry_price * t.qty
                sell_amt = t.exit_price * t.qty
                name = self._names.get(t.ticker, "")
                cells = [
                    _num(f"{t.entry_date:%Y-%m-%d}", t.entry_date.toordinal()),
                    _num(f"{t.exit_date:%Y-%m-%d}", t.exit_date.toordinal()),
                    _text(name, tooltip=name),
                    _text(t.ticker),
                    _text(self._markets.get(t.ticker, ""), _CENTER),
                    _num(f"{t.qty:,}", t.qty),
                    _num(fmt_price(t.entry_price), t.entry_price),
                    _num(fmt_price(t.exit_price), t.exit_price),
                    _num(f"{buy_amt:,.0f}", buy_amt),
                    _num(f"{sell_amt:,.0f}", sell_amt),
                    _num(f"{t.pnl:+,.0f}", t.pnl, fg_for(t.pnl)),
                    _num(f"{t.ret_pct:+.1f}%", t.ret_pct, fg_for(t.ret_pct)),
                    _num(str(t.holding_days), t.holding_days),
                    _num(f"{cost:,.0f}", cost),
                    _text(reason_label(t.reason), tooltip=t.reason),
                ]
                cells[0].setTextAlignment(_CENTER)
                cells[1].setTextAlignment(_CENTER)
                cells[COL_REASON].setData(Qt.ItemDataRole.UserRole, t.reason)
                for c, item in enumerate(cells):
                    self.table.setItem(i, c, item)
        finally:
            self.table.setUpdatesEnabled(True)

    # ---------------------------------------------------------------- widths
    def fit_columns(self):
        """Each column as wide as its header and its widest cell (at least the
        COLUMNS minimum; the name / reason columns capped)."""
        header = self.table.horizontalHeader()
        for c, (_, w_min) in enumerate(COLUMNS):
            w = max(w_min, header.sectionSizeHint(c),
                    self.table.sizeHintForColumn(c) + _PAD if self.table.rowCount() else 0)
            self.table.setColumnWidth(c, min(w, _MAX_WIDTH.get(c, w)))

    def columns_width(self) -> int:
        """The width the table needs to show every column without a horizontal
        scroll bar (the vertical bar's room included)."""
        t = self.table
        cols = sum(t.columnWidth(c) for c in range(t.columnCount()))
        vbar = t.verticalScrollBar().sizeHint().width()
        return cols + vbar + 2 * t.frameWidth()

    # ---------------------------------------------------------------- filter
    def _row_trade(self, row: int):
        """The Trade a (possibly re-sorted) table row shows."""
        key = (self.table.item(row, COL_TICKER).text(), self.table.item(row, 0).text(),
               self.table.item(row, 1).text())
        return next((t for t in self._trades
                     if (t.ticker, f"{t.entry_date:%Y-%m-%d}", f"{t.exit_date:%Y-%m-%d}") == key), None)

    def _apply_filter(self, *_):
        text = self.search.text().strip().lower()
        reason = self.reason_combo.currentData() or ""
        shown = []
        for row in range(self.table.rowCount()):
            ticker = self.table.item(row, COL_TICKER).text()
            name = self.table.item(row, COL_NAME).text()
            ok = (not text or text in ticker.lower() or text in name.lower()) and (
                not reason or self.table.item(row, COL_REASON).data(Qt.ItemDataRole.UserRole) == reason)
            self.table.setRowHidden(row, not ok)
            if ok:
                shown.append(self._row_trade(row))
        if not self._trades:
            self.summary_lbl.setText("")
            return
        line = trade_summary([t for t in shown if t is not None])
        if len(shown) != len(self._trades):
            line = f"필터 {len(shown)}/{len(self._trades)}건 · " + line
        self.summary_lbl.setText(line)

    def visible_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]

    def select_trade(self, ticker: str, exit_date=None) -> bool:
        """Select (and scroll to) the row of `ticker`'s trade closed on `exit_date` (any of its trades without one)."""
        for row in self.visible_rows():
            if self.table.item(row, COL_TICKER).text() != ticker:
                continue
            if exit_date is not None and self.table.item(row, 1).text() != f"{exit_date:%Y-%m-%d}":
                continue
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))
            return True
        return False


class StrategyTradesDialog(QDialog):
    """The same list as the 매매 내역 tab in a larger window, opened wide
    enough for every column (no horizontal scroll bar) when the screen allows.
    `trades`: backtest.Trade objects; `names` / `markets`: ticker -> company
    name / market (blank when unknown)."""

    def __init__(self, title: str, trades: list, names: dict | None = None, markets: dict | None = None,
                 select_ticker: str | None = None, select_exit=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"매매 내역 — {title}")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)

        root = QVBoxLayout(self)
        self.view = StrategyTradesView(self)
        root.addWidget(self.view, 1)
        note = QLabel("가격·금액은 원화(미국 종목은 그날 USD/KRW로 환산). 손익은 양쪽 비용을 뺀 값이고, "
                      "비용은 매입분 몫과 매도 비용의 합입니다. 열 머리글을 누르면 정렬, Ctrl+C로 선택 행 복사.")
        note.setObjectName("faint")
        note.setWordWrap(True)
        root.addWidget(note)
        btns = QHBoxLayout()
        btns.addStretch()
        btns.addWidget(make_button("닫기", on_click=self.accept))
        root.addLayout(btns)

        # the view's parts under the names callers (and the tests) use
        self.table, self.search, self.reason_combo = self.view.table, self.view.search, self.view.reason_combo
        self.summary_lbl = self.view.summary_lbl
        self.visible_rows, self.select_trade, self._row_trade = (
            self.view.visible_rows, self.view.select_trade, self.view._row_trade)

        self.view.set_trades(trades, names, markets)
        self._fit_to_columns()
        if select_ticker:
            self.select_trade(select_ticker, select_exit)

    def wanted_width(self) -> int:
        m = self.layout().contentsMargins()
        return self.view.columns_width() + m.left() + m.right()

    def _fit_to_columns(self):
        """Open wide enough for every column, within the screen's free area."""
        screen = (self.parentWidget().screen() if self.parentWidget() is not None else None) \
            or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        w, h = self.wanted_width(), 720
        if avail is not None:
            w, h = min(w, avail.width() - 40), min(h, avail.height() - 60)
        self.resize(w, h)
