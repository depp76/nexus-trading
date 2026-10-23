"""ui/dialogs/assets_report.py — AssetsReportDialog, the Total Assets graph's
"Report": positions, cash and total at the start and middle of every month
next to the KOSPI close (rows from ui.assets_report.build_report)."""
from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QHeaderView, QLabel,
    QPushButton, QSplitter, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui.common import create_font, FONT_SMALL, FONT_HEADING
from ui.colors import QC_PROFIT, QC_LOSS, QC_FLAT
from ui.widgets import NumericItem

_SUMMARY_HEADERS = ["Date", "KOSPI", "KOSPI Cum %", "Positions", "Cash", "Total",
                    "Positions %", "Total Cum %", "vs KOSPI (%p)"]
_HOLDING_HEADERS = ["Name", "Ticker", "Qty", "Cost", "Close", "Value", "Weight %"]
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _signed_color(v):
    if v is None:
        return QC_FLAT
    return QC_PROFIT if v > 0 else (QC_LOSS if v < 0 else QC_FLAT)


def _num(text, value, color=None):
    it = NumericItem(text, value)
    it.setTextAlignment(_RIGHT)
    if color is not None:
        it.setForeground(color)
    return it


def _pct(v):
    return _num("-" if v is None else f"{v:+.1f}%", v if v is not None else 0.0, _signed_color(v))


def _position_weight(row) -> float:
    """Positions as a share of Total (%)."""
    return row["value"] / row["total"] * 100 if row["total"] else 0.0


def _make_table(headers):
    tbl = QTableWidget(0, len(headers))
    tbl.setHorizontalHeaderLabels(headers)
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    tbl.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    tbl.setAlternatingRowColors(True)
    tbl.verticalHeader().setVisible(False)
    tbl.verticalHeader().setDefaultSectionSize(24)
    tbl.setFont(create_font(FONT_SMALL, style_name="Semilight"))
    tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return tbl


class AssetsReportDialog(QDialog):
    def __init__(self, rows: list[dict], parent=None):
        super().__init__(parent)
        self._rows = rows
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.setWindowTitle("Total Assets — Early / Mid-Month Report")
        self.resize(1100, 760)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        title = QLabel("Positions · Cash · KOSPI — first trading day and mid-month (≤ 15th)")
        title.setFont(create_font(FONT_HEADING, QFont.Weight.Bold))
        v.addWidget(title)
        note = QLabel(
            "Rebuilt from the Trading History trade log, valued at each day's close (US positions at "
            "that day's USD/KRW). Cash is rolled back from the current Deposit with cumulative "
            "Withdrawals added back (dated deposits/withdrawals are not recorded). "
            "Cumulative % is relative to the first row."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        v.addWidget(note)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.summary = _make_table(_SUMMARY_HEADERS)
        self.holdings = _make_table(_HOLDING_HEADERS)
        self.holdings.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._holdings_lbl = QLabel("")
        self._holdings_lbl.setFont(create_font(FONT_SMALL))
        lower = QWidget()
        lower_v = QVBoxLayout(lower)
        lower_v.setContentsMargins(0, 0, 0, 0)
        lower_v.addWidget(self._holdings_lbl)
        lower_v.addWidget(self.holdings)
        splitter.addWidget(self.summary)
        splitter.addWidget(lower)
        splitter.setSizes([460, 260])
        v.addWidget(splitter, 1)

        self._fill_summary()
        self.summary.itemSelectionChanged.connect(self._on_row_selected)
        if rows:
            self.summary.selectRow(len(rows) - 1)

        btns = QHBoxLayout()
        btns.addStretch()
        export_btn = QPushButton("Export CSV")
        export_btn.clicked.connect(self._export_csv)
        btns.addWidget(export_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        v.addLayout(btns)

    def _fill_summary(self):
        tbl = self.summary
        tbl.setUpdatesEnabled(False)
        try:
            tbl.setRowCount(len(self._rows))
            for r, row in enumerate(self._rows):
                date_it = NumericItem(row["date"], r)
                date_it.setData(Qt.ItemDataRole.UserRole, r)
                tbl.setItem(r, 0, date_it)
                k = row["kospi"]
                tbl.setItem(r, 1, _num(f"{k:,.2f}" if k else "-", k))
                tbl.setItem(r, 2, _pct(row["kospi_cum_pct"]))
                val_txt = f"{row['value']:,.0f}" + ("" if row["priced"] else " *")
                tbl.setItem(r, 3, _num(val_txt, row["value"]))
                tbl.setItem(r, 4, _num(f"{row['cash']:,.0f}", row["cash"]))
                tbl.setItem(r, 5, _num(f"{row['total']:,.0f}", row["total"]))
                w = _position_weight(row)
                tbl.setItem(r, 6, _num(f"{w:.1f}%", w))
                tbl.setItem(r, 7, _pct(row["total_cum_pct"]))
                tc, kc = row["total_cum_pct"], row["kospi_cum_pct"]
                tbl.setItem(r, 8, _pct(tc - kc if tc is not None and kc is not None else None))
        finally:
            tbl.setUpdatesEnabled(True)

    def _on_row_selected(self):
        sel = self.summary.selectedItems()
        if not sel:
            return
        row = self._rows[self.summary.item(sel[0].row(), 0).data(Qt.ItemDataRole.UserRole)]
        holdings = row["holdings"]
        self._holdings_lbl.setText(
            f"Holdings on {row['date']} — "
            f"{len(holdings)} position(s), positions {row['value']:,.0f} / cash {row['cash']:,.0f} KRW"
            + ("" if row["priced"] else "   (* no close found: valued at cost)")
        )
        tbl = self.holdings
        tbl.setUpdatesEnabled(False)
        try:
            tbl.setRowCount(len(holdings))
            total = row["total"] or 0.0
            for r, h in enumerate(holdings):
                tbl.setItem(r, 0, NumericItem(h["company"], r))
                tbl.setItem(r, 1, NumericItem(h["ticker"], r))
                tbl.setItem(r, 2, _num(f"{h['qty']:,.0f}", h["qty"]))
                tbl.setItem(r, 3, _num(f"{h['cost']:,.0f}", h["cost"]))
                tbl.setItem(r, 4, _num(f"{h['close']:,.0f}" if h["priced"] else "-", h["close"]))
                tbl.setItem(r, 5, _num(f"{h['value']:,.0f}" + ("" if h["priced"] else " *"), h["value"]))
                w = h["value"] / total * 100 if total else 0.0
                tbl.setItem(r, 6, _num(f"{w:.1f}%", w))
        finally:
            tbl.setUpdatesEnabled(True)

    def _export_csv(self):
        import csv
        path, _ = QFileDialog.getSaveFileName(self, "Export Report", "assets_report.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Date", "KOSPI", "KOSPI Cum %", "Positions", "Cash", "Total",
                            "Positions %", "Total Cum %", "Holdings"])
                for row in self._rows:
                    w.writerow([
                        row["date"],
                        f"{row['kospi']:.2f}", "" if row["kospi_cum_pct"] is None else f"{row['kospi_cum_pct']:.2f}",
                        f"{row['value']:.0f}", f"{row['cash']:.0f}", f"{row['total']:.0f}",
                        f"{_position_weight(row):.1f}",
                        "" if row["total_cum_pct"] is None else f"{row['total_cum_pct']:.2f}",
                        "; ".join(f"{h['company']} {h['qty']:.0f} ({h['value']:,.0f})" for h in row["holdings"]),
                    ])
        except OSError as e:
            QMessageBox.warning(self, "Export", f"Failed to save:\n{e}")
