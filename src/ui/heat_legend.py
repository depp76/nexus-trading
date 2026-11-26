"""ui/heat_legend.py — HeatLegend, the Trading Universe footer legend for the
change/momentum heatmap (docs/ui.md 1.2 / 8.7).

Moved out of ui/widgets.py (2026-11-15, code review #5); the ui.widgets re-export
module was removed on 2026-11-26.
"""
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QSize
from PyQt6.QtGui import QColor, QPainter

from i18n import tr
from ui.common import create_font, FONT_CAPTION
from ui.colors import heatmap_bg
from ui.theme import TEXT_FAINT, LINE_GROUP
from ui.universe_columns import COLUMNS


class HeatLegend(QWidget):
    """Footer legend for the change/momentum heatmap (docs/ui.md 1.2 / 8.7):
    a LOSS -> 0 -> PROFIT color bar drawn with heatmap_bg() itself, and the
    distance at which each column group reaches full color, read from
    COLUMNS' scale values so the legend cannot drift from the cells."""

    BAR_W, BAR_H = 96, 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        self.text = self.scale_text()
        self.setToolTip(tr("셀 색은 변동이 클수록 짙어지고 아래 값에서 가장 짙습니다. "
                           "파랑 = 상승, 빨강 = 하락. 금리는 ±25bp 기준.",
                           "Cell tint grows with the size of the move and is full at these values; "
                           "blue = up, red = down. Yields use +-25 bp."))

    @staticmethod
    def scale_text() -> str:
        def scale(key):
            return next(c.scale for c in COLUMNS if c.key == key)
        return tr(f"\ucd5c\ub300 \uc0c9  \ub4f1\ub77d \u00b1{scale('chg'):g}%  \u00b7  3D\u2013120D \u00b1{scale('d3'):g}%  "
                  f"\u00b7  MA Div 100 \u00b1{scale('ma20div'):g}",
                  f"full at  Chg \u00b1{scale('chg'):g}%  \u00b7  3D\u2013120D \u00b1{scale('d3'):g}%  "
                  f"\u00b7  MA Div \u00b1{scale('ma20div'):g} from 100")

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance("-") * 2 + self.BAR_W + fm.horizontalAdvance(self.text) + 40,
                     max(fm.height(), self.BAR_H) + 4)

    def paintEvent(self, _event):
        p = QPainter(self)
        fm = self.fontMetrics()
        mid = self.height() // 2
        x = 0
        p.setPen(QColor(TEXT_FAINT))
        p.drawText(x, mid + fm.ascent() // 2 - 1, "\u2212")
        x += fm.horizontalAdvance("\u2212") + 4
        top = mid - self.BAR_H // 2
        for i in range(self.BAR_W):
            v = (i / (self.BAR_W - 1)) * 2 - 1        # -1 .. +1 of full scale
            bg = heatmap_bg(v, 1.0)
            if bg is not None:
                p.fillRect(x + i, top, 1, self.BAR_H, bg)
        p.setPen(QColor(LINE_GROUP))
        p.drawRect(x, top, self.BAR_W - 1, self.BAR_H - 1)
        p.drawLine(x + self.BAR_W // 2, top, x + self.BAR_W // 2, top + self.BAR_H - 1)
        x += self.BAR_W + 4
        p.setPen(QColor(TEXT_FAINT))
        p.drawText(x, mid + fm.ascent() // 2 - 1, "+")
        x += fm.horizontalAdvance("+") + 10
        p.drawText(x, mid + fm.ascent() // 2 - 1, self.text)
        p.end()
