"""ui/dialogs/strategy_trades.py — StrategyTradesDialog: the closed trades of one
Strategy › Trend Following run in a large, sortable table (docs/ui.md 8.13).

Opened from the result screen's 매매 내역 tab ("자세히 보기" or a double-click on
a row). Shows what the tab's compact list leaves out -- market, quantity, the
amounts on both sides, net P&L and costs in KRW -- with a name / ticker search
and an exit-reason filter. Prices and amounts are KRW (US names converted at
the day's USD/KRW, spec 9-25). Read-only: nothing here changes the run.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView,
)

from ui.colors import fg_for
from ui.common import make_button, setup_table, install_table_keys, make_footer_label
from ui.strategy_widgets import reason_label, fmt_price, trade_summary
from ui.widgets import NumericItem

_TITLE = "Strategy"
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter

# (header, width) -- the column order of the table.
COLUMNS = [
    ("진입일", 92), ("청산일", 92), ("종목명", 170), ("티커", 72), ("시장", 64), ("수량", 64),
    ("매입가", 90), ("매도가", 90), ("매입금액", 110), ("매도금액", 110), ("손익(원)", 100),
    ("수익률", 70), ("보유일", 60), ("비용(원)", 84), ("청산 사유", 120),
]
COL_TICKER, COL_REASON = 3, 14
ALL_REASONS = "전체 사유"


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


class StrategyTradesDialog(QDialog):
    """`trades`: backtest.Trade objects (newest exit first); `names` /
    `markets`: ticker -> company name / market (blank when unknown)."""

    def __init__(self, title: str, trades: list, names: dict | None = None, markets: dict | None = None,
                 select_ticker: str | None = None, select_exit=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"매매 내역 — {title}")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1360, 720)
        self._trades = list(trades)
        self._names = dict(names or {})
        self._markets = dict(markets or {})

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("종목명 또는 티커 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search)
        self.reason_combo = QComboBox()
        self.reason_combo.addItem(ALL_REASONS, "")
        for code in sorted({t.reason for t in self._trades}, key=reason_label):
            self.reason_combo.addItem(reason_label(code), code)
        self.reason_combo.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.reason_combo)
        bar.addStretch()
        root.addLayout(bar)

        self.table = setup_table(QTableWidget(0, len(COLUMNS)),
                                 empty_text="이 변형은 기간 중 청산한 매매가 없습니다")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setHorizontalHeaderLabels([h for h, _ in COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for c, (_, w) in enumerate(COLUMNS):
            self.table.setColumnWidth(c, w)
        install_table_keys(self.table)            # Ctrl+C copies the selected rows as TSV
        self._fill()
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)     # newest exit first
        root.addWidget(self.table, 1)

        self.summary_lbl = make_footer_label("")
        root.addWidget(self.summary_lbl)
        note = QLabel("가격·금액은 원화(미국 종목은 그날 USD/KRW로 환산). 손익은 양쪽 비용을 뺀 값이고, "
                      "비용은 매입분 몫과 매도 비용의 합입니다. 열 머리글을 누르면 정렬, Ctrl+C로 선택 행 복사.")
        note.setObjectName("faint")
        note.setWordWrap(True)
        root.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch()
        btns.addWidget(make_button("닫기", on_click=self.accept))
        root.addLayout(btns)

        self._apply_filter()
        if select_ticker:
            self.select_trade(select_ticker, select_exit)

    # ------------------------------------------------------------------ table
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
            name = self.table.item(row, 2).text()
            ok = (not text or text in ticker.lower() or text in name.lower()) and (
                not reason or self.table.item(row, COL_REASON).data(Qt.ItemDataRole.UserRole) == reason)
            self.table.setRowHidden(row, not ok)
            if ok:
                shown.append(self._row_trade(row))
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
