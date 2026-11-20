"""ui/dialogs/holdings_summary.py — Per-company P/L summary popup for the Trading
History tab (split out of TradingHistoryTab on 2026-09-17)."""
from i18n import tr
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel,
)
from PyQt6.QtCore import Qt

from ui.common import create_font, FONT_BODY, TABLE_ROW_H, make_button, button_row, fit_to_screen
from ui.colors import PROFIT, LOSS, QC_PROFIT, QC_LOSS, QC_FLAT
from ui.table_items import NumericItem


def _add_position(maps: dict, comp: str, buy_amt: float, eval_amt: float, pl_val: float,
                  days: int, bd: str) -> None:
    """Accumulate one record into the per-company maps."""
    maps["pl"][comp]   = maps["pl"].get(comp, 0.0)   + pl_val
    maps["buy"][comp]  = maps["buy"].get(comp, 0.0)  + buy_amt
    maps["eval"][comp] = maps["eval"].get(comp, 0.0) + eval_amt
    if days > 0:
        maps["days"].setdefault(comp, []).append(days)
    # Track earliest buy_date per company
    if bd and (comp not in maps["buy_date"] or bd < maps["buy_date"][comp]):
        maps["buy_date"][comp] = bd


def _accumulate_by_company(closed_data: list, open_data: list) -> dict:
    """Per-company totals: "pl" (total P/L), "buy" (total cost), "eval" (total
    eval amount), "days" (list of days held), "buy_date" (earliest buy_date)."""
    maps: dict[str, dict] = {"pl": {}, "buy": {}, "eval": {}, "days": {}, "buy_date": {}}

    for rec in closed_data:
        comp     = rec.get("company", "")
        buy_amt  = float(rec.get("buy_amount", 0.0))
        sell_amt = float(rec.get("sell_amount", 0.0))
        pl_val   = float(rec.get("pl", 0.0))
        days     = int(rec.get("days_held", 0) or 0)
        bd       = rec.get("buy_date", "")
        # For closed: eval = sell amount (realized value)
        _add_position(maps, comp, buy_amt, sell_amt, pl_val, days, bd)

    for rec in open_data:
        comp    = rec.get("company", "")
        buy_amt = float(rec.get("buy_amount", 0.0))
        curr_pl = float(rec.get("curr_pl", 0.0))
        # If curr_price is not yet available, unrealized P/L = 0
        if rec.get("curr_price", 0.0) <= 0:
            curr_pl = 0.0
        eval_amt = buy_amt + curr_pl
        days     = int(rec.get("curr_days", 0) or 0)
        bd       = rec.get("buy_date", "")
        _add_position(maps, comp, buy_amt, eval_amt, curr_pl, days, bd)
    return maps


def _build_summary_table(rows: list, buy_map: dict, eval_map: dict) -> QTableWidget:
    """The 5-column table (Company | Buy Amount | Value | P/L | P/L(%)),
    sorted by P/L descending and sortable by any column."""
    # 5 columns: Company | Buy Amount | Value (sell amount if closed, else current) | P/L | P/L(%)
    tbl = QTableWidget(len(rows), 5)
    tbl.setHorizontalHeaderLabels([tr("종목", "Name"), tr("매수 금액", "Buy Amount"), tr("평가액", "Value"),
                                   tr("손익", "P/L"), tr("손익(%)", "P/L(%)")])
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    tbl.setAlternatingRowColors(True)
    tbl.verticalHeader().setVisible(False)
    tbl.setShowGrid(True)   # table/header chrome comes from ui/theme.py's global QSS
    tbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
    tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for c in range(1, 5):
        tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
    tbl.verticalHeader().setDefaultSectionSize(TABLE_ROW_H)

    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    def _ri(text, value, color=None):
        it = NumericItem(text, value)
        it.setTextAlignment(right)
        if color:
            it.setForeground(color)
        return it

    tbl.setSortingEnabled(False)
    tbl.setUpdatesEnabled(False)
    try:
        for r, (comp, pl) in enumerate(rows):
            cost    = buy_map.get(comp, 0.0)
            eval_v  = eval_map.get(comp, 0.0)
            pct     = (pl / cost * 100) if cost > 0 else 0.0
            pl_col  = QC_PROFIT if pl > 0 else (QC_LOSS if pl < 0 else QC_FLAT)

            comp_it = QTableWidgetItem(comp)
            comp_it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(r, 0, comp_it)
            tbl.setItem(r, 1, _ri(f"{cost:,.0f}", cost))
            tbl.setItem(r, 2, _ri(f"{eval_v:,.0f}", eval_v))
            tbl.setItem(r, 3, _ri(f"{pl:+,.0f}", pl, pl_col))
            tbl.setItem(r, 4, _ri(f"{pct:+.1f}%", pct, pl_col))
    finally:
        tbl.setUpdatesEnabled(True)
    # Indicator first, then enable: setSortingEnabled(True) sorts by whatever
    # the header shows at that moment (same rule as the Total Assets table).
    tbl.horizontalHeader().setSortIndicator(3, Qt.SortOrder.DescendingOrder)
    tbl.setSortingEnabled(True)
    return tbl


