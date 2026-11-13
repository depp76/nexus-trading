"""ui/strategy_widgets.py — the pieces of the Strategy › Trend Following result
screen (docs/ui_redesign_2026-11.md P1, docs/ui.md 8.10), kept out of
ui/trend_following_tab.py so the tab module stays about wiring.

  - target checks: TARGET_SHARPE / TARGET_MDD and the badge they give a run
  - drawdown_series: the drawdown curve under the NAV chart
  - SegmentRow: a row of fixed-width toggle buttons, one checked at a time
  - KpiRow: the six KpiCards for the selected run, reflowing to 3x2 when narrow
  - TargetCheckCard: what the app itself can judge about the selected run
  - SharpeBarDelegate: the comparison table's Sharpe bar cell
  - fill_comparison / fill_monthly / fill_trades: the result tables

Pure display: every number comes from a RunSummary the research run already
produced; nothing here recomputes a metric the scorecard reports.
"""
from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import Qt, QRect, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPen
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QGridLayout, QVBoxLayout, QLabel, QFrame, QButtonGroup, QTableWidget,
    QTableWidgetItem,
)

from strategy.trend_following import VARIANTS
from strategy.trend_following.variant_info import describe_variant
from strategy.trend_following.metrics import monthly_returns, yearly_returns
from ui.colors import OK_TEXT, FAIL_TEXT, fg_for, heatmap_bg
from ui.common import KpiCard, make_badge, make_button, set_badge
from ui.delegates import CellDelegate
from ui.theme import ACCENT, TEXT, TEXT_MUTED, TEXT_EMPTY, LINE_STRONG

# ---------------------------------------------------------------------------
# Targets (user's performance bar, Nexus README "H"): Sharpe >= 1.5 and a
# maximum drawdown no deeper than 15% over the whole window.
# ---------------------------------------------------------------------------
TARGET_SHARPE = 1.5
TARGET_MDD = 0.15
TARGET_SOURCE = "사용자 성과 기준 (Nexus README, 기준 H)"

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def sharpe_ok(sharpe) -> bool | None:
    return (sharpe >= TARGET_SHARPE) if _finite(sharpe) else None


def mdd_ok(mdd) -> bool | None:
    # A tiny epsilon so -0.15 reported as -0.15000000000000002 still counts as "within".
    return (abs(mdd) <= TARGET_MDD + 1e-12) if _finite(mdd) else None


def meets_targets(metrics: dict) -> bool | None:
    """Both targets at once (criterion H); None when either value is missing."""
    s, m = sharpe_ok(metrics.get("sharpe")), mdd_ok(metrics.get("mdd"))
    if s is None or m is None:
        return None
    return s and m


def sharpe_badge(sharpe) -> tuple[str, str]:
    ok = sharpe_ok(sharpe)
    if ok is None:
        return "na", "값 없음"
    return ("ok", "✓ 목표 충족") if ok else ("fail", f"✕ {TARGET_SHARPE:g} 미만")


def mdd_badge(mdd) -> tuple[str, str]:
    ok = mdd_ok(mdd)
    if ok is None:
        return "na", "값 없음"
    pct = f"{TARGET_MDD * 100:g}%"
    return ("ok", f"✓ {pct} 이내") if ok else ("fail", f"✕ {pct} 초과")


