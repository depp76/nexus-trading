"""ui/table_headers.py — custom QHeaderViews: FilterableHeader (the Trading
Universe header: group dividers, right edge, active-filter marks) and
GroupedHeaderView (the two-row grouped header of Trading History and Total
Assets).

Moved out of ui/widgets.py (2026-11-15, code review #5); ui.widgets still
re-exports both.
"""
from PyQt6.QtWidgets import QHeaderView, QStyleOptionHeader
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QRect
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygon

from ui.common import FONT_BODY, FONT_CAPTION
from ui.theme import ACCENT, TEXT_FAINT, TEXT_SUB, TEXT_MUTED, LINE_GROUP, GRP_BG
from ui.universe_columns import GROUP_LINE_W


# ---------------------------------------------------------------------------
# FilterableHeader  — draws ▼ icon per filter column
# ---------------------------------------------------------------------------
class FilterableHeader(QHeaderView):
    filter_requested = pyqtSignal(int, QPoint)
    # Market's dropdown filter moved to toolbar ALL/KOSPI/KOSDAQ buttons
    # (docs/ui.md 2.1); no column currently owns a filter icon. Left as an
    # empty set (not deleted) since docs/ui.md 2.6's "saved views" pass will
    # want per-column filters again for numeric-range/category columns.
    FILTER_COLS = set()

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self._active_filter_cols = set()
        # Columns whose left edge carries a group divider (StockTable sets
        # GROUP_START_COLS); painted per section so the header lines up with
        # the body's dividers.
        self.divider_cols = ()
        # When set, paintEvent adds a 1px line on the viewport's last pixel
        # (the table's right outer edge; the last section's own border only
        # lands there when the sections end exactly at the viewport edge).
        self.right_edge = False

    def set_active_filter_cols(self, cols):
        self._active_filter_cols = set(cols)
        self.viewport().update()

    def paintSection(self, painter, rect, logicalIndex):
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex in self.divider_cols:
            painter.save()
            painter.fillRect(rect.left(), rect.top(), GROUP_LINE_W // 2, rect.height(), QColor(LINE_GROUP))
            painter.restore()
        if logicalIndex + 1 in self.divider_cols:
            # The divider sits on this section's right edge (the last pixel
            # before the next group's first column), over its own border.
            painter.save()
            w = GROUP_LINE_W - GROUP_LINE_W // 2
            painter.fillRect(rect.right() - w + 1, rect.top(), w, rect.height(), QColor(LINE_GROUP))
            painter.restore()
        # Columns with an active range filter (docs/ui.md 8.9) get the same
        # accent mark the old per-column dropdown filter used.
        if logicalIndex in self.FILTER_COLS or logicalIndex in self._active_filter_cols:
            is_active = logicalIndex in self._active_filter_cols
            icon_w, icon_h = 10, 6
            ix = rect.right() - icon_w - 4
            iy = rect.center().y() - icon_h // 2 + 1
            painter.save()
            color = QColor(ACCENT) if is_active else QColor(TEXT_FAINT)
            painter.setPen(color)
            painter.setBrush(color)
            pts = QPolygon([
                QPoint(ix, iy),
                QPoint(ix + icon_w, iy),
                QPoint(ix + icon_w // 2, iy + icon_h),
            ])
            painter.drawPolygon(pts)
            painter.restore()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.right_edge:
            painter = QPainter(self.viewport())
            painter.fillRect(self.viewport().width() - 1, 0, 1, self.viewport().height(), QColor(LINE_GROUP))
            painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            logical = self.logicalIndexAt(pos)
            if logical in self.FILTER_COLS:
                # Any click on a filter column - open popup (no sorting)
                gp = self.viewport().mapToGlobal(pos)
                self.filter_requested.emit(logical, gp)
                return
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# GroupedHeaderView  — two-row grouped column header
# ---------------------------------------------------------------------------
class GroupedHeaderView(QHeaderView):
    """
    A QHeaderView that renders two rows:
      Top row - section group labels spanning multiple columns
                   (e.g. "Buy", "Sell", "Position", "Past")
      Bottom row - individual column sub-labels

    sections: list of (group_label, start_col, col_span, color_hex)
    sub_labels: list of per-column label strings (len == columnCount)
    """

    def __init__(self, sections, sub_labels, parent=None, group_h=22, sub_h=22, sortable=False):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._GROUP_H = group_h
        self._SUB_H = sub_h
        self._sections   = sections    # [(label, start, span, color), ...]
        self._sub_labels = sub_labels  # [str, ...]
        self.setDefaultSectionSize(72)
        # Clicking a section is what QTableView's own sortingEnabled hooks
        # into (QHeaderView.mousePressEvent -> setSortIndicator ->
        # sortIndicatorChanged); leaving this False (the default, matching
        # every caller before Total Assets' Phase 4) makes a table's
        # setSortingEnabled(True) inert -- there's no click for it to react to.
        self.setSectionsClickable(sortable)

    def set_sub_labels(self, sub_labels):
        """Swap the bottom-row labels in place (e.g. Total Assets tab's
        currency toggle relabels a "Total Assets" column KRW/USD) and repaint."""
        self._sub_labels = sub_labels
        self.viewport().update()

    # ---total header height ---
    def sizeHint(self):
        sh = super().sizeHint()
        sh.setHeight(self._GROUP_H + self._SUB_H)
        return sh

    def sectionSizeHint(self, _):
        return self._GROUP_H + self._SUB_H

    # ---painting ---
    def paintSection(self, painter, rect, logical_index):
        """Paint the bottom sub-label row only (group row drawn in paintEvent)."""
        if not rect.isValid():
            return
        painter.save()

        # Bottom sub-label cell
        sub_rect = rect.adjusted(0, self._GROUP_H, 0, 0)
        opt = self._style_option(logical_index)

        # Group headers are neutral (docs/ui.md 8.3): blue/red mean
        # up/down and accent means selection everywhere else, so a "Sell"
        # header in red or a "Cumulative P/L" header in blue read as a
        # loss/gain. The sections' color field is ignored.
        col_color = TEXT_MUTED

        # Draw frame
        self.style().drawControl(
            self.style().ControlElement.CE_Header, opt, painter, self
        )

        # Draw sub-label text with group colour
        painter.setPen(QColor(col_color))
        f = painter.font()
        f.setPointSize(FONT_CAPTION)
        f.setBold(True)
        painter.setFont(f)
        label = self._sub_labels[logical_index] if logical_index < len(self._sub_labels) else ""
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.restore()

    def paintEvent(self, event):
        """First paint all sections (bottom row), then overlay the top group row."""
        super().paintEvent(event)

        painter = QPainter(self.viewport())
        painter.save()

        total_h = self._GROUP_H + self._SUB_H

        for label, start, span, _color in self._sections:
            if start >= self.count():
                continue
            # X-coordinates: from left edge of start col to right edge of (start+span-1)
            x_left  = self.sectionViewportPosition(start)
            end_col  = min(start + span - 1, self.count() - 1)
            x_right  = self.sectionViewportPosition(end_col) + self.sectionSize(end_col)
            gx = x_left
            gw = x_right - x_left
            if gw <= 0:
                continue

            group_rect = QRect(gx, 0, gw, self._GROUP_H)

            # Background
            painter.fillRect(group_rect, QColor(GRP_BG))

            # Thin top/bottom border for the group row
            pen_thin = QPen(QColor(LINE_GROUP))
            pen_thin.setWidth(1)
            pen_thin.setCosmetic(True)
            painter.setPen(pen_thin)
            painter.drawLine(gx, 0, gx + gw - 1, 0)                                  # top
            painter.drawLine(gx, self._GROUP_H - 1, gx + gw - 1, self._GROUP_H - 1) # bottom

            # Label only (vertical borders are drawn per-column in paintSection)
            painter.setPen(QPen(QColor(TEXT_SUB)))
            f = painter.font()
            f.setPointSize(FONT_BODY)
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(group_rect, Qt.AlignmentFlag.AlignCenter, label)

        # ---Section boundary vertical lines drawn LAST (on top of everything) ---
        last_section_end = max(s + sp - 1 for _, s, sp, _ in self._sections)
        pen_sec = QPen()
        pen_sec.setWidth(1)
        pen_sec.setCosmetic(True)
        for _, start, span, _color in self._sections:
            if start >= self.count():
                continue
            end_col = min(start + span - 1, self.count() - 1)
            pen_sec.setColor(QColor(LINE_GROUP))
            painter.setPen(pen_sec)
            # Left border of this section
            xl = self.sectionViewportPosition(start)
            painter.drawLine(xl, 0, xl, total_h - 1)
            # Right border only for the very last section
            if end_col == last_section_end:
                xr = self.sectionViewportPosition(end_col) + self.sectionSize(end_col) - 1
                painter.drawLine(xr, 0, xr, total_h - 1)

        painter.restore()

    def _style_option(self, logical_index):
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.section = logical_index
        opt.rect    = QRect(
            self.sectionViewportPosition(logical_index),
            self._GROUP_H,
            self.sectionSize(logical_index),
            self._SUB_H,
        )
        opt.text = ""   # we draw text ourselves
        opt.position = QStyleOptionHeader.SectionPosition.Middle
        return opt
