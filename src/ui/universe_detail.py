"""ui/universe_detail.py — the Trading Universe detail panel to the right of the
table (docs/ui_redesign_2026-11.md P2-2, docs/ui.md 8.10).

Follows the table's current row: name / ticker / market, price and Chg, a
mini price chart of the cached daily closes (no download: data.history's
_HIST_CACHE, filled by the universe refresh), and -- once Trend Score has run --
the score breakdown (the three percentiles the total is built from, the
weekly average) and the gate checks with Pass/Fail badges. MA Chart and AI
Report do what the row's context menu does.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame

from data_fetcher import peek_recent_closes
from ui.colors import PROFIT, LOSS, fg_for
from ui.common import KpiCard, create_font, make_badge, make_button, set_badge, FONT_HEADING, FONT_SMALL
from ui.theme import TEXT_FAINT, TEXT_EMPTY

CHART_SESSIONS = 120
NOT_RUN_TEXT = "Run Trend Score to see the score breakdown"
NOT_SCORED_TEXT = "Not scored in the last Trend Score run (illiquid, too little history, or not a KR stock)"


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


class PriceSpark(QWidget):
    """A plain close-price polyline, PROFIT when the window ends higher than it
    started, LOSS when lower; a hint line when there is nothing to draw."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.closes: list[float] = []
        self.setMinimumHeight(90)

    def set_closes(self, closes):
        self.closes = [float(c) for c in closes if _finite(c)]
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(4, 6, -4, -6)
        if len(self.closes) < 2:
            p.setPen(QColor(TEXT_FAINT))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No cached daily history yet")
            p.end()
            return
        lo, hi = min(self.closes), max(self.closes)
        span = (hi - lo) or 1.0
        n = len(self.closes)
        pts = QPolygonF([QPointF(r.x() + r.width() * i / (n - 1), r.bottom() - (v - lo) / span * r.height())
                         for i, v in enumerate(self.closes)])
        pen = QPen(QColor(PROFIT if self.closes[-1] >= self.closes[0] else LOSS))
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.drawPolyline(pts)
        p.setPen(QColor(TEXT_EMPTY))
        p.drawText(self.rect().adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                   f"{n} sessions")
        p.end()


GATE_ROWS = [("liquid", "Liquidity"), ("ma50", "MA50Div > 0"), ("range52", "Range52 ≥ {min:.2f}"),
             ("heat", "Not overheated")]


