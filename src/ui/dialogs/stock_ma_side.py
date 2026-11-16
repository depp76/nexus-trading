"""ui/dialogs/stock_ma_side.py — the MA chart dialog's right panel
(ui/dialogs/stock_ma.py StockMaDialog): the investor net-buying table (KR
stocks) or the WTI futures curve, the "전략 점검" block, the 'Company Info'
button, and the geometry pass that lines the panel up with the dialog's
button row and chart.

Moved out of ui/dialogs/stock_ma.py (2026-11-15, code review #5). The dialog
still exposes the pieces under its old attribute names (_right_panel,
_right_layout, _right_title, _right_table, _right_gap, strategy_section) and
keeps _align_right_panel as a thin wrapper around StockMaSidePanel.align.
"""
from PyQt6.QtWidgets import QVBoxLayout, QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QWidget
from PyQt6.QtCore import Qt, QPoint, QObject, QEvent
from PyQt6.QtGui import QColor, QFont, QFontMetrics

from i18n import tr
from ui.strategy_check_section import StrategyCheckSection, has_section
from ui.common import create_font, setup_table, FONT_BODY
from ui.colors import PROFIT, LOSS


class _EqualColumns(QObject):
    """Keeps `cols` of `table` one width -- at least `floor`, plus an equal
    share of whatever the viewport has left -- whenever the table resizes."""

    def __init__(self, table: QTableWidget, cols: list, floor: int):
        super().__init__(table)
        self._table, self._cols, self._floor = table, cols, floor
        table.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self.apply()
        return False

    def apply(self) -> None:
        t = self._table
        fixed = sum(t.columnWidth(c) for c in range(t.columnCount()) if c not in self._cols)
        share = (t.viewport().width() - fixed) // len(self._cols)
        w = max(self._floor, share)
        for c in self._cols:
            if t.columnWidth(c) != w:
                t.setColumnWidth(c, w)


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _new_side_table(headers: list) -> QTableWidget:
    # App-wide table defaults (docs/ui.md 8.2 / 8.6): row height, font,
    # zebra; the frame and grid come from the theme. Read-only: no
    # selection.
    table = setup_table(QTableWidget())
    table.horizontalHeader().setFont(create_font(FONT_BODY, QFont.Weight.Bold))
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    return table