def drawdown_series(nav, base: float = 1.0) -> np.ndarray:
    """``nav / running peak - 1`` (<= 0), the peak starting at `base` like
    metrics.max_drawdown, so the curve's low point is the MDD card's value."""
    nav = np.asarray(nav, dtype=float)
    if len(nav) == 0:
        return nav
    peak = np.maximum.accumulate(np.concatenate([[base], nav]))[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(peak > 0, nav / peak - 1.0, 0.0)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt_pct(x, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%" if _finite(x) else "-"


def fmt_pp(x, digits: int = 1) -> str:
    """A CAGR difference as percentage points, signed."""
    return f"{x * 100:+.{digits}f}%p" if _finite(x) else "-"


def fmt_num(x, digits: int = 2) -> str:
    return f"{x:.{digits}f}" if _finite(x) else "-"


def _hex(x) -> str | None:
    return fg_for(x).name() if _finite(x) else None


def entry_label(run) -> str:
    """Short description of a run for the comparison table: the variant's
    picker title, or the benchmark's own label."""
    if run.kind == "strategy" and run.id in VARIANTS:
        return describe_variant(run.id).title
    return run.label


# ---------------------------------------------------------------------------
# Segmented selector
# ---------------------------------------------------------------------------
class SegmentRow(QWidget):
    """Fixed-width toggle buttons, exactly one checked (docs/ui.md 8.1 allows a
    fixed width for a segmented row). `selected(key)` fires on a user click
    and on select(); disabled keys cannot be chosen."""

    selected = pyqtSignal(str)

    def __init__(self, width: int = 44, parent=None):
        super().__init__(parent)
        self._width = width
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[str, object] = {}

    def set_keys(self, keys, labels: dict | None = None, enabled=None, tooltips: dict | None = None):
        for btn in self.buttons.values():
            self._group.removeButton(btn)
            btn.deleteLater()
        self.buttons = {}
        for key in keys:
            btn = make_button((labels or {}).get(key, key), width=self._width, checkable=True,
                              tooltip=(tooltips or {}).get(key, ""))
            btn.setEnabled(enabled is None or key in enabled)
            btn.clicked.connect(lambda _checked, k=key: self.selected.emit(k))
            self._group.addButton(btn)
            self._layout.addWidget(btn)
            self.buttons[key] = btn

    def current(self) -> str | None:
        for key, btn in self.buttons.items():
            if btn.isChecked():
                return key
        return None

    def select(self, key: str, emit: bool = False) -> bool:
        btn = self.buttons.get(key)
        if btn is None or not btn.isEnabled():
            return False
        btn.setChecked(True)
        if emit:
            self.selected.emit(key)
        return True


# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------
KPI_CARDS = [
    ("sharpe", "Sharpe", f"위험 1단위당 초과수익 · 목표 ≥ {TARGET_SHARPE:g}"),
    ("mdd", "Max drawdown", f"고점 대비 최대 하락 · 목표 |MDD| ≤ {TARGET_MDD * 100:g}%"),
    ("cagr", "CAGR", "연평균 수익률"),
    ("excess", "vs BM1", "시장(KODEX 200TR) 대비 CAGR 차이"),
    ("vol", "Volatility", "수익률 출렁임(연환산)"),
    ("trades", "Trades", "기간 중 매매 횟수"),
]


class KpiRow(QWidget):
    """Six KpiCards for the selected run: one row, or 3 x 2 below
    `wrap_width` px."""

    def __init__(self, wrap_width: int = 960, parent=None):
        super().__init__(parent)
        self._wrap_width = wrap_width
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self.cards = {key: KpiCard(label, help_text) for key, label, help_text in KPI_CARDS}
        for key in ("sharpe", "mdd"):
            self.cards[key].badge.setToolTip(TARGET_SOURCE)
        self._cols = 0
        self._reflow(6)

    def _reflow(self, cols: int):
        if cols == self._cols:
            return
        self._cols = cols
        for card in self.cards.values():
            self._grid.removeWidget(card)
        for i, card in enumerate(self.cards.values()):
            self._grid.addWidget(card, i // cols, i % cols)
        for c in range(6):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, event):
        self._reflow(6 if event.size().width() >= self._wrap_width else 3)
        super().resizeEvent(event)

    def clear(self):
        for card in self.cards.values():
            card.set("-")

    def show_run(self, run):
        m = run.metrics
        judged = run.kind == "strategy"       # benchmarks are not held to the target (no badge)
        sharpe, mdd = m.get("sharpe"), m.get("mdd")
        self.cards["sharpe"].set(fmt_num(sharpe), badge=sharpe_badge(sharpe) if judged else None)
        self.cards["mdd"].set(fmt_pct(mdd), _hex(mdd) if _finite(mdd) and mdd < 0 else None,
                              badge=mdd_badge(mdd) if judged else None)
        cagr = m.get("cagr")
        self.cards["cagr"].set(fmt_pct(cagr), _hex(cagr))
        excess = m.get("bm1_excess_cagr")
        self.cards["excess"].set(fmt_pp(excess), _hex(excess))
        self.cards["vol"].set(fmt_pct(m.get("vol")))
        n = m.get("n_trades")
        self.cards["trades"].set(str(n) if n is not None else "-")


# ---------------------------------------------------------------------------
# Target check card
# ---------------------------------------------------------------------------
class TargetCheckCard(QFrame):
    """The judgements the app can make itself for the selected run: Sharpe,
    MDD, both (criterion H). Robustness (E2) and the look-ahead check (A5)
    are not computed here, so that row always says so instead of quoting a
    fixed verdict from an earlier validation report."""

    ROWS = [
        ("sharpe", f"Sharpe ≥ {TARGET_SHARPE:g}"),
        ("mdd", f"|MDD| ≤ {TARGET_MDD * 100:g}% (전체 구간)"),
        ("both", "두 목표 동시 충족 (H)"),
        ("robust", "강건성 (E2) · 미래정보 (A5)"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DashboardCard")
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)
        title = QLabel("목표 점검")
        title.setObjectName("kpiLabel")
        box.addWidget(title)
        self.subject = QLabel("-")
        self.subject.setObjectName("muted")
        box.addWidget(self.subject)
        self.badges: dict[str, QLabel] = {}
        self.labels: dict[str, QLabel] = {}
        for key, text in self.ROWS:
            row = QHBoxLayout()
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            badge = make_badge("na", "-")
            row.addWidget(lbl, 1)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
            box.addLayout(row)
            self.badges[key] = badge
            self.labels[key] = lbl
        set_badge(self.badges["robust"], "na", "앱에서 미실행")
        self.badges["robust"].setToolTip("독립 검증은 Validation 리포트(NEX-59 등)에서 확인")
        self.badges["sharpe"].setToolTip(TARGET_SOURCE)
        self.badges["mdd"].setToolTip(TARGET_SOURCE)
        box.addStretch()

    def clear(self):
        self.subject.setText("-")
        for key in ("sharpe", "mdd", "both"):
            set_badge(self.badges[key], "na", "-")

    def show_run(self, run):
        self.subject.setText(f"{run.id} · 비용 {run.cost_mult:g}x")
        if run.kind != "strategy":
            for key in ("sharpe", "mdd", "both"):
                set_badge(self.badges[key], "na", "벤치마크")
            return
        m = run.metrics
        set_badge(self.badges["sharpe"], *sharpe_badge(m.get("sharpe")))
        set_badge(self.badges["mdd"], *mdd_badge(m.get("mdd")))
        both = meets_targets(m)
        if both is None:
            set_badge(self.badges["both"], "na", "값 없음")
        else:
            set_badge(self.badges["both"], *(("ok", "✓ 충족") if both else ("fail", "✕ 미충족")))


# ---------------------------------------------------------------------------
# Sharpe bar cell
# ---------------------------------------------------------------------------
SHARPE_ROLE = Qt.ItemDataRole.UserRole       # {"value": float | None, "bench": bool}


class SharpeBarDelegate(CellDelegate):
    """The number on the left, then a bar over 0..`MAX` (ACCENT, benchmarks
    TEXT_EMPTY) with a tick at the target. Negative values draw no bar."""

    MAX = 1.6
    NUM_W = 40

    def paint(self, painter, option, index):
        data = index.data(SHARPE_ROLE) or {}
        value, bench = data.get("value"), data.get("bench", False)
        painter.save()
        self.paint_background(painter, option, index)
        r = option.rect.adjusted(4, 0, -6, 0)
        painter.setFont(option.font)
        painter.setPen(QColor(TEXT_MUTED if bench else TEXT))
        painter.drawText(QRect(r.x(), r.y(), self.NUM_W, r.height()), _RIGHT, fmt_num(value))
        bar = QRect(r.x() + self.NUM_W + 6, r.center().y() - 4, max(r.width() - self.NUM_W - 6, 10), 8)
        if _finite(value) and value > 0:
            w = int(bar.width() * min(value, self.MAX) / self.MAX)
            painter.fillRect(QRect(bar.x(), bar.y(), w, bar.height()), QColor(TEXT_EMPTY if bench else ACCENT))
        tx = bar.x() + int(bar.width() * TARGET_SHARPE / self.MAX)
        painter.setPen(QPen(QColor(LINE_STRONG), 1))
        painter.drawLine(tx, bar.y() - 3, tx, bar.bottom() + 3)
        painter.restore()


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
COMPARISON_HEADERS = ["ID", "Entry", "CAGR", "Vol", f"Sharpe (| = {TARGET_SHARPE:g})", "MDD", "Trades",
                      "vs BM1 (%p)", "Target (H)"]
SHARPE_COL = 4


def _item(text: str, align=_RIGHT, color: str | None = None, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(align)
    if color:
        item.setForeground(QBrush(QColor(color)))
    if tooltip:
        item.setToolTip(tooltip)
    return item


def comparison_rows(result, mult: float) -> list:
    """Strategies in VARIANTS order, then benchmarks by id, at one cost multiplier."""
    order = {vid: i for i, vid in enumerate(VARIANTS)}
    runs = result.runs_at(mult)
    return sorted(runs, key=lambda r: (r.kind != "strategy", order.get(r.id, 99), r.id))


def fill_comparison(table: QTableWidget, runs: list) -> None:
    table.setUpdatesEnabled(False)
    try:
        table.setRowCount(len(runs))
        for i, r in enumerate(runs):
            m = r.metrics
            bench = r.kind != "strategy"
            muted = TEXT_MUTED if bench else None
            mdd = m.get("mdd")
            excess = m.get("bm1_excess_cagr")
            h = None if bench else meets_targets(m)
            sharpe = _item(fmt_num(m.get("sharpe")))
            sharpe.setData(SHARPE_ROLE, {"value": m.get("sharpe"), "bench": bench})
            ok_mdd = mdd_ok(mdd)
            cells = [
                _item(r.id, _LEFT, muted, r.label),
                _item(entry_label(r), _LEFT, muted),
                _item(fmt_pct(m.get("cagr")), color=_hex(m.get("cagr"))),
                _item(fmt_pct(m.get("vol")), color=muted),
                sharpe,
                _item(fmt_pct(mdd), color=None if ok_mdd is None else (OK_TEXT if ok_mdd else FAIL_TEXT)),
                _item(str(m.get("n_trades", "-")), color=muted),
                (_item("-", color=TEXT_MUTED) if r.id == "BM1"
                 else _item(fmt_pp(excess).removesuffix("%p"), color=_hex(excess))),
                _item("-" if h is None else ("✓" if h else "✕"), Qt.AlignmentFlag.AlignCenter,
                      TEXT_MUTED if h is None else (OK_TEXT if h else FAIL_TEXT),
                      "" if bench else f"Sharpe ≥ {TARGET_SHARPE:g} and |MDD| ≤ {TARGET_MDD * 100:g}%"),
            ]
            for c, item in enumerate(cells):
                item.setData(Qt.ItemDataRole.UserRole + 1, r.id)
                table.setItem(i, c, item)
    finally:
        table.setUpdatesEnabled(True)


MONTH_HEADERS = ["Year"] + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] \
    + ["Year total"]


def _heat_item(v) -> QTableWidgetItem:
    if not _finite(v):
        return _item("", color=TEXT_EMPTY)
    item = _item(f"{v * 100:.1f}", color=fg_for(v).name())
    bg = heatmap_bg(v * 100, scale=10)
    if bg is not None:
        item.setBackground(bg)
    return item


def fill_monthly(table: QTableWidget, run) -> None:
    """Year x month returns (%) of one run, the last column the calendar year."""
    table.setUpdatesEnabled(False)
    try:
        if run is None or len(run.nav) == 0:
            table.setRowCount(0)
            return
        monthly = monthly_returns(run.nav, run.dates)
        yearly = yearly_returns(run.nav, run.dates)
        years = sorted(yearly)
        table.setRowCount(len(years))
        for i, y in enumerate(years):
            table.setItem(i, 0, _item(str(y), _LEFT))
            for mth in range(1, 13):
                table.setItem(i, mth, _heat_item(monthly.get((y, mth))))
            table.setItem(i, 13, _heat_item(yearly.get(y)))
    finally:
        table.setUpdatesEnabled(True)


TRADE_HEADERS = ["Entry", "Exit", "Ticker", "Return", "Days", "Reason"]


def fill_trades(table: QTableWidget, run) -> None:
    """One row per closed trade of a strategy run, newest exit first."""
    table.setUpdatesEnabled(False)
    try:
        trades = list(getattr(run, "trades", None) or []) if run is not None and run.kind == "strategy" else []
        trades.sort(key=lambda t: (t.exit_date, t.entry_date), reverse=True)
        table.setRowCount(len(trades))
        for i, t in enumerate(trades):
            ret = t.ret_pct / 100.0
            cells = [
                _item(f"{t.entry_date:%Y-%m-%d}", _LEFT),
                _item(f"{t.exit_date:%Y-%m-%d}", _LEFT),
                _item(t.ticker, _LEFT),
                _item(f"{t.ret_pct:+.1f}%", color=_hex(ret)),
                _item(str(t.holding_days)),
                _item(t.reason, _LEFT),
            ]
            for c, item in enumerate(cells):
                table.setItem(i, c, item)
    finally:
        table.setUpdatesEnabled(True)