class UniverseDetailPanel(QFrame):
    ma_chart_requested = pyqtSignal(str)
    ai_report_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DashboardCard")
        self.ticker: str | None = None
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)

        self.name_lbl = QLabel("Select a row")
        self.name_lbl.setFont(create_font(FONT_HEADING))
        self.name_lbl.setWordWrap(True)
        self.meta_lbl = QLabel("")
        self.meta_lbl.setObjectName("muted")
        price_row = QHBoxLayout()
        self.price_lbl = QLabel("")
        self.price_lbl.setFont(create_font(FONT_HEADING))
        self.chg_lbl = QLabel("")
        price_row.addWidget(self.price_lbl)
        price_row.addWidget(self.chg_lbl)
        price_row.addStretch()
        for w in (self.name_lbl, self.meta_lbl):
            box.addWidget(w)
        box.addLayout(price_row)

        self.spark = PriceSpark()
        box.addWidget(self.spark)

        title = QLabel("TREND SCORE")
        title.setObjectName("kpiLabel")
        box.addWidget(title)
        self.hint_lbl = QLabel(NOT_RUN_TEXT)
        self.hint_lbl.setObjectName("faint")
        self.hint_lbl.setWordWrap(True)
        box.addWidget(self.hint_lbl)

        self.score_box = QWidget()
        sb = QVBoxLayout(self.score_box)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.total_lbl = QLabel("")
        sb.addWidget(self.total_lbl)
        cards = QGridLayout()
        cards.setSpacing(6)
        self.cards = {
            "ma50": KpiCard("MA50Div", "추세 방향 +"),
            "range52": KpiCard("Range52", "추세 위치 +"),
            "r3": KpiCard("R3", "최근 3일 −"),
        }
        for i, card in enumerate(self.cards.values()):
            card.value.setFont(create_font(FONT_SMALL))
            cards.addWidget(card, 0, i)
        sb.addLayout(cards)
        gate_title = QLabel("GATE")
        gate_title.setObjectName("kpiLabel")
        sb.addWidget(gate_title)
        self.gate_badges: dict[str, QLabel] = {}
        self.gate_labels: dict[str, QLabel] = {}
        for key, text in GATE_ROWS:
            row = QHBoxLayout()
            lbl = QLabel(text)
            badge = make_badge("na", "-")
            row.addWidget(lbl, 1)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
            sb.addLayout(row)
            self.gate_badges[key] = badge
            self.gate_labels[key] = lbl
        box.addWidget(self.score_box)
        self.score_box.setVisible(False)

        box.addStretch()
        btns = QHBoxLayout()
        self.ma_btn = make_button("MA Chart", on_click=self._emit_ma)
        self.ai_btn = make_button("AI Report", on_click=self._emit_ai)
        btns.addWidget(self.ma_btn)
        btns.addWidget(self.ai_btn)
        btns.addStretch()
        box.addLayout(btns)
        self.show_item(None, None)

    def _emit_ma(self):
        if self.ticker:
            self.ma_chart_requested.emit(self.ticker)

    def _emit_ai(self):
        if self.ticker:
            self.ai_report_requested.emit(self.ticker)

    # ------------------------------------------------------------------
    def show_item(self, item: dict | None, rec, closes=None):
        """Show one Universe row (`item`, an all_data dict) with the last
        Trend Score run `rec` (None before any run). `closes` overrides the
        cache lookup (tests)."""
        self.ticker = item.get("ticker") if item else None
        for b in (self.ma_btn, self.ai_btn):
            b.setEnabled(self.ticker is not None)
        if item is None:
            self.name_lbl.setText("Select a row")
            self.meta_lbl.setText("")
            self.price_lbl.setText("")
            self.chg_lbl.setText("")
            self.spark.set_closes([])
            self._show_score(None, rec)
            return
        self.name_lbl.setText(item.get("name", self.ticker))
        self.meta_lbl.setText(f"{self.ticker} · {item.get('market', '')}")
        price = item.get("usd_price") if item.get("currency") == "$" and "usd_price" in item else item.get("price")
        try:
            price = float(price)
            self.price_lbl.setText(f"{price:,.2f}" if item.get("is_index") or price < 1000 else f"{price:,.0f}")
        except (TypeError, ValueError):
            self.price_lbl.setText("-")
        chg = float((item.get("changes") or {}).get("1d", 0.0) or 0.0)
        self.chg_lbl.setText(f"{chg:+.2f}%")
        self.chg_lbl.setStyleSheet(f"color:{fg_for(chg).name()};")
        if closes is None:
            closes = peek_recent_closes(self.ticker, CHART_SESSIONS) if self.ticker else []
        self.spark.set_closes(closes)
        self._show_score(rec.scored.get(self.ticker) if rec is not None else None, rec)

    def _show_score(self, s, rec):
        if rec is None:
            self.hint_lbl.setText(NOT_RUN_TEXT)
        elif s is None:
            self.hint_lbl.setText(NOT_SCORED_TEXT if self.ticker else "")
        self.hint_lbl.setVisible(s is None)
        self.score_box.setVisible(s is not None)
        if s is None:
            return
        rank = ""
        if any(t.ticker == s.ticker for t in rec.top):
            rank = f" · Top {next(i for i, t in enumerate(rec.top, 1) if t.ticker == s.ticker)}"
        elif any(t.ticker == s.ticker for t in rec.bottom):
            rank = f" · Bottom {next(i for i, t in enumerate(rec.bottom, 1) if t.ticker == s.ticker)}"
        self.total_lbl.setText(f"Weekly avg {s.week_avg:+.2f}  (latest {s.latest_total:+.2f}){rank}"
                               f"  · as of {rec.as_of:%Y-%m-%d}")
        for key, v in (("ma50", s.pct_ma50), ("range52", s.pct_range52), ("r3", s.pct_r3)):
            self.cards[key].set(f"{v:.2f}" if _finite(v) else "-")
        p = rec.params
        self.gate_labels["range52"].setText(f"Range52 ≥ {p.gate_range_min:.2f}")
        checks = {
            "liquid": s.liquid,
            "ma50": _finite(s.ma50_div) and s.ma50_div > 0,
            "range52": _finite(s.range52) and s.range52 >= p.gate_range_min,
            "heat": not s.overheated,
        }
        for key, ok in checks.items():
            set_badge(self.gate_badges[key], "ok" if ok else "fail", "Pass" if ok else "Fail")
