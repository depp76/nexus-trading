"""ui/universe_table.py — StockTable, the Trading Universe watchlist table
(docs/ui.md 2): frozen identity column, group dividers, row actions, sort
state that survives reloads, incremental row updates and the toolbar /
range filters.

Moved out of ui/widgets.py (2026-11-15, code review #5); its column spec lives
in ui/universe_columns.py, its header in ui/table_headers.py and its cell
painters in ui/delegates.py. ui.widgets still re-exports StockTable.
"""
from PyQt6.QtWidgets import (
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QApplication, QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QRect, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QKeySequence

from i18n import tr
from ui.common import create_font, FONT_BODY, _EmptyHint
from ui.delegates import IdentityDelegate, RangeBarDelegate, TrendDelegate
from ui.universe_views import matches as view_matches
from ui.colors import fg_for, heatmap_bg, PROFIT, LOSS, FLAT, OK, WARN
from ui.theme import TEXT, TEXT_EMPTY, LINE_GROUP
from ui.table_items import NumericItem
from ui.filter_popups import FilterPopup
from ui.table_headers import FilterableHeader
from ui.universe_columns import (
    COLUMNS, COL_IDENTITY, COL_CAP, COL_TPER, COL_FPER, COL_PRICE, COL_CHG, COL_RANGE52W,
    COL_MA20DIV, COL_MA60DIV, COL_TREND, GROUP_START_COLS, GROUP_LINE_W,
    _RANGE_FILTER_COLS, _MOMENTUM_COLS, _market_label, _fmt_change, _change_bg,
)

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _price_value_and_text(item, mode):
    """(raw price, display text) for the Price cell: USD rows show their
    dollar price, yields a percent level, index/level rows two decimals and
    KRW equities whole won."""
    is_index = bool(item.get('is_index'))
    is_usd = (item.get('currency', '') == '$' and 'usd_price' in item)
    price_raw = float(item.get('usd_price', 0) if is_usd else item.get('price', 0))
    if mode == 'bp':
        price_text = f"{price_raw:.2f}%"
    elif is_usd:
        price_text = f"${price_raw:,.2f}"
    elif is_index or mode == 'abs':
        price_text = f"{price_raw:,.2f}"
    else:
        price_text = f"{int(price_raw):,}"
    return price_raw, price_text


def _usd_market_cap(item) -> float:
    """A US row's market cap in USD: `usd_market_cap`, else (rows cached before
    2026-11-18) the KRW cap converted back at the row's own price ratio."""
    usd = item.get('usd_market_cap')
    if usd is not None:
        return float(usd or 0)
    price, usd_price = float(item.get('price', 0) or 0), float(item.get('usd_price', 0) or 0)
    if price <= 0 or usd_price <= 0:
        return 0.0
    return float(item.get('market_cap', 0) or 0) * usd_price / price


def _fmt_usd_cap(usd: float) -> str:
    """Always billions with one decimal ($3,410.0B / $512.3B / $0.9B; user
    direction 2026-11-18, docs/ui.md 8.31), so the digits line up and compare
    like the Korean rows' whole 억 (was $3.41T / $512.3B / $850M)."""
    return f"${usd / 1e9:,.1f}B"


class _MarketCapItem(NumericItem):
    """The 시총 cell (2026-11-18, user direction; docs/ui.md 8.31). Sorting by
    cap with Korean and US rows on screen gives two blocks -- the Korean rows by
    their KRW cap, then the US rows by their USD cap -- in either direction,
    instead of one mixed list on the KRW-converted value. `_sort_key` is the
    cap in the row's own currency (KRW 억 / USD); `_filter_value` keeps the KRW
    cap in 억 for the 시총 range filter (one unit for every row, row_values).

    Qt sorts descending by calling `right < left`, so the block order is fixed
    by reading the header's current direction (every sort of this table goes
    through the header indicator: header clicks, load_data, setSortingEnabled)."""

    def __init__(self, text: str, value, group: int, krw_eok):
        super().__init__(text, value)
        self._group = group               # 0 Korean, 1 US
        self._filter_value = krw_eok

    def __lt__(self, other):
        other_group = getattr(other, "_group", None)
        if other_group is None or other_group == self._group:
            return super().__lt__(other)
        table = self.tableWidget()
        descending = (table is not None and
                      table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder)
        return self._group > other_group if descending else self._group < other_group


def _empty_numeric_item():
    """A centred, muted "-" cell that sorts below every real value (a missing
    cap, PER or MA divergence)."""
    cell = NumericItem("-", float('-inf'))
    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    cell.setForeground(QColor(TEXT_EMPTY))
    return cell



# ---------------------------------------------------------------------------
# StockTable  — main universe table with filters and action buttons
# ---------------------------------------------------------------------------
class StockTable(QTableWidget):
    """Trading Universe watchlist table (docs/ui.md 2). Holds every
    watchlist row -- equities plus the index/yield/commodity rows, which
    format their price and changes by change_mode (see _fmt_change).

    Per-row actions used to be three QPushButtons wired via
    add_action_buttons()/setCellWidget(); that's gone (docs/ui.md's own
    issue #7: 900 button widgets for 300 rows, always-visible Delete risking
    misclicks). What replaced it:
      - status button: painted inline by IdentityDelegate at one fixed size
        (blank / Port / Target); a click on it cycles the status
        (toggle_requested, user direction 2026-09-29), as does the context
        menu entry
      - MA chart: double-click or Enter/Return on a row (ma_chart_requested)
      - Delete: context menu, still confirmed by the caller (delete_requested)
      - AI Stock Report: context menu (ai_report_requested, unchanged)
      - Ctrl+C: copies the selected rows as TSV
    """
    col_filter_changed = pyqtSignal()  # emitted when any column filter changes
    user_sorted = pyqtSignal()         # a header click chose a sort column (not load_data's own re-apply)
    ai_report_requested = pyqtSignal(str)  # emitted with the row's ticker (roadmap 2-1)
    ma_chart_requested = pyqtSignal(str)   # emitted with the row's ticker
    delete_requested = pyqtSignal(str)     # emitted with the row's ticker
    toggle_requested = pyqtSignal(str)     # emitted with the row's ticker

    # 28 px, not the app-wide TABLE_ROW_H (24): the 52W Range cell draws two
    # lines -- the bar, then the low / % / high labels under it -- which do
    # not fit in 24 px (user report 2026-10-25; docs/ui.md 8.2 exception).
    ROW_HEIGHT = 28

    def __init__(self):
        super().__init__()
        self.setColumnCount(len(COLUMNS))
        self.setHorizontalHeaderLabels([c.label for c in COLUMNS])
        # Table font: Malgun Gothic Semilight 9pt (set appropriate size to prevent text cutoff)
        self.setFont(create_font(FONT_BODY, style_name="Semilight"))
        # Same family as text cells (one app font); built once per table
        # instance rather than per cell.
        self._numeric_font = create_font(FONT_BODY, style_name="Semilight")
        # Frame, grid and header lines come from ui/theme.py's table rules
        # (docs/ui.md 8.2). Only the right side differs: paintEvent draws it,
        # because the last column's own grid line already sits on the
        # viewport's last pixel and a frame border there would make 2 px.
        self.setStyleSheet("QTableWidget { border-right: none; }")
        self._empty_hint = _EmptyHint(self, tr("아직 불러온 종목이 없습니다 -- 새로고침으로 유니버스를 받습니다",
                                                  "No stocks loaded yet -- Refresh fetches the universe"))
        self._identity_delegate = IdentityDelegate(self)
        self._range_delegate = RangeBarDelegate(self)
        self._trend_delegate = TrendDelegate(self)
        self.setItemDelegateForColumn(COL_IDENTITY, self._identity_delegate)
        self.setItemDelegateForColumn(COL_RANGE52W, self._range_delegate)
        self.setItemDelegateForColumn(COL_TREND, self._trend_delegate)
        # Use filterable header
        self._filter_header = FilterableHeader(Qt.Orientation.Horizontal, self)
        self._filter_header.divider_cols = GROUP_START_COLS
        self._filter_header.right_edge = True
        self._filter_header.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        self._filter_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._filter_header.filter_requested.connect(self._show_filter_popup)
        self.setHorizontalHeader(self._filter_header)
        # Sort state the user last chose, so load_data() can reapply it after
        # a full reload instead of always resetting to insertion order
        # (docs/ui.md 1.8/1.9). -1 means "no sort / insertion order".
        self._sort_col = -1
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._loading = False    # True inside load_data, whose indicator re-apply is not a user sort
        self._filter_header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        self._col_filters = {}  # col_index -> frozenset | None
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # docs/ui.md 1.8: row selection + keyboard navigation + Ctrl+C, in
        # place of the old NoSelection/NoFocus (which blocked both).
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        # docs/ui.md 1.5: don't force-fit columns into the viewport -- once
        # min-widths exceed it, a real horizontal scrollbar should appear
        # instead of silently shrinking every column below its floor.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Vertical bar always shown so the viewport width -- and with it every
        # flex column's width -- does not change when search / status /
        # market filters show fewer rows than fit (2026-11-15).
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.doubleClicked.connect(self._on_row_activated)

        self._build_frozen_column()
        # A manual header drag on column 0 bypasses _stretch_columns, so the
        # overlay needs its own hook to stay the same width as the column.
        self._filter_header.sectionResized.connect(self._on_section_resized)
        self.verticalHeader().setDefaultSectionSize(self.ROW_HEIGHT)
        self._frozen.verticalHeader().setDefaultSectionSize(self.ROW_HEIGHT)

    def paintEvent(self, event):
        """Default cell painting, then a vertical divider on the left edge of
        each column group (GROUP_START_COLS). Drawn over the viewport so it
        covers delegate-painted and plain cells alike and follows horizontal
        scrolling via columnViewportPosition."""
        super().paintEvent(event)
        vp = self.viewport()
        painter = QPainter(vp)
        h = vp.height()
        for col in GROUP_START_COLS:
            if self.isColumnHidden(col):
                continue
            x = self.columnViewportPosition(col)
            if 0 <= x < vp.width():
                # The grid line sits on the previous column's last pixel
                # (x - 1); the divider replaces it there.
                painter.fillRect(x - GROUP_LINE_W + GROUP_LINE_W // 2, 0, GROUP_LINE_W, h, QColor(LINE_GROUP))
        # Right outer edge: one pixel on the viewport's last column, which is
        # also where the last column's grid line falls when the columns fill
        # the viewport (so the two coincide rather than stacking).
        painter.fillRect(vp.width() - 1, 0, 1, h, QColor(LINE_GROUP))
        painter.end()

    def _build_frozen_column(self):
        """Pins the Name column on horizontal scroll (docs/ui.md 1.5:
        identifier column stays visible) via a second, borderless
        QTableWidget mirroring column 0's content, overlaid on the main
        viewport's left edge. QTableWidget has no shared-model
        frozen-column support the way QTableView does, so this duplicates
        Qt's "Frozen Column Example" pattern at the item level.

        It covers only the data rows, not the header: the header cell
        (sort click, filter-icon painting) is left alone rather than adding
        a second click-passthrough layer to reason about, so clicking
        "Name" to sort still works exactly as before.
        """
        self._frozen = QTableWidget(self)
        self._frozen.setColumnCount(1)
        self._frozen.horizontalHeader().setVisible(False)
        self._frozen.verticalHeader().setVisible(False)
        self._frozen.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._frozen.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._frozen.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._frozen.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frozen.setAlternatingRowColors(True)
        self._frozen.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._frozen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._frozen.setFont(self.font())
        self._frozen.setFrameShape(QFrame.Shape.NoFrame)
        self._frozen.setStyleSheet(f"QTableWidget {{ border: none; border-right: 1px solid {LINE_GROUP}; }}")
        # Purely a visual mirror -- never the target of a click/hover, so
        # every real interaction (row selection, context menu, sort) still
        # reaches the main table underneath it.
        self._frozen.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._frozen.setItemDelegateForColumn(0, self._identity_delegate)
        self.verticalScrollBar().valueChanged.connect(self._frozen.verticalScrollBar().setValue)

    def _reposition_frozen(self):
        header_h = self.horizontalHeader().height()
        col_w = self.columnWidth(0)
        self._frozen.setGeometry(
            self.frameWidth(),
            header_h + self.frameWidth(),
            col_w,
            self.viewport().height(),
        )
        # The overlay's own column must track the main table's column 0
        # width too; left at QTableWidget's 100px default, the overlay paints
        # the identity cell at 100px and blank viewport for the rest of its
        # width -- which reads as a gap between Name and Price.
        self._frozen.setColumnWidth(0, col_w)
        self._frozen.raise_()

    def _on_section_resized(self, logical, _old, _new):
        if logical == 0:
            self._reposition_frozen()

    def _sync_frozen_column(self):
        """Mirror column 0's cell content and row-hidden state into the
        frozen overlay. Called after anything that reorders or (un)hides
        rows -- load_data()'s sort reapply, apply_col_filters()'s row-hide
        pass and update_changed_rows() (its setSortingEnabled(True) re-sorts
        by the current indicator without emitting sortIndicatorChanged, so a
        price change under a Price/Chg sort moves rows the overlay would
        otherwise not follow). IdentityDelegate reads Qt.ItemDataRole.UserRole,
        so that has to be copied along with the display text."""
        if self._frozen.rowCount() != self.rowCount():
            self._frozen.setRowCount(self.rowCount())
        for row in range(self.rowCount()):
            src = self.item(row, 0)
            if src is None:
                continue
            mirror = QTableWidgetItem(src.text())
            mirror.setTextAlignment(src.textAlignment())
            mirror.setData(Qt.ItemDataRole.UserRole, src.data(Qt.ItemDataRole.UserRole))
            self._frozen.setItem(row, 0, mirror)
            self._frozen.setRowHidden(row, self.isRowHidden(row))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Guard against re-entrant calls: setColumnWidth() inside _stretch_columns
        # triggers another resizeEvent, which would create an infinite feedback loop
        # causing the viewport to oscillate left/right during scrolling.
        if getattr(self, '_resizing', False):
            return
        self._resizing = True
        try:
            self._stretch_columns()
        finally:
            self._resizing = False

    def showEvent(self, event):
        super().showEvent(event)
        self._stretch_columns()

    def _status_button_ticker_at(self, pos):
        """Ticker whose identity-cell status button is under `pos` (viewport
        coordinates), else None. x < columnWidth(0) is column 0 whatever the
        horizontal scroll: the frozen overlay paints column 0 there and is
        transparent to mouse events, so this is what the user sees clicked."""
        col_w = self.columnWidth(COL_IDENTITY)
        if pos.x() >= col_w:
            return None
        row = self.rowAt(pos.y())
        if row < 0:
            return None
        cell = QRect(0, self.rowViewportPosition(row), col_w, self.rowHeight(row))
        if not IdentityDelegate.status_button_rect(cell).contains(pos):
            return None
        data = self._row_identity(row)
        return data["ticker"] if data else None

    def mousePressEvent(self, event):
        """A left click on a row's status button cycles that row's status
        (toggle_requested); every other click behaves as before."""
        if event.button() == Qt.MouseButton.LeftButton:
            ticker = self._status_button_ticker_at(event.pos())
            if ticker is not None:
                self.toggle_requested.emit(ticker)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Two quick clicks on the status button advance it twice instead of
        opening the MA chart (the second press arrives as this event)."""
        if event.button() == Qt.MouseButton.LeftButton:
            ticker = self._status_button_ticker_at(event.pos())
            if ticker is not None:
                self.toggle_requested.emit(ticker)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _row_identity(self, row):
        """The (ticker, status) UserRole payload for a view row, or None.
        Reads it straight from the clicked/activated row's own cell rather
        than an index into the data list passed to load_data(), so it stays
        correct after the user re-sorts a column (setSortingEnabled(True)
        means visual row order != load order)."""
        item = self.item(row, COL_IDENTITY)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data or not data.get("ticker"):
            return None
        return data

    def contextMenuEvent(self, event):
        """Right-click menu (docs/ui.md 1.8): the per-row action buttons
        this table used to have (Tg/MA/Del) live here now, plus the
        pre-existing AI Stock Report action."""
        row = self.rowAt(event.pos().y())
        if row < 0:
            return
        data = self._row_identity(row)
        if data is None:
            return
        ticker = data["ticker"]

        menu = QMenu(self)
        status = data.get("status", "-")
        toggle_label = {"On": tr("관심으로 표시", "Set as Target"),
                        "Tg": tr("보유/관심 해제", "Clear Held/Target")}.get(status, tr("보유로 표시", "Mark as Held"))
        toggle_action = menu.addAction(toggle_label)
        ma_action = menu.addAction(tr("이평 차트 보기", "View MA Chart"))
        report_action = menu.addAction(tr("AI 종목 리포트", "AI Stock Report"))
        menu.addSeparator()
        delete_action = menu.addAction(tr("유니버스에서 제거...", "Remove from Universe..."))

        chosen = menu.exec(event.globalPos())
        if chosen is toggle_action:
            self.toggle_requested.emit(ticker)
        elif chosen is ma_action:
            self.ma_chart_requested.emit(ticker)
        elif chosen is report_action:
            self.ai_report_requested.emit(ticker)
        elif chosen is delete_action:
            self.delete_requested.emit(ticker)

    def _on_row_activated(self, index):
        """Double-click opens the MA chart (docs/ui.md 1.8), replacing the
        old per-row 📈 button."""
        data = self._row_identity(index.row())
        if data is not None:
            self.ma_chart_requested.emit(data["ticker"])
        # No selection / current cell / focus left behind by the double-click
        # (user direction 2026-11-17). Deferred so the double-click's own
        # release cannot reselect the row. Enter keeps them (keyboard use).
        QTimer.singleShot(0, self._drop_focus)

    def _drop_focus(self):
        """Clear the selection and current cell and hand keyboard focus to the
        parent (the Universe tab, given ClickFocus): with no focus widget the
        window would re-focus the table on reactivation (the MA chart
        closing), and Qt then makes the first row current again."""
        try:
            self.clearSelection()
            self.setCurrentIndex(self.model().index(-1, -1))
            parent = self.parentWidget()
            if parent is None:
                self.clearFocus()
                return
            if parent.focusPolicy() == Qt.FocusPolicy.NoFocus:
                parent.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            parent.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:        # the table is already gone
            return

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection_tsv()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = self.currentRow()
            data = self._row_identity(row) if row >= 0 else None
            if data is not None:
                self.ma_chart_requested.emit(data["ticker"])
                return
        super().keyPressEvent(event)

    def _copy_selection_tsv(self):
        """Ctrl+C: copy the selected rows as tab-separated text, one line
        per row, in on-screen column order (docs/ui.md 1.8)."""
        rows = sorted({idx.row() for idx in self.selectedIndexes()})
        if not rows:
            return
        lines = []
        for row in rows:
            identity = self._row_identity(row) or {}
            cells = [identity.get("name", ""), identity.get("meta", "")]
            for col in range(1, self.columnCount()):
                if self.isColumnHidden(col):
                    continue
                item = self.item(row, col)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def _stretch_columns(self):
        """Assigns every visible column's width from COLUMNS: fixed columns
        (weight=None) get their min_width outright; flexible columns share
        the remaining viewport width by weight, floored at their own
        min_width (docs/ui.md 1.5) instead of one flat constant. Once the
        min-width floor of the visible columns exceeds the viewport, widths
        simply stop shrinking below it and a horizontal scrollbar appears
        (ScrollBarAsNeeded) rather than columns being squeezed unreadable.
        Hidden columns (docs/ui.md 2.5 group toggle) are excluded from the
        budget entirely, so hiding a group hands its space to what remains.
        """
        if self.rowCount() == 0:
            return

        # Full viewport width: the old 0.99 factor left a ~1% empty strip at
        # the right edge on every width. The re-entrancy guards (_resizing,
        # _last_col_widths) are what actually stop resize feedback loops.
        vp_w = self.viewport().width()
        if vp_w <= 0:
            return

        fixed_total = 0
        flex_specs = []  # (col, spec) for visible, flexible columns
        for col, spec in enumerate(COLUMNS):
            if self.isColumnHidden(col):
                continue
            if spec.weight is None:
                fixed_total += spec.min_width
            else:
                flex_specs.append((col, spec))

        flex_w = vp_w - fixed_total
        total_weight = sum(spec.weight for _, spec in flex_specs) or 1.0
        unit = flex_w / total_weight if flex_specs else 0.0

        new_widths = {}
        for col, spec in enumerate(COLUMNS):
            if spec.weight is None:
                new_widths[col] = spec.min_width
        for col, spec in flex_specs:
            new_widths[col] = max(spec.min_width, int(spec.weight * unit))

        # Rounding residual - absorb into the identity column (index 0),
        # when visible, using exact arithmetic to prevent per-frame pixel
        # drift that causes the viewport to oscillate.
        if not self.isColumnHidden(0):
            used = sum(w for c, w in new_widths.items() if not self.isColumnHidden(c))
            residual = vp_w - used
            new_widths[0] = max(COLUMNS[0].min_width, new_widths[0] + residual)

        # Skip redundant setColumnWidth calls when nothing has changed.
        # Each setColumnWidth() fires an internal geometry update that can
        # re-trigger resizeEvent, so avoiding no-op calls breaks the loop.
        if getattr(self, '_last_col_widths', None) != new_widths:
            self._last_col_widths = new_widths
            for col, w in new_widths.items():
                self.setColumnWidth(col, w)

        self._reposition_frozen()

    def _on_sort_indicator_changed(self, col, order):
        self._sort_col = col
        self._sort_order = order
        if col >= 0 and not self._loading:
            self.user_sorted.emit()
        # A live header click reaches here through Qt's own internal
        # sortIndicatorChanged handling, not through load_data()/
        # apply_col_filters(), so the frozen overlay needs its own resync
        # hook for this path. Whether Qt's own reorder has already run by
        # the time this slot fires depends on connection order, which shifts
        # every time load_data() cycles setSortingEnabled() (confirmed by
        # trial: it's fired both before and after depending on how many
        # reloads preceded it) -- not something to rely on. Deferring to the
        # next event-loop tick sidesteps that entirely: by then every slot
        # for this emission, in whatever order, has finished.
        QTimer.singleShot(0, self._sync_frozen_column)

    def reset_sort(self):
        """Forget the user's column sort so the next load_data() shows the
        data order again (the Universe tab pins its Trend Score rows that way)."""
        self._sort_col = -1
        self._sort_order = Qt.SortOrder.AscendingOrder

    def load_data(self, data, highlights=None):
        if highlights is None:
            highlights = {}

        self.setUpdatesEnabled(False)  # UI Batch Repaint Optimization
        self._loading = True
        try:
            self.setSortingEnabled(False)
            self.clearContents()
            if self.rowCount() != len(data):
                self.setRowCount(len(data))

            for row, item in enumerate(data):
                self._populate_row(row, item, highlights)

            # MA20 buttons are added externally after load_data via add_ma20_button
            # Reapply whatever sort the user last chose (docs/ui.md 1.8: sort
            # state must survive a data refresh) instead of always resetting
            # to insertion order (Index-KOSPI-KOSDAQ-NASDAQ-S&P500, by market
            # cap) -- that reset is still what happens the first time, since
            # _sort_col starts at -1. setSortingEnabled(True) internally
            # triggers Qt's sortByColumn() using whatever the header's
            # indicator is at that moment, so the indicator must be
            # positioned BEFORE re-enabling sorting, not after.
            self._filter_header.setSortIndicator(self._sort_col, self._sort_order)
            self.setSortingEnabled(True)
            self._stretch_columns()
            self._sync_frozen_column()
        finally:
            self._loading = False
            self.setUpdatesEnabled(True)

    def _ticker_to_row(self) -> dict:
        """Ticker -> current view row, read fresh off the identity column's
        UserRole payload.

        Built on demand rather than incrementally maintained: with
        setSortingEnabled(True), a header click reorders items via Qt's own
        internal sort machinery, and there is no documented, reliable hook to
        patch an incremental cache in lock-step with that reorder. A fresh
        scan costs one QTableWidgetItem.data() read per row (a few hundred
        for this table) -- negligible next to the cell rebuild it's guarding,
        so it can never go stale.
        """
        mapping = {}
        for row in range(self.rowCount()):
            data = self._row_identity(row)
            if data is not None:
                mapping[data["ticker"]] = row
        return mapping

    def update_row_status(self, ticker: str, status: str):
        """Repaints just the identity cell's badge/marker for one ticker
        (docs/ui.md 1.7), in place of the old add_action_buttons() call that
        used to rebuild the Tg/MA/Del cell widgets after a status toggle."""
        row = self._ticker_to_row().get(ticker)
        if row is None:
            return
        item = self.item(row, COL_IDENTITY)
        if item is None:
            return
        data = dict(item.data(Qt.ItemDataRole.UserRole) or {})
        data["status"] = status
        item.setData(Qt.ItemDataRole.UserRole, data)
        self.viewport().update()
        self._sync_frozen_column()

    def update_changed_rows(self, data, changed_rows, highlights=None):
        """Incremental counterpart to load_data(): re-renders only the rows
        named in `changed_rows` (indices into `data`), instead of rebuilding
        every row's ~20 columns from scratch.

        Used by the 60s lightweight auto-refresh (UniverseLightweightFetchThread),
        which only ever changes price/changes for a subset of tickers -- name,
        market, ticker, market cap and PER never change on that path. Rebuilding
        every cell of every row on every tick regardless (as load_data() does)
        means tens of thousands of QTableWidgetItem allocations a minute for a
        few-hundred-row universe, for cells whose displayed value didn't move.

        `changed_rows` indices are positions in `data`, NOT view rows: with
        setSortingEnabled(True), a user sort reorders the displayed rows, so
        data index != screen row (docs/ui.md 1.9). Each changed item is
        therefore looked up by ticker to find where it currently sits on
        screen, instead of writing straight into `data`'s row number.
        """
        if not changed_rows:
            return
        if highlights is None:
            highlights = {}

        row_of_ticker = self._ticker_to_row()

        self.setUpdatesEnabled(False)
        try:
            self.setSortingEnabled(False)
            for data_idx in changed_rows:
                if not (0 <= data_idx < len(data)):
                    continue
                item = data[data_idx]
                view_row = row_of_ticker.get(item.get('ticker'))
                if view_row is None or not (0 <= view_row < self.rowCount()):
                    continue
                self._populate_row(view_row, item, highlights)
            self.setSortingEnabled(True)
            self._sync_frozen_column()
        finally:
            self.setUpdatesEnabled(True)

    def _populate_row(self, row, item, highlights):
        """Renders every cell of one row from the redesigned 15-column spec
        (docs/ui.md 2.2). Index/yield/commodity rows share the table with
        equities; change_mode picks the price/change formatting and the
        cap/PER cells show "-" where the concept does not apply."""
        mode = item.get('change_mode', 'pct')       # 'pct' | 'bp' (yields) | 'abs' (VIX/WTI levels)
        price_raw, price_text = _price_value_and_text(item, mode)
        changes = item.get('changes', {})

        self._set_identity_cell(row, item, highlights, mode)
        self._set_price_cells(row, price_text, price_raw, changes, mode)
        self._set_cap_cell(row, item)
        self._set_range_cell(row, price_raw, changes)
        self._set_per_cells(row, item)
        self._set_ma_div_cell(row, COL_MA20DIV, changes.get("ma20_div"))
        # COL_MA60DIV: MA60 Div (MA50 Div re-added 2026-09-22, moved to MA60 on 2026-11-18 with the
        # strategy's trend line; price/MA60*100, mirrors the MA20 Div cell). A cached row from before
        # has no "ma60_div" and shows "-" until the refresh recomputes it.
        self._set_ma_div_cell(row, COL_MA60DIV, changes.get("ma60_div"))
        trend_points = self._set_momentum_cells(row, changes, mode)
        self._set_trend_cell(row, trend_points)

    def _set_identity_cell(self, row, item, highlights, mode):
        """col 0: identity (name + "ticker · market" meta + status badge) --
        see IdentityDelegate. Sorts by name (DisplayRole), same as the old
        dedicated Name column did."""
        ticker = item.get('ticker', '')
        status = highlights.get(ticker, "-")
        identity_item = QTableWidgetItem(item.get('name', ticker))
        identity_item.setData(Qt.ItemDataRole.UserRole, {
            "ticker": ticker,
            "name": item.get('name', ticker),
            "market": item.get('market', ''),
            # A pinned 전략 신호 row shows only its short tag ("A0 상위 3") next
            # to the name, so the name keeps the room (user direction
            # 2026-11-16); ticker and market move to the tooltip.
            "meta": item['signal_tag'] if item.get('signal_tag')
                    else f"{ticker} · {_market_label(item.get('market', ''))}",
            "status": status,
            "mode": mode,
        })
        if item.get('signal_tag'):
            # The pinned row's ticker, market and figure (RS, 52w-high
            # proximity or weekly score) on the tooltip (2026-11-16).
            tip = f"{ticker} · {_market_label(item.get('market', ''))} · {item['signal_tag']}"
            identity_item.setToolTip(tip + (f" · {item['signal_tip']}" if item.get('signal_tip') else ""))
        self.setItem(row, COL_IDENTITY, identity_item)

    def _set_price_cells(self, row, price_text, price_raw, changes, mode):
        """COL_PRICE and COL_CHG."""
        # COL_PRICE: Price -- NumericItem (see its docstring: setData(EditRole)
        # + setText() alone would silently make this column sort as text).
        price_item = NumericItem(price_text, price_raw)
        price_item.setTextAlignment(_RIGHT)
        price_item.setFont(self._numeric_font)
        self.setItem(row, COL_PRICE, price_item)

        # COL_CHG: Chg -- day-over-day change (data/cache.py's _TD_PERIODS has
        # a "1d" entry now, computed the same way as 3D/20D/60D/120D).
        chg = float(changes.get("1d", 0.0) or 0.0)
        chg_item = NumericItem(_fmt_change(chg, mode), chg)
        chg_item.setTextAlignment(_RIGHT)
        chg_item.setFont(self._numeric_font)
        chg_item.setForeground(fg_for(chg))
        bg = _change_bg(chg, COLUMNS[COL_CHG].scale, mode)
        if bg is not None:
            chg_item.setBackground(bg)
        self.setItem(row, COL_CHG, chg_item)

    def _set_cap_cell(self, row, item):
        """COL_CAP: Cap (market_cap is in KRW; 100,000,000 = 100M KRW). Index,
        yield and commodity rows have no market cap. US rows show their cap in
        dollars ($3,410.0B, user direction 2026-11-18, docs/ui.md 8.31) with the
        KRW figure on the tooltip and sort by it, as a block after the Korean
        rows (_MarketCapItem); the 시총 range filter stays in KRW 억 for all."""
        cap_eok = None
        if not item.get('is_index'):
            try:
                cap_eok = int(item.get('market_cap', 0)) // 100_000_000
            except (TypeError, ValueError, OverflowError):
                cap_eok = 0
        if cap_eok is None:
            cap_item = _empty_numeric_item()
        else:
            is_usd = item.get('currency', '') == '$' and 'usd_price' in item
            usd = _usd_market_cap(item) if is_usd else 0.0
            if usd > 0:
                cap_item = _MarketCapItem(_fmt_usd_cap(usd), usd, 1, cap_eok)
                cap_item.setToolTip(tr(f"약 {cap_eok:,}억 원 (USD/KRW 환산)", f"about {cap_eok:,} x100M KRW (at USD/KRW)"))
            else:
                cap_item = _MarketCapItem(f"{cap_eok:,}", cap_eok, 0, cap_eok)
            cap_item.setTextAlignment(_RIGHT)
        cap_item.setFont(self._numeric_font)
        self.setItem(row, COL_CAP, cap_item)

    def _set_range_cell(self, row, price_raw, changes):
        """COL_RANGE52W: 52W Range bar (docs/ui.md 2.3) -- replaces the old separate
        52W High/High Diff/Low/Low Diff columns. UserRole is left unset
        (RangeBarDelegate paints nothing) when there's no valid range yet."""
        low = float(changes.get("52w_low", 0.0) or 0.0)
        high = float(changes.get("52w_high", 0.0) or 0.0)
        if high > low > 0:
            pos = max(0.0, min(1.0, (price_raw - low) / (high - low)))
            color = PROFIT if pos > 0.8 else LOSS if pos < 0.25 else FLAT
            range_item = NumericItem("", pos)
            range_item.setData(Qt.ItemDataRole.UserRole, {
                "low": f"{low:,.0f}", "high": f"{high:,.0f}",
                "pos": pos, "pos_label": f"{pos * 100:.0f}%", "color": color,
            })
        else:
            range_item = NumericItem("", -1.0)
        self.setItem(row, COL_RANGE52W, range_item)

    def _set_per_cells(self, row, item):
        """COL_TPER / COL_FPER: tPER / fPER. tPER's color is a cheap/expensive value
        judgment (docs/ui.md mockup: green <12x, warning-red >60x), a
        different axis from the PROFIT/LOSS up/down convention, so it
        intentionally does not reuse those two colors."""
        for col, key in ((COL_TPER, 'trailing_per'), (COL_FPER, 'forward_per')):
            val = item.get(key)
            if val is not None:
                cell = NumericItem(f"{val:.1f}", float(val))
                cell.setTextAlignment(_RIGHT)
                cell.setFont(self._numeric_font)
                if col == COL_TPER:
                    color = OK if val < 12 else WARN if val > 60 else TEXT
                    cell.setForeground(QColor(color))
            else:
                cell = _empty_numeric_item()
            self.setItem(row, col, cell)

    def _set_ma_div_cell(self, row, col, raw):
        """COL_MA20DIV / COL_MA60DIV: the price/MA*100 divergence ratio,
        heatmapped around 100; a muted "-" when the ratio is missing (0)."""
        div = float(raw or 0.0)
        if div != 0.0:
            diff = div - 100.0
            ma_item = NumericItem(f"{div:.1f}%", div)
            ma_item.setTextAlignment(_RIGHT)
            ma_item.setFont(self._numeric_font)
            ma_item.setForeground(fg_for(diff))
            bg = heatmap_bg(diff, COLUMNS[col].scale)
            if bg is not None:
                ma_item.setBackground(bg)
        else:
            ma_item = _empty_numeric_item()
        self.setItem(row, col, ma_item)

    def _set_momentum_cells(self, row, changes, mode) -> list:
        """momentum: 3D / 10D / 20D / 60D / 120D (5D dropped). Returns the
        readings in column order for the Trend sparkline."""
        trend_points = []
        for col, key in _MOMENTUM_COLS:
            val = float(changes.get(key, 0.0) or 0.0)
            trend_points.append(val)
            cell = NumericItem(_fmt_change(val, mode), val)
            cell.setTextAlignment(_RIGHT)
            cell.setFont(self._numeric_font)
            if val != 0:
                cell.setForeground(fg_for(val))
                bg = _change_bg(val, COLUMNS[col].scale, mode)
                if bg is not None:
                    cell.setBackground(bg)
            self.setItem(row, col, cell)
        return trend_points

    def _set_trend_cell(self, row, trend_points):
        """COL_TREND: Trend -- a small chart of the five momentum readings
        (_MOMENTUM_COLS) (docs/ui.md's "1Y" sparkline, honestly relabeled:
        see TrendDelegate)."""
        trend_item = NumericItem("", trend_points[-1] if trend_points else 0.0)
        trend_item.setData(Qt.ItemDataRole.UserRole, {
            "points": trend_points,
            "color": PROFIT if trend_points and trend_points[-1] >= 0 else LOSS,
        })
        self.setItem(row, COL_TREND, trend_item)

    # ---Excel-style column filters (docs/ui.md 2.6: reserved for a future
    # "saved views" pass -- no column currently registers a FILTER_COLS
    # entry, since Market moved to the toolbar's ALL/KOSPI/KOSDAQ buttons) ---
    def _get_unique_col_vals(self, col):
        vals = set()
        for row in range(self.rowCount()):
            item = self.item(row, col)
            if item:
                vals.add(item.text())
        return vals

    def _show_filter_popup(self, col, global_pos):
        unique_vals = self._get_unique_col_vals(col)
        current_filter = self._col_filters.get(col)
        popup = FilterPopup(col, unique_vals, current_filter, None)  # None = independent popup window
        popup.filter_changed.connect(self._on_col_filter_changed)
        popup.move(global_pos)
        popup.show()
        popup.adjustSize()
        self._active_popup = popup  # keep reference to prevent GC

    def _on_col_filter_changed(self, col, values):
        if values is None:
            self._col_filters.pop(col, None)
        else:
            self._col_filters[col] = values
        self._filter_header.set_active_filter_cols(set(self._col_filters.keys()))
        self.col_filter_changed.emit()

    def row_values(self, row) -> dict:
        """One view row as the plain dict ui.universe_views.matches() takes:
        identity fields plus each range-filterable column's stored value
        (None where the cell has no value: "-" cells, no 52W range yet)."""
        data = self._row_identity(row) or {}
        values = {}
        for key, col in _RANGE_FILTER_COLS.items():
            item = self.item(row, col)
            # _filter_value when a cell sorts on something else (US caps sort in USD, filter in KRW 억).
            v = getattr(item, "_filter_value", getattr(item, "_sort_key", None)) if item is not None else None
            if v is None or v == float('-inf') or (col == COL_RANGE52W and v < 0):
                v = None
            values[key] = v
        return {
            "market": data.get("market", ""), "status": data.get("status", ""),
            "name": data.get("name", ""), "ticker": data.get("ticker", ""),
            "mode": data.get("mode", "pct"), "values": values,
        }

    def all_row_values(self) -> list:
        return [self.row_values(r) for r in range(self.rowCount())]

    def apply_col_filters(self, text_filter="", status="", market="ALL", ranges=None):
        """Apply the toolbar's market filter, status filter, text search and
        the Filters panel's numeric ranges (docs/ui.md 2.6 / 8.9). Called by
        UniverseTab.filter_table(). `market` is one of "ALL"/"KOSPI"/"KOSDAQ"
        (docs/ui.md 2.1: toolbar buttons, Market is not a column); `status`
        is "" (no filter), "On" (Held) or "Tg" (Target); `ranges` is
        {column key: [lo, hi]} in the units the panel shows. Every row goes
        through ui.universe_views.matches(), the same test the saved views'
        counts use."""
        state = {"market": market, "status": status, "search": text_filter, "ranges": ranges or {}}
        self._filter_header.set_active_filter_cols(
            {_RANGE_FILTER_COLS[k] for k, b in state["ranges"].items()
             if k in _RANGE_FILTER_COLS and b and any(x is not None for x in b)})

        hdr = self._filter_header
        sort_col = hdr.sortIndicatorSection()
        sort_order = hdr.sortIndicatorOrder()
        self.setUpdatesEnabled(False)  # suppress repaints while toggling row visibility
        self.setSortingEnabled(False)

        for row in range(self.rowCount()):
            self.setRowHidden(row, not view_matches(self.row_values(row), state))

        # Re-enable sorting and restore the exact same sort indicator so Qt
        # does not apply a different sort order.
        self.setSortingEnabled(True)
        hdr.setSortIndicator(sort_col, sort_order)
        self._sync_frozen_column()
        self.setUpdatesEnabled(True)
