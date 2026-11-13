"""ui/trend_score_section.py — the Trend Score block of the MA chart dialog's
right panel (docs/ui.md 8.10).

Shows one name from the last Trend Score run (trend_following.md 2-5, run from
the Trading Universe toolbar): the weekly average total and its Top/Bottom
rank, the three latest-session percentiles the total is built from, and the
four gate checks as Pass/Fail badges. It is a snapshot of the run the dialog
was opened with -- a later run does not update an open dialog, so the title
carries the run's as-of date. Before any run, or for a name the run did not
score, one line says so.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel

from ui.common import KpiCard, create_font, make_badge, set_badge, FONT_SMALL

NOT_RUN_TEXT = "Run Trend Score in Trading Universe to see the score breakdown"
NOT_SCORED_TEXT = "Not scored in the last Trend Score run (illiquid, too little history, or not a KR stock)"


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def rank_of(rec, ticker: str) -> str:
    """"Top 3" / "Bottom 7" / "" for `ticker` in `rec`."""
    for label, names in (("Top", rec.top), ("Bottom", rec.bottom)):
        for i, s in enumerate(names, 1):
            if s.ticker == ticker:
                return f"{label} {i}"
    return ""


class TrendScoreSection(QWidget):
    def __init__(self, rec, ticker: str, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        s = rec.scored.get(ticker) if rec is not None else None
        title = QLabel("TREND SCORE" + (f" · as of {rec.as_of:%Y-%m-%d}" if rec is not None else ""))
        title.setObjectName("kpiLabel")
        box.addWidget(title)

        self.hint_lbl = QLabel(NOT_RUN_TEXT if rec is None else NOT_SCORED_TEXT)
        self.hint_lbl.setObjectName("faint")
        self.hint_lbl.setWordWrap(True)
        box.addWidget(self.hint_lbl)
        self.hint_lbl.setVisible(s is None)

        self.total_lbl = QLabel("")
        self.cards: dict[str, KpiCard] = {}
        self.gate_badges: dict[str, QLabel] = {}
        if s is None:
            return

        rank = rank_of(rec, ticker)
        self.total_lbl.setText(f"Weekly avg {s.week_avg:+.2f}  (latest {s.latest_total:+.2f})"
                               + (f"  · {rank}" if rank else ""))
        box.addWidget(self.total_lbl)

        cards = QGridLayout()
        cards.setSpacing(6)
        for i, (key, label, help_text, v) in enumerate((
                ("ma50", "MA50Div", "추세 방향 +", s.pct_ma50),
                ("range52", "Range52", "추세 위치 +", s.pct_range52),
                ("r3", "R3", "최근 3일 −", s.pct_r3))):
            card = KpiCard(label, help_text)
            card.value.setFont(create_font(FONT_SMALL))
            card.set(f"{v:.2f}" if _finite(v) else "-")
            cards.addWidget(card, 0, i)
            self.cards[key] = card
        box.addLayout(cards)

        min_range = rec.params.gate_range_min
        checks = [
            ("liquid", "Liquidity", s.liquid),
            ("ma50", "MA50Div > 0", _finite(s.ma50_div) and s.ma50_div > 0),
            ("range52", f"Range52 ≥ {min_range:.2f}", _finite(s.range52) and s.range52 >= min_range),
            ("heat", "Not overheated", not s.overheated),
        ]
        for key, text, ok in checks:
            row = QHBoxLayout()
            row.addWidget(QLabel(text), 1)
            badge = make_badge("ok", "")
            set_badge(badge, "ok" if ok else "fail", "Pass" if ok else "Fail")
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
            box.addLayout(row)
            self.gate_badges[key] = badge