def _build_subtotal_label(pl_map: dict) -> QLabel:
    """Bottom summary panel: total profit and total loss across companies."""
    pos_pl = sum(v2 for v2 in pl_map.values() if v2 > 0)
    neg_pl = sum(v2 for v2 in pl_map.values() if v2 < 0)

    def _html_val(val, positive=True):
        color = PROFIT if positive else LOSS
        sign  = "+" if positive else ""
        return f"<b style='color:{color}'>{sign}{val:,.0f} KRW</b>"

    subtotal_html = (
        tr("(+) 총 이익:  ", "(+) Total Profit:  ") + _html_val(pos_pl, positive=True)
        + "&nbsp;&nbsp;&nbsp;&nbsp;"
        + tr("(-) 총 손실:  ", "(-) Total Loss:  ") + _html_val(neg_pl, positive=False)
    )
    subtotal_lbl = QLabel(subtotal_html)
    subtotal_lbl.setTextFormat(Qt.TextFormat.RichText)
    subtotal_lbl.setStyleSheet("padding:0px 4px 4px 4px;")
    subtotal_lbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
    return subtotal_lbl


def build_holdings_summary(parent, closed_data: list, open_data: list):
    """The Holdings Summary dialog (total P/L per company: closed realized +
    open unrealized), or None when there is nothing to show. Sortable by any
    column; numeric columns sort by value (NumericItem), default P/L desc."""
    # ---Accumulate per-company: buy amount, eval amount, P/L, days, buy_date ---
    maps = _accumulate_by_company(closed_data, open_data)
    pl_map = maps["pl"]
    if not pl_map:
        return None

    rows = sorted(pl_map.items(), key=lambda x: x[1], reverse=True)

    # ---Build dialog ---
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("종목별 요약 - 종목별 손익", "Holdings Summary - P/L by Company"))
    fit_to_screen(dlg, 800, min(100 + 28 * len(rows) + 130, 780))

    v = QVBoxLayout(dlg)
    v.setContentsMargins(12, 10, 12, 10)
    v.setSpacing(8)

    tbl = _build_summary_table(rows, maps["buy"], maps["eval"])
    dlg.table = tbl
    v.addWidget(tbl, 1)

    v.addWidget(_build_subtotal_label(pl_map))

    v.addLayout(button_row(make_button(tr("닫기", "Close"), on_click=dlg.accept)))
    return dlg


def show_holdings_summary(parent, closed_data: list, open_data: list) -> bool:
    """Modal wrapper around build_holdings_summary(). Returns False when there
    is nothing to show; the caller reports that on the status bar (docs/ui.md
    8.5: no modal box for information)."""
    dlg = build_holdings_summary(parent, closed_data, open_data)
    if dlg is None:
        return False
    dlg.exec()
    return True
