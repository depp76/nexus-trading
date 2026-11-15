"""ui/trend_score_section.py — the Trend Score block of the MA chart dialog's
right panel (docs/ui.md 8.10 / 8.21).

Shows one name from the last Trend Score run (trend_following.md 2-5, run from
the Trading Universe toolbar): the weekly average total and its Top/Bottom
rank, the three latest-session percentiles the total is built from, and the
four gate checks as Pass/Fail badges. It is a snapshot of the run the dialog
was opened with -- a later run does not update an open dialog, so the title
carries the run's as-of date.

The block always has the same rows and height (user direction 2026-11-15):
before any run, or for a name the run did not score, every value is "-" and
every gate badge is a neutral "-"; an unscored name says "채점 제외" on the
summary line with the reason in its tooltip.

Type sizes (2026-11-15 review): the title matches the panel's other section
title (FONT_BODY bold); the weekly average -- the score itself -- is the
largest figure (FONT_KPI); the three percentile cards one step below
(FONT_HEADING); captions, card help and badges FONT_CAPTION; gate names
FONT_BODY.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel

from i18n import tr
from strategy.trend_following import StrategyParams
from ui.common import (
    KpiCard, create_font, make_badge,
    FONT_BODY, FONT_CAPTION, FONT_HEADING, FONT_KPI,
)

NOT_SCORED_TEXT = tr("직전 추세 점수 실행에서 채점되지 않은 종목입니다 (유동성 부족, 이력 부족 또는 국내 종목 아님)",
                     "Not scored in the last Trend Score run (illiquid, too little history, or not a KR stock)")
NOT_SCORED_SHORT = tr("채점 제외", "Not scored")


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def rank_of(rec, ticker: str) -> str:
    """"Top 3" / "Bottom 7" / "" for `ticker` in `rec`."""
    for label, names in (("Top", rec.top), ("Bottom", rec.bottom)):
        for i, s in enumerate(names, 1):
            if s.ticker == ticker:
                return f"{label} {i}"
    return ""


def _lock_height(label: QLabel, sample: str) -> None:
    """Fix `label`'s height to what `sample` needs in the same style, so a
    row is as tall with "-" as with Hangul text (a fallback font can be
    taller), and the block keeps one height whatever it shows."""
    probe = QLabel(sample)
    probe.setObjectName(label.objectName())
    probe.setFont(label.font())
    probe.setStyleSheet(label.styleSheet())
    probe.ensurePolished()
    label.ensurePolished()
    label.setFixedHeight(max(probe.sizeHint().height(), label.sizeHint().height()))


class TrendScoreSection(QWidget):
    def __init__(self, rec, ticker: str, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        s = rec.scored.get(ticker) if rec is not None else None

        # Title: the same style as the panel's table title above it.
        title = QLabel(tr("추세 점수", "Trend Score")
                       + ((tr(f" · {rec.as_of:%Y-%m-%d} 기준", f" · as of {rec.as_of:%Y-%m-%d}"))
                          if rec is not None else ""))
        title.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl = title
        box.addWidget(title)

        # Summary: caption | weekly average (the score) | latest + rank.
        summary = QHBoxLayout()
        summary.setSpacing(8)
        cap = QLabel(tr("주간 평균", "Weekly avg"))
        cap.setObjectName("muted")
        cap.setFont(create_font(FONT_CAPTION))
        summary.addWidget(cap, 0, Qt.AlignmentFlag.AlignVCenter)
        self.total_lbl = QLabel("-")
        self.total_lbl.setFont(create_font(FONT_KPI, style_name="Semilight"))
        summary.addWidget(self.total_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hint_lbl = QLabel("")           # latest total + rank, or why the name is not scored
        self.hint_lbl.setObjectName("muted")
        self.hint_lbl.setFont(create_font(FONT_BODY))
        summary.addWidget(self.hint_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(summary)
        for lbl in (cap, self.total_lbl, self.hint_lbl):
            _lock_height(lbl, "통과 +1.00 · Top 1")

        if s is not None:
            self.total_lbl.setText(f"{s.week_avg:+.2f}")
            rank = rank_of(rec, ticker)
            self.hint_lbl.setText(tr(f"최근 {s.latest_total:+.2f}", f"latest {s.latest_total:+.2f}")
                                  + (f"  · {rank}" if rank else ""))
        elif rec is not None:
            self.hint_lbl.setText(NOT_SCORED_SHORT)
            self.hint_lbl.setToolTip(NOT_SCORED_TEXT)
            self.total_lbl.setToolTip(NOT_SCORED_TEXT)

        # Three percentile cards, always there ("-" without a score).
        cards = QGridLayout()
        cards.setSpacing(6)
        self.cards: dict[str, KpiCard] = {}
        for i, (key, label, help_text, attr) in enumerate((
                ("ma50", "MA50Div", tr("추세 방향 +", "Trend direction +"), "pct_ma50"),
                ("range52", "Range52", tr("추세 위치 +", "Trend position +"), "pct_range52"),
                ("r3", "R3", tr("최근 3일 −", "Last 3 days −"), "pct_r3"))):
            card = KpiCard(label, help_text)
            v = getattr(s, attr) if s is not None else None
            card.set(f"{v:.2f}" if _finite(v) else "-")
            # One step below the weekly average. Appended to the sheet set()
            # just wrote (its color); setFont would lose to the global
            # QFrame#KpiCard QLabel#kpiValue rule.
            card.value.setStyleSheet(card.value.styleSheet() + f" font-size: {FONT_HEADING}pt;")
            cards.addWidget(card, 0, i)
            self.cards[key] = card
        box.addLayout(cards)

        # Four gate rows, always there (neutral "-" badges without a score).
        min_range = (rec.params if rec is not None else StrategyParams()).gate_range_min
        checks = [
            ("liquid", tr("유동성", "Liquidity"), (lambda x: x.liquid)),
            ("ma50", "MA50Div > 0", (lambda x: _finite(x.ma50_div) and x.ma50_div > 0)),
            ("range52", f"Range52 ≥ {min_range:.2f}", (lambda x: _finite(x.range52) and x.range52 >= min_range)),
            ("heat", tr("과열 아님", "Not overheated"), (lambda x: not x.overheated)),
        ]
        self.gate_badges: dict[str, QLabel] = {}
        for key, text, test in checks:
            row = QHBoxLayout()
            name = QLabel(text)
            name.setFont(create_font(FONT_BODY))
            row.addWidget(name, 1)
            if s is None:
                badge = make_badge("na", "-")
            else:
                ok = test(s)
                badge = make_badge("ok" if ok else "fail", tr("통과", "Pass") if ok else tr("미달", "Fail"))
            _lock_height(badge, "통과 미달")
            _lock_height(name, "유동성 Range52 ≥ 0.70")
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
            box.addLayout(row)
            self.gate_badges[key] = badge
