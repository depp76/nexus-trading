"""ui/dialogs/kis_sync.py — confirm a KIS execution import before it is saved
(Trading History's KIS 조회, docs/ui.md 8.28).

Lists every new fill (date, side, stock, qty, price, amount) with what it does
to the table (new open lot / which lots it closes / skipped), the warnings
(sells larger than the open quantity, holdings that disagree with KIS) and the
deposit read in the same call. 추가 saves the plan; 취소 saves nothing (the next
KIS 조회 brings the same fills back)."""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView

from i18n import tr
from ui.common import button_row, fit_to_screen, make_button, setup_table
from ui.table_items import NumericItem

_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter

COLUMNS = [tr("날짜", "Date"), tr("구분", "Side"), tr("종목명", "Name"), tr("코드", "Ticker"), tr("수량", "Qty"),
           tr("가격", "Price"), tr("금액", "Amount"), tr("반영", "Effect")]
SIDE_LABELS = {"buy": tr("매수", "Buy"), "sell": tr("매도", "Sell")}


def _item(text: str, align=_LEFT) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    return it


def _num(value: float, fmt: str = "{:,.0f}") -> NumericItem:
    it = NumericItem(fmt.format(value), float(value))
    it.setTextAlignment(_RIGHT)
    return it


class KisSyncDialog(QDialog):
    """`plan`: ui.kis_sync.ImportPlan; `deposit`: the KRW deposit just read."""

    def __init__(self, plan, deposit: float, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle(tr("KIS 체결 반영", "Apply KIS executions"))
        root = QVBoxLayout(self)

        n_new = sum(1 for r in plan.rows if r.applied)
        # NaN: KIS sent no readable deposit (data.collectors.kis._deposit_of).
        dep = (tr(f"예수금 {deposit:,.0f}원", f"Deposit {deposit:,.0f} KRW") if math.isfinite(deposit)
               else tr("예수금 조회 불가", "Deposit unavailable"))
        head = QLabel(tr(f"{dep} · 새 체결 {n_new}건을 Trading History에 반영합니다.",
                         f"{dep} · {n_new} new fill(s) to apply to Trading History."))
        root.addWidget(head)

        self.table = setup_table(QTableWidget(len(plan.rows), len(COLUMNS)),
                                 empty_text=tr("새 체결이 없습니다", "No new fills"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setUpdatesEnabled(False)
        try:
            for i, r in enumerate(plan.rows):
                cells = [_item(r.date, _CENTER), _item(SIDE_LABELS.get(r.side, r.side), _CENTER), _item(r.name),
                         _item(r.ticker, _CENTER), _num(r.qty), _num(r.price), _num(r.amount), _item(r.note)]
                cells[-1].setToolTip(r.note)
                for c, it in enumerate(cells):
                    if not r.applied:
                        it.setForeground(self.palette().placeholderText())
                    self.table.setItem(i, c, it)
        finally:
            self.table.setUpdatesEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        root.addWidget(self.table, 1)

        self.warn_lbl = QLabel("\n".join(f"• {w}" for w in plan.warnings))
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setVisible(bool(plan.warnings))
        root.addWidget(self.warn_lbl)

        note = QLabel(tr("금액은 가격×수량(수수료·세금 제외). 매도는 같은 종목의 먼저 산 보유분부터 청산합니다. "
                         "취소하면 아무것도 저장하지 않고, 다음 KIS 조회에서 다시 보입니다.",
                         "Amounts are price x qty (before fees and taxes). A sell closes the oldest open lots "
                         "of that stock first. Cancel saves nothing; the fills show again on the next fetch."))
        note.setObjectName("faint")
        note.setWordWrap(True)
        root.addWidget(note)

        # 취소 → 추가, right-aligned, like every edit dialog (docs/ui.md 8.17).
        if plan.applicable:
            self.apply_btn = make_button(tr("추가", "Apply"), "primary", self.accept)
            btns = button_row(make_button(tr("취소", "Cancel"), on_click=self.reject), self.apply_btn)
        else:
            self.apply_btn = None
            btns = button_row(make_button(tr("닫기", "Close"), on_click=self.reject))
        root.addLayout(btns)
        fit_to_screen(self, 900, 460)