def _cell(text: str, align=Qt.AlignmentFlag.AlignCenter, color: str = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setTextAlignment(align)
    if color:
        it.setForeground(QColor(color))
    return it


# Horizontal room a cell needs beyond its text: ui/theme.py's
# QTableWidget::item padding (3px a side since docs/ui.md 8.2; was 8),
# the style's own text margin
# (~3px a side), plus slack for hinting/DPI differences between the
# metrics measured here and the glyphs painted later. 22px was exactly
# the sum with no slack, and a 7-digit close (1,849,000) still elided
# on a real screen.
_SIDE_CELL_PAD = 36


def _fit_side_table(table: QTableWidget, equal_cols=()) -> None:
    """Size every column to its widest header/cell text and pin the
    table's minimum width to the total, so the splitter cannot squeeze
    the side panel below what its numbers need. Net-purchase figures
    are share counts (7-8 digits, e.g. -1,234,567) -- under the old
    Stretch mode a narrow panel elided them to '1,23...'."""
    # Measure with the fonts the widget will actually paint with: the
    # global stylesheet re-resolves them at polish time.
    table.ensurePolished()
    table.horizontalHeader().ensurePolished()
    head_fm = QFontMetrics(table.horizontalHeader().font())
    cell_fm = QFontMetrics(table.font())
    total = 0
    widths = []
    for c in range(table.columnCount()):
        header = table.horizontalHeaderItem(c)
        w = head_fm.horizontalAdvance(header.text()) if header else 0
        for r in range(table.rowCount()):
            it = table.item(r, c)
            if it is not None:
                w = max(w, cell_fm.horizontalAdvance(it.text()))
        widths.append(w + _SIDE_CELL_PAD)
    # Columns that compare like with like (investor groups) share one width.
    if equal_cols:
        same = max(widths[c] for c in equal_cols)
        for c in equal_cols:
            widths[c] = same
    for c, w in enumerate(widths):
        table.setColumnWidth(c, w)
        total += w
    hdr = table.horizontalHeader()
    hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    if equal_cols:
        # The spare panel width is shared equally by those columns (the
        # last one used to take it all), never below what their numbers
        # need. Not QHeaderView.Stretch: that sizes from the table's
        # geometry alone and cut "-12,345,678" with a wider font (CI).
        hdr.setStretchLastSection(False)
        table._equal_cols = _EqualColumns(table, list(equal_cols), same)
    else:
        hdr.setStretchLastSection(True)
    scrollbar_w = table.style().pixelMetric(table.style().PixelMetric.PM_ScrollBarExtent)
    table.setMinimumWidth(total + scrollbar_w + 2 * table.frameWidth())


def _build_futures_table(rows: list) -> QTableWidget:
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    table = _new_side_table([tr("종목명", "Name"), tr("코드", "Code"), tr("가격", "Price")])
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        table.setItem(i, 0, _cell(row['Contract']))
        table.setItem(i, 1, _cell(row['Symbol']))
        table.setItem(i, 2, _cell(f"${row.get('Close', 0):,.2f}", right))
    _fit_side_table(table)
    return table


def _build_investor_table(rows: list) -> QTableWidget:
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    has_details = any(r.get('InvestmentTrust', 0) != 0 or r.get('PrivateEquity', 0) != 0 for r in rows)
    headers = [tr("날짜", "Date"), tr("종가", "Close"), tr("외국인", "Foreigner"),
               tr("기관", "Institution"), tr("개인", "Retail")]
    keys = ["Foreigner", "Institution", "Retail"]
    if has_details:
        headers.extend([tr("투신", "Inv. Trust"), tr("사모", "Private Eq.")])
        keys.extend(["InvestmentTrust", "PrivateEquity"])

    table = _new_side_table(headers)
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        table.setItem(i, 0, _cell(row['Date']))
        table.setItem(i, 1, _cell(f"{row.get('Close', 0):,}", right))
        for j, key in enumerate(keys, start=2):
            val = row.get(key, 0)
            color = PROFIT if val > 0 else (LOSS if val < 0 else None)
            table.setItem(i, j, _cell(f"{val:,}", right, color))
    # Foreigner / Institution / Retail (and the detail groups) one width,
    # so the groups read as equals (2026-11-15).
    _fit_side_table(table, equal_cols=range(2, 2 + len(keys)))
    return table


class StockMaSidePanel:
    """Builds the right panel and keeps it aligned. Not a widget itself:
    `widget` is the plain QWidget the dialog adds to its splitter (the widget
    tree is the one the dialog used to build inline)."""

    def __init__(self, ticker, market, investor_data, is_stock: bool, name: str = "", signal_ctx=None):
        self._ticker = ticker
        self._market = market
        self._name = name or ticker
        self._signal_ctx = signal_ctx
        self.strategy_section = None
        self.title = None       # first line of the panel, level with the button row
        self.table = None       # starts level with the chart's top edge
        self.gap = None         # spacer between the two, sized by align()
        self.widget, self.layout = self._build(investor_data or [], is_stock)

    def _build(self, inv: list, is_stock: bool):
        """Top to bottom (user direction 2026-11-15): the table's title level
        with the button row, the investor-flow table (KR stocks) or futures
        curve (WTI) starting level with the chart's top edge, the "전략 점검"
        block (markets some registered strategy applies to), the 'Company Info' button. `align` keeps the two alignments as
        the button row wraps or the dialog resizes."""
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        # No implicit spacing: the title-to-table distance is set by the
        # spacer alone (one-line button rows leave only a few pixels).
        v.setSpacing(0)

        is_wti_futures = bool(inv) and self._ticker == "CL=F" and 'Contract' in inv[0]
        if is_wti_futures:
            self.title = _section_label(tr("선물 가격 (최근 8개월)", "Futures Price (Last 8 Months)"))
            self.table = _build_futures_table(inv)
        elif inv:
            self.title = _section_label(tr(f"투자자별 순매수 (최근 {len(inv)}일)",
                                           f"Investor Net Buying (Last {len(inv)} Days)"))
            self.table = _build_investor_table(inv)
        if self.table is not None:
            v.addWidget(self.title)
            self.gap = QWidget()
            self.gap.setFixedHeight(0)
            v.addWidget(self.gap)
            v.addWidget(self.table)

        if has_section(self._market):
            # "전략 점검" (2026-11-16): a strategy combo and this stock's check under it.
            self.strategy_section = StrategyCheckSection(self._ticker, self._name, self._market, self._signal_ctx)
            if self.table is not None:
                v.addSpacing(12)
            else:
                self.title = self.strategy_section     # its title line goes level with the buttons
            v.addWidget(self.strategy_section)

        if is_stock:
            fin_btn = QPushButton(tr("기업 정보", "Company Info"))
            fin_btn.clicked.connect(self._open_company_page)
            v.addSpacing(10)
            v.addWidget(fin_btn)

        if not inv:
            v.addStretch()
        return panel, v

    def align(self, buttons: list, canvas: QWidget) -> None:
        """Panel's first line level with the first button line (`buttons[0]`),
        its table's top level with `canvas`'s top edge (2026-11-15). Both
        depend on the live geometry (the button row wraps on a narrow chart),
        so the dialog runs this on show, resize and splitter moves."""
        panel, v = self.widget, self.layout
        if not panel.isVisible():
            return
        if not buttons:
            return

        def y_in_panel(w, local_y=0):
            return w.mapToGlobal(QPoint(0, local_y)).y() - panel.mapToGlobal(QPoint(0, 0)).y()

        first_line_mid = y_in_panel(buttons[0], buttons[0].height() // 2)
        title = self.title
        if title is self.strategy_section and title is not None:
            title_h = title.combo.sizeHint().height()
        else:
            title_h = title.sizeHint().height() if title is not None else 0
        top = max(0, first_line_mid - title_h // 2)
        old = v.contentsMargins()
        if old.top() != top:
            v.setContentsMargins(0, top, 0, 0)
        if self.gap is not None:
            # Lay out with the new top first, then correct the spacer by what
            # is actually left between the table and the chart's top edge.
            v.activate()
            delta = y_in_panel(canvas) - y_in_panel(self.table)
            gap = max(0, self.gap.height() + delta)
            if self.gap.height() != gap:
                self.gap.setFixedHeight(gap)
                v.activate()

    def _open_company_page(self, *_):
        import webbrowser
        if self._market in ("KOSPI", "KOSDAQ"):
            url = f"https://finance.naver.com/item/main.naver?code={str(self._ticker).zfill(6)}"
        else:
            url = f"https://www.google.com/finance?q={str(self._ticker).replace('.', '-')}"
        webbrowser.open(url)
