"""ui/trend_following_tab.py — TrendFollowingTab: the research/backtest UI for
strategy.trend_following (trend_following.md, version in SPEC_VERSION).

One form (window, markets, universe, sizing, strategy choice, cost
scenarios, target Sharpe) -> one background run (threads.strategy_threads) -> the result
screen of docs/ui_redesign_2026-11.md P1 (docs/ui.md 8.10):

  run toolbar (reports/strategy_run_settings_ui.md: 전략 | 시장 | 기간 |
  매매전략... | 중지 백테스트, one line or two fixed lines by window width)
  with its status line (summary or error | 홀드아웃 포함 · 개발 구간만) ->
  result head (basis line, "다음 실행에 적용", Save) -> variant and cost
  segments -> six KPI cards of the selected run -> NAV + drawdown chart
  next to the target-check card -> result tabs (variant comparison, all
  metrics, monthly returns, trades, warnings).

The form has no investor-flows or event-study switches (2026-11-15): flows
are loaded whenever a chosen strategy needs them (A3-A5), and the 6-1 event
study is run from the headless runner only.

Picking a variant or a cost multiplier redraws from the result already
received; nothing re-runs. "Save Report" writes the markdown the spec asks to
keep per version. Signal generation / research only: nothing here places
orders (docs/ui.md 5.6 banner). The widgets live in ui/strategy_widgets.py.
"""
from __future__ import annotations

import dataclasses
import logging
import os

import numpy as np
from matplotlib.dates import date2num, num2date
from datetime import date, datetime

from html import escape

from PyQt6.QtCore import Qt, QDate, QSize
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox, QPushButton,
    QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QSplitter, QFrame, QDialog, QTabWidget, QMessageBox, QScrollArea, QSizePolicy, QLayout,
)

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

from i18n import tr
from paths import REPORTS_DIR
from strategy.trend_following import (
    SPEC_VERSION, DEV_END, StrategyParams, VARIANTS, COST_MULTIPLIERS, ResearchRequest, ResearchResult, MARKETS,
)
from strategy.trend_following.research import SUMMARY_COLUMNS
from threads.strategy_threads import TrendFollowingResearchThread, CANCELLED_MESSAGE
from ui.dialogs.trend_conditions import TrendConditions, TrendConditionsDialog
from ui.common import (
    create_font, make_button, setup_table, make_footer_label, install_table_keys, ThreadOwnerMixin,
    TAB_MARGINS, TAB_SPACING, FONT_BODY, BTN_H, remember_splitter,
    fix_toolbar_widths, register_toolbar_labels,
)
from ui.colors import LOSS, WARN, SERIES_BENCH, SERIES_TARGET, DRAWDOWN_ALPHA, SERIES_PALETTE
from ui.theme import TEXT, TEXT_MUTED, TEXT_FAINT, TEXT_EMPTY, LINE
from ui.strategy_widgets import (
    TARGET_MDD, SegmentRow, KpiRow, TargetCheckCard, SharpeBarDelegate, COMPARISON_HEADERS, SHARPE_COL,
    DEFAULT_TARGET_SHARPE, comparison_headers, fmt_target,
    MONTH_HEADERS, comparison_rows, fill_comparison, fill_monthly,
    drawdown_series, sorted_trades, run_label,
)
from ui.dialogs.strategy_trades import StrategyTradesDialog, StrategyTradesView

logger = logging.getLogger(__name__)

_STRATEGY_NAME = tr("추세추종", "Trend Following")
_ID_ROLE = Qt.ItemDataRole.UserRole + 1      # run id on every comparison-table item

# Result tabs, in order (docs/ui_redesign_2026-11.md P1-1 item 5).
TAB_COMPARISON, TAB_ALL, TAB_MONTHLY, TAB_TRADES, TAB_WARNINGS = range(5)

_HOLDOUT_CHIP = tr("홀드아웃 포함", "Includes hold-out")
_TITLE = "Strategy"     # message boxes are titled with the tab name (docs/ui.md 8.5)
_DEV_START = date(2021, 1, 1)      # the development window's default start (spec 6-5)
_GAP = 12               # between toolbar groups (strategy_run_settings_ui.md 1)
_DATE_ERROR = tr("시작일이 종료일보다 앞이어야 합니다", "Start must be before End")
_RULES_REQUIRED = tr("매매전략에서 규칙을 1개 이상 선택하세요", "Select at least one rule in Trading Strategy")
_HOLDOUT_NOTE = tr(f"홀드아웃 구간({DEV_END:%Y-%m-%d} 이후)이 포함된 결과입니다. 스펙 6-5의 홀드아웃은 이 실행으로 열린 것으로 "
                   "봐야 합니다 (trend_following.md 9-23).",
                   f"This result includes the hold-out (after {DEV_END:%Y-%m-%d}). Treat the spec 6-5 hold-out "
                   "as opened by this run (trend_following.md 9-23).")
_TRADES_EMPTY = tr("백테스트를 실행하면 선택한 규칙의 매매 내역이 나옵니다", "Run Backtest to list the selected rule's trades")
_TRADES_EMPTY_BM = tr("벤치마크는 매매 내역이 없습니다. 전략 변형을 고르세요",
                      "Benchmarks have no trades; pick a strategy variant")


# Display labels of the scorecard columns (strategy.trend_following.research
# SUMMARY_COLUMNS, whose English names stay the report's headers); a name not
# listed here (ID, CAGR, Vol, Sharpe, MDD, Calmar, IR, Beta, Alpha, periods) is shown as is.
SUMMARY_LABELS = {
    "Trades": tr("매매 수", "Trades"), "Win": tr("승률", "Win"), "Hold(d)": tr("보유(일)", "Hold(d)"),
    "Turnover": tr("회전율", "Turnover"), "Cost/yr": tr("연 비용", "Cost/yr"), "Cash": tr("현금", "Cash"),
    "vs BM1": tr("BM1 대비", "vs BM1"), "Up cap": tr("상승 포착", "Up cap"),
    "Down cap": tr("하락 포착", "Down cap"),
}


def summary_headers() -> list[str]:
    """The All Metrics table's header labels: ID, cost, then SUMMARY_COLUMNS."""
    return ["ID", tr("비용", "Cost")] + [SUMMARY_LABELS.get(h, h) for h, _ in SUMMARY_COLUMNS[1:]]


def _fit_headers(table: QTableWidget) -> None:
    """Widen a fixed-width column whose header label (Korean or English) would
    be cut; the stretch column is left to the layout."""
    header = table.horizontalHeader()
    for c in range(table.columnCount()):
        if header.sectionResizeMode(c) != QHeaderView.ResizeMode.Stretch:
            table.setColumnWidth(c, max(table.columnWidth(c), header.sectionSizeHint(c)))


def _mult_key(m: float) -> str:
    return f"{m:g}"


class _RunToolbar(QWidget):
    """The run toolbar (reports/strategy_run_settings_ui.md 1, 4).

    One line when everything fits:  전략 · 시장 · 기간 · 조건 ........ 중지 실행
    otherwise two fixed lines:      전략 · 시장 · 조건
                                    기간 ....................... 중지 실행
    The choice depends on the available width only: every control has a
    fixed width that does not follow the selected value, so picking a
    strategy, markets or rules never moves anything or changes the height."""

    def __init__(self, strategy, market, period, conditions, actions, parent=None):
        super().__init__(parent)
        self._groups = dict(strategy=strategy, market=market, period=period, conditions=conditions,
                            actions=actions)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._rows = [QHBoxLayout(), QHBoxLayout()]
        for row in self._rows:
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(_GAP)
            v.addLayout(row)
        self.one_line: bool | None = None
        self._arrange(True)

    @staticmethod
    def _w(widget) -> int:
        # The width the layout will give it: a fixed width wins over the hint
        # (the market button's hint follows its text, its fixed width does not).
        return widget.sizeHint().expandedTo(widget.minimumSize()).boundedTo(widget.maximumSize()).width()

    def _need(self, names) -> int:
        return sum(self._w(self._groups[n]) for n in names) + _GAP * len(names)

    def one_line_width(self) -> int:
        return self._need(("strategy", "market", "period", "conditions", "actions"))

    def two_line_width(self) -> int:
        return max(self._need(("strategy", "market", "conditions")), self._need(("period", "actions")))

    def _arrange(self, one_line: bool):
        if one_line == self.one_line:
            return
        self.one_line = one_line
        for row in self._rows:
            while row.count():
                row.takeAt(0)
        g = self._groups
        if one_line:
            order = [[g["strategy"], g["market"], g["period"], g["conditions"], None, g["actions"]], []]
        else:
            order = [[g["strategy"], g["market"], g["conditions"], None],
                     [g["period"], None, g["actions"]]]
        for row, items in zip(self._rows, order):
            for w in items:
                if w is None:
                    row.addStretch(1)
                else:
                    row.addWidget(w)
        self.updateGeometry()

    def resizeEvent(self, event):
        self._arrange(event.size().width() >= self.one_line_width())
        super().resizeEvent(event)

    def minimumSizeHint(self):
        h = max(w.sizeHint().height() for w in self._groups.values())
        lines = 1 if self.one_line else 2
        return QSize(self.two_line_width(), lines * h + (lines - 1) * 6)

    def sizeHint(self):
        size = self.minimumSizeHint()
        return QSize(self.one_line_width(), size.height())


def _reserve_place(box: QWidget) -> None:
    """One BTN_H line that keeps its place while hidden: the result head and
    the selector row appear with a result, and the cards, chart and result
    tabs under them must not move when they do (user direction 2026-11-16)."""
    box.setFixedHeight(BTN_H)
    pol = box.sizePolicy()
    pol.setRetainSizeWhenHidden(True)
    box.setSizePolicy(pol)


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def _set_role(lbl: QLabel, name: str) -> None:
    if lbl.objectName() != name:
        lbl.setObjectName(name)
        _repolish(lbl)


def _set_invalid(w: QWidget, invalid: bool) -> None:
    """Outline an input the status line asks to fix (theme: [invalid="true"])."""
    if bool(w.property("invalid")) != invalid:
        w.setProperty("invalid", invalid)
        _repolish(w)


class TrendFollowingTab(ThreadOwnerMixin, QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: ResearchResult | None = None
        # The rules and run conditions ("매매전략..."; renamed from "조건..." 2026-11-16); no rule is chosen at first.
        self.conditions = TrendConditions()
        # What "개발 구간만" changed, shown on the status line until the next date edit.
        self._range_note = ""
        self._research_thread = None
        self._running = False
        # Set while the "개발 구간만" link moves the dates, so its range note
        # is not cleared by its own dateChanged signals.
        self._setting_dates = False
        # What the result screen shows: one cost multiplier and one run id of it.
        self._mult: float | None = None
        # Several runs may be selected (docs/ui.md 8.14): `_sel_ids` in selection
        # order for the chart and the comparison table; `_sel_id` is the focus
        # the cards, the target check and the monthly / trade tabs show.
        self._sel_ids: list[str] = []
        self._sel_id: str | None = None
        self._syncing = False
        # The Sharpe target the shown result is judged against, and the form's
        # value captured at Run for the result that run will bring.
        self.target_sharpe = DEFAULT_TARGET_SHARPE
        self._pending_target_sharpe = DEFAULT_TARGET_SHARPE
        # Hover readout state: (label, color, x as matplotlib date numbers, y) per plotted series.
        self._chart_series: list[tuple[str, str, np.ndarray, np.ndarray]] = []
        self._crosshair = None
        self._crosshair_dd = None
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(TAB_SPACING)
        root.setContentsMargins(*TAB_MARGINS)
        # No in-tab title (docs/ui.md 8.4): no other tab has one and the tab
        # label already names it. What a run does is on the Run button's tooltip.
        self._run_tooltip = tr(f"trend_following.md {SPEC_VERSION} 기준 연구용 백테스트: 변형 "
                               f"{', '.join(VARIANTS)}을 BM1–BM4와 0·1·2배 비용 시나리오로 비교합니다.",
                               f"Research backtest per trend_following.md {SPEC_VERSION}: variants "
                               f"{', '.join(VARIANTS)} against BM1–BM4 with the 0x/1x/2x cost scenarios.")

        root.addWidget(self._build_run_card())
        root.addWidget(self._build_result_head())
        self.selector_row = self._build_selector_row()
        root.addWidget(self.selector_row)

        self.kpi_row = KpiRow()
        root.addWidget(self.kpi_row)
        # Target check: one summary line, details on demand (8.16).
        self.target_card = TargetCheckCard()
        root.addWidget(self.target_card)

        # Chart over the result tabs at full width; the handle sets the split.
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_chart())
        splitter.addWidget(self._build_result_tabs())
        splitter.setStretchFactor(0, 55)
        splitter.setStretchFactor(1, 45)
        # 55:45 to start, then the user's last split (docs/ui.md 8.17).
        remember_splitter(splitter, "strategy_chart_results", [550, 450])
        self.result_splitter = splitter
        root.addWidget(splitter, 1)

        # Footer: status and errors in their own line, never over the results
        # (docs/ui.md 5.2), under the results like every tab's footer (8.4).
        self.status_lbl = make_footer_label(tr("준비됨", "Ready"))
        root.addWidget(self.status_lbl)

        banner = QFrame()
        banner.setObjectName("DashboardCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        notice = QLabel(tr("연구·백테스트용 신호 생성기이며 투자 권유가 아닙니다. 백테스트는 현재 KOSPI 구성 종목"
                           "(생존 편향)과 trend_following.md 4-1의 비용 가정을 쓰며, 이 화면은 주문을 내지 않습니다.",
                           "Research and backtesting signal generator, not investment advice. Backtests use today's "
                           "KOSPI constituents (survivorship bias) and the cost assumptions of trend_following.md 4-1; "
                           "nothing here places orders."))
        notice.setWordWrap(True)
        notice.setFont(create_font(FONT_BODY))
        # Information, not an error: muted text on the card, not DANGER red (docs/ui.md 8.3).
        notice.setObjectName("muted")
        banner_layout.addWidget(notice)
        root.addWidget(banner)

        # The range note of "개발 구간만" goes as soon as the user edits a date.
        for edit in (self.start_edit, self.end_edit):
            edit.dateChanged.connect(self._on_date_edited)
            edit.dateChanged.connect(self._on_form_changed)
        self._set_tab_order()
        self._on_form_changed()

    def _build_run_card(self) -> QFrame:
        """The toolbar and its one-line status line in one card."""
        card = QFrame()
        card.setObjectName("DashboardCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 8, 12, 6)
        v.setSpacing(4)

        # 1. Strategy: one selector instead of sub-tabs and a card title. The
        # host (StrategyTab) fills it with every strategy and switches pages.
        self.strategy_combo = QComboBox()
        self.strategy_combo.setToolTip(tr("백테스트할 전략", "Strategy to backtest"))
        self.set_strategy_choices([_STRATEGY_NAME], 0)

        # 2. Markets (spec 9-25): one toggle per market, any combination, at
        # least one -- the Universe market toggles' look (user direction
        # 2026-11-16, docs/ui.md 8.17; it was a menu button with checks).
        self.market_buttons: dict[str, QPushButton] = {}
        self.market_box = QWidget()
        ml = QHBoxLayout(self.market_box)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        for m in MARKETS:
            btn = make_button(m, checkable=True, checked=(m == "KOSPI"),
                              tooltip=tr(f"{m}: 켜면 이 시장 종목도 백테스트 유니버스에 넣습니다. 여러 시장을 고르면 "
                                         "선택한 시장 전체를 원화 시가총액 합산 순위로 종목을 고릅니다. 최소 한 시장은 켜져 있어야 합니다.",
                                         f"{m}: when on, this market's names join the backtest universe. With several "
                                         "markets, names are picked from one KRW market-cap ranking of all of them. "
                                         "At least one market must stay on."))
            btn.toggled.connect(lambda checked, m=m: self._on_market_toggled(m, checked))
            self.market_buttons[m] = btn
            ml.addWidget(btn)

        # 3. Window: two dates of one width side by side, the short label left.
        # The window ends today by default; past DEV_END is the hold-out
        # (spec 6-5, 9-23).
        today = QDate.currentDate()
        self.start_edit = QDateEdit(QDate(_DEV_START.year, _DEV_START.month, _DEV_START.day))
        self.end_edit = QDateEdit(today)
        for de in (self.start_edit, self.end_edit):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
            de.setMaximumDate(today)
        date_w = max(de.sizeHint().width() for de in (self.start_edit, self.end_edit))
        for de in (self.start_edit, self.end_edit):
            de.setFixedWidth(date_w)
        self.end_edit.setToolTip(tr(f"{DEV_END:%Y-%m-%d} 이후는 홀드아웃 구간입니다 (trend_following.md 6-5). "
                                    "넘기면 홀드아웃을 포함해 실행하고 결과에 표시합니다.",
                                    f"After {DEV_END:%Y-%m-%d} is the hold-out (trend_following.md 6-5). "
                                    "An end past it runs with the hold-out and marks the result."))
        self.period_box = QWidget()
        pl = QHBoxLayout(self.period_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        pl.addWidget(QLabel(tr("기간", "Period")))
        pl.addWidget(self.start_edit)
        pl.addWidget(QLabel("~"))
        pl.addWidget(self.end_edit)

        # 4. Conditions: the same place and label for every strategy.
        self.conditions_btn = make_button(tr("매매전략...", "Trading Strategy..."), on_click=self._on_conditions_clicked,
                                          tooltip=tr("규칙(A0~A5, B, C1~C3)과 종목·보유·비용·평가 조건을 고릅니다",
                                                     "Choose the rules (A0-A5, B, C1-C3) and the universe, "
                                                     "holding, cost and evaluation conditions"))

        # 5. Run control, right: both places exist before any run; only enabled flips.
        self.stop_btn = make_button(tr("중지", "Stop"), on_click=self._on_stop_clicked)
        self.stop_btn.setEnabled(False)
        self.run_btn = make_button(tr("백테스트", "Backtest"), "primary", self._on_run_clicked, tooltip=self._run_tooltip)
        self.actions_box = QWidget()
        al = QHBoxLayout(self.actions_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        al.addWidget(self.stop_btn)
        al.addWidget(self.run_btn)
        # Saving belongs to the shown result: it sits on the result head.
        self.save_btn = make_button(tr("리포트 저장", "Save Report"), on_click=self._on_save_clicked,
                                    tooltip=tr("지금 표시 중인 결과(현재 결과 기준 줄)를 reports/에 저장합니다",
                                               "Save the shown result (the basis line above) to reports/"))
        self.save_btn.setEnabled(False)
        # Back to the empty screen after a run (user direction 2026-11-16):
        # also on the result head, so it exists only while a result is shown.
        self.reset_btn = make_button(tr("초기화", "Reset"), on_click=self._on_reset_clicked,
                                     tooltip=tr("표시 중인 결과를 지우고 실행 전 화면으로 돌아갑니다. "
                                                "시장·기간·매매전략은 그대로 둡니다.",
                                                "Clear the shown result and go back to the screen before a run; "
                                                "markets, period and trading strategy stay."))
        self.reset_btn.setEnabled(False)

        # One button width with the other tabs' toolbars (user direction
        # 2026-11-16; the labels are registered at the bottom of this module).
        fix_toolbar_widths([*self.market_buttons.values(), self.conditions_btn, self.stop_btn, self.run_btn,
                            self.reset_btn, self.save_btn])
        self.toolbar = _RunToolbar(self.strategy_combo, self.market_box, self.period_box,
                                   self.conditions_btn, self.actions_box)
        v.addWidget(self.toolbar)
        v.addWidget(self._build_status_line())
        return card

    def _build_status_line(self) -> QWidget:
        """One line under the toolbar: the current choice, or what to fix;
        on the right a reserved place for the hold-out note and its action."""
        box = QWidget()
        box.setFixedHeight(BTN_H)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.status_summary_lbl = QLabel("")
        self.status_summary_lbl.setObjectName("muted")
        # A long summary is cut, it never widens the tab.
        self.status_summary_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.holdout_lbl = QLabel(_HOLDOUT_CHIP)
        self.holdout_lbl.setObjectName("warn")
        self.holdout_lbl.setToolTip(self.end_edit.toolTip())
        self.dev_only_btn = make_button(tr("개발 구간만", "Development window only"), "link", self._on_dev_only_clicked,
                                        tooltip=tr(f"종료일을 개발 구간 끝({DEV_END:%Y-%m-%d})으로 맞춥니다",
                                                   f"Set the end to the end of the development window "
                                                   f"({DEV_END:%Y-%m-%d})"))
        self.dev_only_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        for w in (self.holdout_lbl, self.dev_only_btn):
            pol = w.sizePolicy()
            pol.setRetainSizeWhenHidden(True)          # the place stays reserved
            w.setSizePolicy(pol)
        row.addWidget(self.status_summary_lbl, 1)
        row.addWidget(self.holdout_lbl)
        row.addWidget(self.dev_only_btn)
        return box

    def set_strategy_choices(self, names, current: int) -> None:
        """The strategy list (StrategyTab passes every registered strategy).
        Fixed width from the longest name, capped; a longer one is cut and
        shown whole on the tooltip, so it never changes the toolbar's lines."""
        self.strategy_combo.blockSignals(True)
        self.strategy_combo.clear()
        self.strategy_combo.addItems(list(names))
        self.strategy_combo.setCurrentIndex(current)
        self.strategy_combo.blockSignals(False)
        fm = self.strategy_combo.fontMetrics()
        self.strategy_combo.setFixedWidth(min(max(fm.horizontalAdvance(n) for n in names) + 48, 200))
        self.strategy_combo.setToolTip(" / ".join(names))

    def _set_tab_order(self):
        """전략 -> 시장 -> 시작일 -> 종료일 -> 조건 -> 실행 (proposal 1)."""
        chain = [self.strategy_combo, *self.market_buttons.values(), self.start_edit, self.end_edit, self.conditions_btn,
                 self.run_btn, self.stop_btn, self.dev_only_btn]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)

    def run_cost_mults(self) -> tuple:
        """The cost multipliers the conditions ask for (4-1)."""
        return self.conditions.cost_multipliers()

    def selected_markets(self) -> tuple[str, ...]:
        return tuple(m for m in MARKETS if self.market_buttons[m].isChecked())

    def _on_market_toggled(self, market: str, checked: bool):
        if not checked and not self.selected_markets():
            self.market_buttons[market].setChecked(True)     # at least one market stays selected
            return
        self._on_form_changed()

    def includes_holdout(self) -> bool:
        return self.end_edit.date().toPyDate() > DEV_END

    def _on_date_edited(self, *_):
        if not self._setting_dates:
            self._range_note = ""

    def _on_dev_only_clicked(self):
        """End on DEV_END; a start that leaves no valid window falls back to
        the development default 2021-01-01 ~ DEV_END, and the status says so."""
        end = QDate(DEV_END.year, DEV_END.month, DEV_END.day)
        self._setting_dates = True
        try:
            moved_start = self.start_edit.date() >= end
            if moved_start:
                self.start_edit.setDate(QDate(_DEV_START.year, _DEV_START.month, _DEV_START.day))
            self.end_edit.setDate(end)
        finally:
            self._setting_dates = False
        self._range_note = (tr(f"기간을 개발 기본 구간 {_DEV_START:%Y-%m-%d} ~ {DEV_END:%Y-%m-%d}로 바꿨습니다",
                               f"Period reset to the development default {_DEV_START:%Y-%m-%d} ~ "
                               f"{DEV_END:%Y-%m-%d}")
                            if moved_start else "")
        self._on_form_changed()

    # ------------------------------------------------------------ validation
    def validation_errors(self) -> dict[str, str]:
        """What keeps the form from running, by field ('period', 'rules')."""
        errors = {}
        if self.start_edit.date() >= self.end_edit.date():
            errors["period"] = _DATE_ERROR
        if not self._selected_variants():
            errors["rules"] = _RULES_REQUIRED
        return errors

    def status_summary(self) -> str:
        """'규칙 2개 · 시총 상위 200 · 비용 1배만 · 목표 Sharpe ≥ 1.0' (+ markets)."""
        c = self.conditions
        parts = [tr(f"규칙 {len(c.variant_ids)}개", f"{len(c.variant_ids)} rules"),
                 tr(f"시총 상위 {c.universe_size}", f"Market-cap top {c.universe_size}"),
                 tr(f"비용 {c.cost_label()}", f"Cost {c.cost_label()}"),
                 tr(f"목표 Sharpe ≥ {fmt_target(c.target_sharpe)}", f"Target Sharpe ≥ {fmt_target(c.target_sharpe)}")]
        markets = self.selected_markets()
        if len(markets) > 1:
            parts.append(tr(f"{'+'.join(markets)} 원화 시총 합산", f"{'+'.join(markets)} by combined KRW market cap"))
        return " · ".join(parts)

    def _on_form_changed(self, *_):
        """Re-check on every edit (proposal 3): the status line shows the
        summary, or how to fix an error with the related input outlined;
        the hold-out place, Run's state and the stale-result note follow."""
        errors = self.validation_errors()
        if errors:
            text, role = " · ".join(errors.values()), "fieldError"
        elif self._range_note:
            text, role = self._range_note, "warn"
        else:
            text, role = self.status_summary(), "muted"
        self.status_summary_lbl.setText(text)
        self.status_summary_lbl.setToolTip(text)
        _set_role(self.status_summary_lbl, role)
        _set_invalid(self.conditions_btn, "rules" in errors)
        for de in (self.start_edit, self.end_edit):
            _set_invalid(de, "period" in errors)
        holdout = self.includes_holdout()
        self.holdout_lbl.setVisible(holdout)
        self.dev_only_btn.setVisible(holdout)
        self._update_run_state()
        self._update_pending()

    def _update_run_state(self):
        errors = self.validation_errors()
        self.run_btn.setEnabled(not errors and not self._running)
        tip = self._run_tooltip
        if errors and not self._running:
            tip = " · ".join(errors.values()) + "\n\n" + tip
        self.run_btn.setToolTip(tip)
        # A run's inputs are fixed while it goes; switching strategy stays possible.
        for w in (*self.market_buttons.values(), self.start_edit, self.end_edit, self.conditions_btn,
                  self.dev_only_btn):
            w.setEnabled(not self._running)
        if hasattr(self, "reset_btn"):
            self.reset_btn.setEnabled(self._result is not None and not self._running)

    def _build_selector_row(self) -> QWidget:
        """Variant segments (one per run id of the shown cost) and the cost
        segments; hidden until a result exists."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        # 비교 대상 (many) and 상세 기준 (one) are separate controls
        # (docs/ui.md 8.17): a segment click only adds or removes a run.
        lbl = QLabel(tr("비교 대상", "Compare"))
        lbl.setObjectName("muted")
        lbl.setToolTip(tr("차트·비교표에 함께 그릴 실행(전략과 BM 모두). 버튼을 누르면 추가하거나 뺍니다(마지막 하나는 남음). "
                          "비교표에서는 Ctrl/Shift 클릭으로 여러 행을 고릅니다.",
                          "Runs drawn together in the chart and comparison table (strategies and BMs). A click "
                          "adds or removes one (the last one stays). In the table, Ctrl/Shift-click picks "
                          "several rows."))
        row.addWidget(lbl)
        self.variant_seg = SegmentRow(width=52, multi=True)
        self.variant_seg.toggled_key.connect(self._on_variant_toggled)
        row.addWidget(self.variant_seg)
        row.addSpacing(14)
        lbl = QLabel(tr("상세 기준", "Detail"))
        lbl.setObjectName("muted")
        lbl.setToolTip(tr("KPI 카드·목표 점검·월별 수익률·매매 내역이 보여 줄 실행(비교 대상 중 하나). "
                          "버튼에는 진한 테두리로 표시됩니다.",
                          "The run the KPI cards, target check, monthly returns and trades show (one of the "
                          "compared runs). Its button has a bold outline."))
        row.addWidget(lbl)
        self.focus_combo = QComboBox()
        self.focus_combo.setMinimumWidth(80)
        self.focus_combo.activated.connect(self._on_focus_picked)
        row.addWidget(self.focus_combo)
        row.addSpacing(14)
        lbl = QLabel(tr("표시 비용", "Cost"))
        lbl.setObjectName("muted")
        lbl.setToolTip(tr("결과를 보여 줄 비용 배수(실행 비용에 포함된 것만 고를 수 있음)",
                          "The cost multiplier the results are shown at (only those the run included)"))
        row.addWidget(lbl)
        self.cost_seg = SegmentRow(width=40)
        self.cost_seg.selected.connect(self._on_cost_picked)
        row.addWidget(self.cost_seg)
        row.addStretch()
        _reserve_place(box)
        box.setVisible(False)
        return box

    def _build_result_head(self) -> QWidget:
        """Under the card: what the shown result was computed with (from the
        result, never the form), "다음 실행에 적용" when the form has moved on,
        and 초기화 · 리포트 저장 on the right. Hidden until a result exists."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.basis_lbl = QLabel("")
        self.basis_lbl.setObjectName("resultBasis")
        # One line, cut when long (the whole text on the tooltip): a wrapped
        # second line would push the cards and result tabs down.
        self.basis_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.pending_lbl = QLabel(tr("다음 실행에 적용", "Applies to next run"))
        self.pending_lbl.setObjectName("warn")
        self.pending_lbl.setToolTip(tr("실행 설정이 지금 결과와 다릅니다. 바뀐 값은 다음 백테스트 실행에 적용됩니다.",
                                       "The run settings differ from the shown result; the changes apply to "
                                       "the next backtest run."))
        self.pending_lbl.setVisible(False)
        row.addWidget(self.basis_lbl, 1)
        row.addWidget(self.pending_lbl)
        row.addWidget(self.reset_btn)
        row.addWidget(self.save_btn)
        _reserve_place(box)
        box.setVisible(False)
        self.result_head = box
        return box

    def _on_focus_picked(self, index: int):
        run_id = self.focus_combo.itemData(index)
        if run_id:
            self.set_selection(self._sel_ids, run_id)

    def _build_chart(self) -> QWidget:
        self.fig = Figure(figsize=(6, 3.2), constrained_layout=True)
        self.ax, self.ax_dd = self.fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        self._draw_empty_chart()
        self.canvas = FigureCanvas(self.fig)
        self.canvas.setMinimumHeight(220)
        self.canvas.mpl_connect("motion_notify_event", self._on_chart_motion)
        self.canvas.mpl_connect("axes_leave_event", self._on_chart_leave)
        self.canvas.mpl_connect("figure_leave_event", self._on_chart_leave)
        # Values under the mouse (latest bar when the mouse is off the chart).
        self.readout_lbl = QLabel("")
        self.readout_lbl.setFont(create_font(FONT_BODY))
        self.readout_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.readout_lbl.setWordWrap(True)
        self.show_all_cb = QCheckBox(tr("전체 표시", "Show all"))
        self.show_all_cb.setToolTip(tr("이 비용의 다른 변형과 벤치마크도 회색 가는 선으로 그립니다",
                                       "Also draw every other variant and benchmark at this cost, thin and grey"))
        self.show_all_cb.toggled.connect(self._redraw_chart)
        head = QHBoxLayout()
        head.addWidget(self.readout_lbl, 1)
        head.addWidget(self.show_all_cb, 0, Qt.AlignmentFlag.AlignTop)
        box = QFrame()
        box.setObjectName("DashboardCard")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.canvas, 1)
        return box

    def _build_result_tabs(self) -> QTabWidget:
        self.result_tabs = QTabWidget()
        self.result_tabs.setObjectName("ResultTabs")   # theme.py colors the last (Warnings) tab
        self.result_tabs.setProperty("warn", False)

        self.result_tabs.addTab(self._build_compare_table(), tr("변형 비교", "Variant Comparison"))
        self.result_tabs.addTab(self._build_metrics_table(), tr("전체 지표", "All Metrics"))
        self.result_tabs.addTab(self._build_monthly_table(), tr("월별 수익률", "Monthly Returns"))
        self.result_tabs.addTab(self._build_trades_view(), tr("매매 내역", "Trades"))
        self.result_tabs.addTab(self._build_warnings_view(), tr("경고 (0)", "Warnings (0)"))
        return self.result_tabs

    def _build_compare_table(self) -> QTableWidget:
        self.compare_table = setup_table(QTableWidget(0, len(COMPARISON_HEADERS)),
                                         empty_text=tr("백테스트를 실행하면 규칙과 벤치마크를 비교합니다",
                                                        "Run Backtest to compare the rules and benchmarks"))
        self.compare_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Ctrl / Shift-click selects several rows: the same selection as the segments.
        self.compare_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.compare_table.setHorizontalHeaderLabels(COMPARISON_HEADERS)
        for c, w in enumerate((50, 190, 70, 64, 170, 70, 60, 84, 76)):
            self.compare_table.setColumnWidth(c, w)
        # The description column takes the spare width, not the narrow verdict column.
        self.compare_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        _fit_headers(self.compare_table)
        self._sharpe_delegate = SharpeBarDelegate(self.compare_table)
        self.compare_table.setItemDelegateForColumn(SHARPE_COL, self._sharpe_delegate)
        self.compare_table.itemSelectionChanged.connect(self._on_compare_selection_changed)
        install_table_keys(self.compare_table)
        return self.compare_table

    def _build_metrics_table(self) -> QTableWidget:
        """Every scorecard column for every run and cost (the table this tab
        had before the 2026-11 redesign, unchanged)."""
        self.table = setup_table(QTableWidget(), empty_text=tr("백테스트를 실행하면 성과표가 채워집니다",
                                                                 "Run Backtest to fill the scorecard"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        headers = summary_headers()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(72)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 50)
        _fit_headers(self.table)
        install_table_keys(self.table)      # Ctrl+C copies the selected rows as TSV (docs/ui.md 8.6)
        return self.table

    def _build_monthly_table(self) -> QTableWidget:
        self.monthly_table = setup_table(QTableWidget(0, len(MONTH_HEADERS)),
                                         empty_text=tr("백테스트를 실행하면 선택한 규칙의 월별 수익률(%)이 나옵니다",
                                                        "Run Backtest to see the selected rule's monthly returns (%)"))
        self.monthly_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.monthly_table.setHorizontalHeaderLabels(MONTH_HEADERS)
        self.monthly_table.horizontalHeader().setDefaultSectionSize(58)
        self.monthly_table.setColumnWidth(len(MONTH_HEADERS) - 1, 80)
        _fit_headers(self.monthly_table)
        install_table_keys(self.monthly_table)
        return self.monthly_table

    def _build_trades_view(self) -> StrategyTradesView:
        """The same list as the popup (StrategyTradesView), without its
        search and reason filter (user direction 2026-11-16): the summary
        line with "자세히" (was "크게 보기") at its right end. 자세히, a
        double-click or Enter on a row opens StrategyTradesDialog."""
        self.trades_detail_btn = make_button(tr("자세히", "Details"), on_click=self.open_trades_dialog,
                                             tooltip=tr("같은 매매 내역을 모든 열이 한 번에 보이는 큰 창으로 엽니다"
                                                        "(종목 검색 · 청산 사유 필터 포함)",
                                                        "Open the same trades in a larger window with every "
                                                        "column visible (with the name search and exit-reason filter)"))
        self.trades_detail_btn.setEnabled(False)
        self.trades_view = StrategyTradesView(empty_text=_TRADES_EMPTY, on_activate=self._on_trade_activated,
                                              extra_widgets=(self.trades_detail_btn,), filters=False)
        self.trades_view.setContentsMargins(6, 6, 6, 0)
        self.trades_table = self.trades_view.table
        self.trades_summary_lbl = self.trades_view.summary_lbl
        return self.trades_view

    def _build_warnings_view(self) -> QScrollArea:
        """The shown result's caveats (benchmark substitutions, flow coverage,
        skipped variants), readable here instead of only in the saved report
        (review_codex.md S3)."""
        self.warn_lbl = QLabel(tr("경고 없음.", "No warnings."))
        self.warn_lbl.setObjectName("warn")
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.warn_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warn_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.warn_lbl.setContentsMargins(8, 8, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.warn_lbl)
        return scroll

    def _draw_empty_chart(self):
        for ax in (self.ax, self.ax_dd):
            ax.clear()
            ax.set_axis_off()
        # Empty state (docs/ui.md 8.2): a hint instead of a bare 0-1 axis.
        self.ax.text(0.5, 0.5, tr("백테스트를 실행하면 선택한 규칙의 NAV와 낙폭을 BM1과 함께 그립니다",
                                 "Run Backtest to plot the selected rule's NAV and drawdown against BM1"),
                     ha="center", va="center", transform=self.ax.transAxes, color=TEXT_FAINT,
                     fontsize=FONT_BODY)

    # --------------------------------------------------------------- request
    def _selected_variants(self) -> tuple[str, ...]:
        """The rules a run will backtest, in VARIANTS order."""
        return self.conditions.variant_ids

    def needs_flows(self) -> bool:
        """Investor flows are loaded only when a chosen rule uses them (A3-A5)."""
        return any(VARIANTS[vid].needs_flows for vid in self._selected_variants())


    # ---Language switch (docs/ui.md 8.20): MainWindow rebuilds every tab in
    # the new language and carries this state over. Plain values only (the
    # ui classes themselves are re-imported).
    def is_running(self) -> bool:
        return bool(self._running)

    def export_state(self) -> dict:
        return {
            "conditions": dataclasses.asdict(self.conditions),
            "markets": self.selected_markets(),
            "start": self.start_edit.date().toPyDate(), "end": self.end_edit.date().toPyDate(),
            "result": self._result, "target_sharpe": self.target_sharpe,
            "mult": self._mult, "sel_ids": list(self._sel_ids), "sel_id": self._sel_id,
        }

    def import_state(self, state: dict) -> None:
        self.set_conditions(TrendConditions(**state["conditions"]))
        wanted = set(state["markets"]) or {"KOSPI"}
        for m in MARKETS:                      # switch the wanted ones on first: one always stays on
            if m in wanted:
                self.market_buttons[m].setChecked(True)
        for m in MARKETS:
            if m not in wanted:
                self.market_buttons[m].setChecked(False)
        for edit, d in ((self.start_edit, state["start"]), (self.end_edit, state["end"])):
            edit.setDate(QDate(d.year, d.month, d.day))
        result = state.get("result")
        if result is not None:
            # Redraw the last finished result in the new language, as a run's
            # finish does (_on_research_finished), then its view selection.
            self._result = result
            self.save_btn.setEnabled(True)
            self.set_target_sharpe(state["target_sharpe"])
            self.populate(result)
            if state.get("mult") is not None:
                self.set_cost(state["mult"])
            if state.get("sel_ids"):
                self.set_selection(state["sel_ids"], focus=state.get("sel_id"))
            self._update_basis()
            self._update_pending()
            self._show_warnings(result)
        self._update_run_state()

    def set_conditions(self, conditions: TrendConditions) -> None:
        self.conditions = conditions
        self._on_form_changed()

    def set_variant_choice(self, ids) -> None:
        """Replace only the rule choice of the conditions."""
        self.set_conditions(dataclasses.replace(self.conditions, variant_ids=tuple(ids)))

    def _on_conditions_clicked(self):
        """취소 keeps the conditions; 적용 replaces them. Focus returns to the button."""
        dlg = TrendConditionsDialog(self.conditions, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.set_conditions(dlg.conditions())
        self.conditions_btn.setFocus()

    def _form_params(self) -> StrategyParams:
        return self.conditions.params()

    def build_request(self) -> ResearchRequest:
        start = self.start_edit.date().toPyDate()
        end = self.end_edit.date().toPyDate()
        params = self._form_params()
        mults = self.run_cost_mults()
        return ResearchRequest(
            start=start, end=end, params=params, variant_ids=self._selected_variants(),
            cost_multipliers=tuple(mults), include_flows=self.needs_flows(),
            run_backtests=True, run_event_study=False,
            allow_holdout=end > DEV_END, markets=self.selected_markets(),
        )

    def _on_run_clicked(self):
        # Run is disabled while the form is invalid; the status line says
        # why, so a stray call only refreshes it.
        if self.validation_errors() or self._running:
            self._on_form_changed()
            return
        req = self.build_request()
        self._pending_target_sharpe = float(self.conditions.target_sharpe)
        thread = TrendFollowingResearchThread(req)
        thread.progress.connect(self._on_progress)
        thread.finished.connect(self._on_research_finished)
        self._track_thread(thread, "_research_thread")
        self._running = True
        self._update_run_state()
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText(tr("시작하는 중...", "Starting..."))
        thread.start()

    def _on_stop_clicked(self):
        """Ask the running research thread to stop at its next check
        (review_agy.md 3.4, 2026-09-29); _on_research_finished re-enables Run."""
        thread = self._research_thread
        if thread is not None and thread.isRunning():
            thread.cancel()
            self.stop_btn.setEnabled(False)
            self.status_lbl.setText(tr("현재 단계가 끝나면 중지합니다...", "Stopping after the current step..."))

    def _on_progress(self, text: str):
        self.status_lbl.setText(text)

    def _on_research_finished(self, result, error: str):
        """A failed or stopped run keeps the last successful result (and its
        Save); Run is re-enabled from the current form's check (proposal 4)."""
        self._running = False
        self.stop_btn.setEnabled(False)
        self._update_run_state()
        if result is None:
            self.status_lbl.setText(error if error == CANCELLED_MESSAGE else tr(f"실행 실패: {error}", f"Run failed: {error}"))
            return
        self._result = result
        self.save_btn.setEnabled(True)
        self.reset_btn.setEnabled(True)
        self.set_target_sharpe(self._pending_target_sharpe)
        self.populate(result)
        self._update_basis()
        self._update_pending()
        self._show_warnings(result)
        n_runs = len(result.runs)
        skipped_ids = ', '.join(result.skipped)
        skipped = tr(f" · 건너뜀 {skipped_ids}", f" · skipped {skipped_ids}") if result.skipped else ""
        n_names = result.dataset_info.get('n_tickers', 0)
        window = f"{result.dataset_info.get('eval_start', '')} → {result.dataset_info.get('eval_end', '')}"
        self.status_lbl.setText(tr(
            f"완료 {result.created_at:%H:%M:%S} · 실행 {n_runs}개 · 종목 {n_names}개 · {window}{skipped}",
            f"Done {result.created_at:%H:%M:%S} · {n_runs} runs · {n_names} names · {window}{skipped}"))

    def _on_reset_clicked(self):
        """초기화: asks first (a run can take minutes), then clear_result()."""
        if self._result is None or self._running:
            return
        answer = QMessageBox.question(
            self, _TITLE, tr("표시 중인 백테스트 결과를 지우고 처음 화면으로 돌아갈까요?\n"
                             "저장하지 않은 결과는 다시 실행해야 볼 수 있습니다.",
                             "Clear the shown backtest result and go back to the start screen?\n"
                             "An unsaved result needs another run to see again."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_result()

    def clear_result(self):
        """The screen before any run: no result, empty cards, tables, chart and
        warnings, result head and selector row hidden. The form (markets,
        period, conditions) is left as it is."""
        if self._running:
            return
        self._result = None
        self._mult, self._sel_id, self._sel_ids = None, None, []
        self.save_btn.setEnabled(False)
        self.reset_btn.setEnabled(False)
        self.set_target_sharpe(DEFAULT_TARGET_SHARPE)
        self.selector_row.setVisible(False)
        self.kpi_row.clear()
        self.target_card.clear()
        self.compare_table.setRowCount(0)
        self.table.setRowCount(0)
        fill_monthly(self.monthly_table, None)
        self.trades_table._empty_hint.text = _TRADES_EMPTY
        self.trades_view.set_trades([])
        self.trades_detail_btn.setEnabled(False)
        self._chart_series = []
        self._draw_empty_chart()
        self._update_readout(None)
        self.canvas.draw_idle()
        self.warn_lbl.setText(tr("경고 없음.", "No warnings."))
        self.result_tabs.setTabText(TAB_WARNINGS, tr("경고 (0)", "Warnings (0)"))
        self.result_tabs.setProperty("warn", False)
        bar = self.result_tabs.tabBar()
        bar.style().unpolish(bar)
        bar.style().polish(bar)
        self.result_tabs.setCurrentIndex(TAB_COMPARISON)
        self._update_basis()
        self._update_pending()
        self._update_run_state()
        self.status_lbl.setText(tr("준비됨", "Ready"))

    def set_target_sharpe(self, target: float):
        """Judge the shown result against `target` (cards, target check,
        comparison table); the form's value is applied at Run."""
        self.target_sharpe = float(target)
        self.kpi_row.set_target_sharpe(self.target_sharpe)
        self.target_card.set_target_sharpe(self.target_sharpe)
        self._sharpe_delegate.target = self.target_sharpe
        self.compare_table.setHorizontalHeaderLabels(comparison_headers(self.target_sharpe))
        _fit_headers(self.compare_table)
        self.compare_table.viewport().update()

    # -------------------------------------------------------------- warnings
    def _show_warnings(self, result: ResearchResult):
        """Fill the Warnings tab from `result` (not from the current form)."""
        lines = [tr(f"{escape(vid)} 미실행: {escape(why)}", f"{escape(vid)} not run: {escape(why)}")
                 for vid, why in result.skipped.items()]
        lines += [escape(w) for w in result.warnings]
        if result.request.allow_holdout:
            lines.insert(0, escape(_HOLDOUT_NOTE))
        self.warn_lbl.setText("<br>".join(f"• {line}" for line in lines) if lines else tr("경고 없음.", "No warnings."))
        self.result_tabs.setTabText(TAB_WARNINGS, tr(f"경고 ({len(lines)})", f"Warnings ({len(lines)})"))
        self.result_tabs.tabBar().setTabTextColor(TAB_WARNINGS, QColor(WARN if lines else TEXT_MUTED))
        # The app QSS sets every tab's color, which wins over setTabTextColor; a
        # property rule on the last tab (QTabBar::tab:last) carries the WARN color.
        self.result_tabs.setProperty("warn", bool(lines))
        bar = self.result_tabs.tabBar()
        bar.style().unpolish(bar)
        bar.style().polish(bar)
        bar.update()

    # --------------------------------------------------------------- results
    def populate(self, result: ResearchResult):
        self._result = result
        self._fill_summary(result)
        mults = sorted({r.cost_mult for r in result.runs})
        keys = sorted(set(COST_MULTIPLIERS) | set(mults))
        self.cost_seg.set_keys([_mult_key(m) for m in keys], labels={_mult_key(m): f"{m:g}x" for m in keys},
                               enabled={_mult_key(m) for m in mults})
        self.selector_row.setVisible(bool(result.runs))
        if not result.runs:
            self._mult, self._sel_id, self._sel_ids = None, None, []
            self.kpi_row.clear()
            self.target_card.clear()
            self.compare_table.setRowCount(0)
            fill_monthly(self.monthly_table, None)
            self.trades_view.set_trades([])
            self.trades_detail_btn.setEnabled(False)
            self._chart_series = []
            self._draw_empty_chart()
            self._update_readout(None)
            self.canvas.draw_idle()
            self._update_basis()
            return
        self._sel_id, self._sel_ids = None, []
        self.set_cost(1.0 if 1.0 in mults else mults[0])

    def selected_run(self):
        """The focused run (cards, target check, monthly / trade tabs)."""
        if self._result is None or self._mult is None:
            return None
        return next((r for r in self._result.runs_at(self._mult) if r.id == self._sel_id), None)

    def selected_runs(self) -> list:
        """Every selected run at the shown cost, in selection order."""
        if self._result is None or self._mult is None:
            return []
        by_id = {r.id: r for r in self._result.runs_at(self._mult)}
        return [by_id[i] for i in self._sel_ids if i in by_id]

    def set_cost(self, mult: float):
        """Show the runs at `mult`; keep the selected id when it ran at that
        cost, else pick the strategy with the best Sharpe (an initial choice
        only, never shown as a ranking)."""
        if self._result is None or not self._result.runs_at(mult):
            return
        self._mult = mult
        self.cost_seg.select(_mult_key(mult))
        rows = comparison_rows(self._result, mult)
        first_bm = next((r.id for r in rows if r.kind != "strategy"), None)
        self.variant_seg.set_keys([r.id for r in rows], tooltips={r.id: run_label(r) for r in rows},
                                  group_starts={first_bm})
        self._syncing = True
        try:
            fill_comparison(self.compare_table, rows, self.target_sharpe)
        finally:
            self._syncing = False
        ids = [r.id for r in rows]
        keep = [i for i in self._sel_ids if i in ids]
        if keep:
            self.set_selection(keep, self._sel_id if self._sel_id in keep else keep[-1], force=True)
        else:
            best = self._default_id(rows)
            first = [best] + (["BM1"] if "BM1" in ids and best != "BM1" else [])
            self.set_selection(first, best, force=True)

    @staticmethod
    def _default_id(rows) -> str:
        strategies = [r for r in rows if r.kind == "strategy"]
        if strategies:
            def key(r):
                s = r.metrics.get("sharpe")
                return s if isinstance(s, (int, float)) and np.isfinite(s) else -np.inf
            return max(strategies, key=key).id
        return next((r.id for r in rows if r.id == "BM1"), rows[0].id)

    def select_variant(self, run_id: str):
        """Make `run_id` the focus, adding it to the end of the selection when it is
        new. An already selected run keeps its place, so its chart colour stays."""
        if self._result is None or run_id == self._sel_id:
            return
        ids = self._sel_ids if run_id in self._sel_ids else self._sel_ids + [run_id]
        self.set_selection(ids, run_id)

    def toggle_variant(self, run_id: str, on: bool):
        """A segment click (비교 대상, docs/ui.md 8.17): add the run to the
        comparison or take it out, never the last one. The detail focus does
        not move unless the focus itself is taken out; it is picked in the
        상세 기준 combo."""
        if on:
            if self._result is not None and run_id not in self._sel_ids:
                self.set_selection(self._sel_ids + [run_id], self._sel_id)
            return
        rest = [i for i in self._sel_ids if i != run_id]
        if not rest:
            self._sync_selection_widgets()      # undo the button's own uncheck
            return
        self.set_selection(rest, self._sel_id if self._sel_id in rest else rest[-1])

    def set_selection(self, ids, focus: str | None = None, force: bool = False):
        """Select `ids` (runs of the shown cost) with `focus` as the run the cards
        and per-variant tabs show; segments, table rows and chart follow."""
        if self._result is None or self._mult is None:
            return
        avail = {r.id for r in self._result.runs_at(self._mult)}
        ids = [i for i in dict.fromkeys(ids) if i in avail]
        if not ids:
            return
        focus = focus if focus in ids else ids[-1]
        focus_changed = focus != self._sel_id or force
        self._sel_ids, self._sel_id = ids, focus
        self._sync_selection_widgets()
        if focus_changed:
            self._show_focus(self.selected_run())
        self._update_basis()
        self._redraw_chart()

    def _sync_selection_widgets(self):
        self.variant_seg.set_checked(self._sel_ids)
        self.variant_seg.set_focus(self._sel_id)
        self.focus_combo.blockSignals(True)
        try:
            self.focus_combo.clear()
            for rid in self._sel_ids:
                self.focus_combo.addItem(rid, rid)
            self.focus_combo.setCurrentIndex(max(0, self.focus_combo.findData(self._sel_id)))
        finally:
            self.focus_combo.blockSignals(False)
        table = self.compare_table
        model = table.selectionModel()
        self._syncing = True
        try:
            table.clearSelection()
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item is None:
                    continue
                rid = item.data(_ID_ROLE)
                if rid in self._sel_ids:
                    idx = table.model().index(row, 0)
                    model.select(idx, model.SelectionFlag.Select | model.SelectionFlag.Rows)
                    if rid == self._sel_id:
                        model.setCurrentIndex(table.model().index(row, max(table.currentColumn(), 0)),
                                              model.SelectionFlag.NoUpdate)
        finally:
            self._syncing = False

    def basis_text(self) -> str:
        """'현재 결과 기준 · strategy · markets top N · period · rules · cost ·
        target · focus', read from the shown result (its request and data
        window), never from the form."""
        result = self._result
        if result is None or not result.runs:
            return ""
        req, info = result.request, result.dataset_info
        start = info.get("eval_start") or req.start
        end = info.get("eval_end") or req.end or ""
        markets = "+".join(info.get("markets") or req.markets)
        parts = [_STRATEGY_NAME, tr(f"{markets} 상위 {req.params.universe_size}", f"{markets} top {req.params.universe_size}"),
                 f"{start} ~ {end}"]
        if req.variant_ids:
            parts.append(tr("규칙 ", "Rules ") + ", ".join(req.variant_ids))
        if self._mult is not None:
            parts.append(tr(f"비용 {self._mult:g}x", f"Cost {self._mult:g}x"))
        parts.append(tr(f"목표 Sharpe ≥ {fmt_target(self.target_sharpe)}", f"Target Sharpe ≥ {fmt_target(self.target_sharpe)}"))
        if self._sel_id:
            parts.append(tr(f"상세 기준 {self._sel_id}", f"Detail {self._sel_id}"))
        if req.allow_holdout:
            parts.append(_HOLDOUT_CHIP)
        return tr("현재 결과 기준 · ", "Shown result · ") + " · ".join(parts)

    def _update_basis(self):
        text = self.basis_text()
        self.basis_lbl.setText(text)
        self.basis_lbl.setToolTip(text)
        self.result_head.setVisible(bool(text))

    def _form_key(self) -> tuple:
        """The form's run inputs, comparable with _request_key()."""
        return (self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate(), self._form_params(),
                self._selected_variants(), tuple(self.run_cost_mults()), self.selected_markets(),
                round(float(self.conditions.target_sharpe), 1))

    def _request_key(self) -> tuple | None:
        if self._result is None:
            return None
        req = self._result.request
        return (req.start, req.end, req.params, tuple(req.variant_ids), tuple(req.cost_multipliers),
                tuple(req.markets), round(float(self.target_sharpe), 1))

    def _update_pending(self):
        """'다음 실행에 적용' while the form differs from the shown result's run."""
        if not hasattr(self, "pending_lbl"):
            return
        key = self._request_key()
        self.pending_lbl.setVisible(key is not None and key != self._form_key())

    def _show_focus(self, run):
        self.kpi_row.show_run(run)
        self.target_card.show_run(run)
        fill_monthly(self.monthly_table, run)
        self.trades_table._empty_hint.text = _TRADES_EMPTY if run.kind == "strategy" else _TRADES_EMPTY_BM
        info = self._result.dataset_info
        trades = sorted_trades(run)
        self.trades_view.set_trades(trades, info.get("names"), info.get("ticker_markets"))
        self.trades_detail_btn.setEnabled(bool(trades))

    # ------------------------------------------------------- trade list popup
    def open_trades_dialog(self, *_, row: int | None = None):
        """The selected run's trades in StrategyTradesDialog; the trade on `row`
        of the tab's list (as sorted there) is pre-selected. Modeless."""
        run = self.selected_run()
        trades = sorted_trades(run)
        if not trades:
            return None
        pick = self.trades_view._row_trade(row) if row is not None and 0 <= row < self.trades_table.rowCount() else None
        info = self._result.dataset_info
        dlg = StrategyTradesDialog(
            tr(f"{run.id} · 비용 {run.cost_mult:g}x · {run_label(run)}", f"{run.id} · cost {run.cost_mult:g}x · {run.label}"), trades, info.get("names"), info.get("ticker_markets"),
            select_ticker=pick.ticker if pick else None, select_exit=pick.exit_date if pick else None, parent=self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        self._trades_dialog = dlg      # keep a reference while it is open
        return dlg

    def _on_trade_activated(self, row: int, _col: int = 0):
        self.open_trades_dialog(row=row)

    def _on_variant_toggled(self, run_id: str, checked: bool):
        self.toggle_variant(run_id, checked)

    def _on_cost_picked(self, key: str):
        self.set_cost(float(key))

    def _on_compare_selection_changed(self):
        """Rows picked in the comparison table (Ctrl / Shift for several) become
        the selection; the current row is the focus."""
        if self._syncing:
            return
        table = self.compare_table
        rows = sorted({i.row() for i in table.selectedIndexes()})
        ids = [table.item(r, 0).data(_ID_ROLE) for r in rows if table.item(r, 0) is not None]
        if not ids:
            return
        cur = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        focus = cur.data(_ID_ROLE) if cur is not None and cur.data(_ID_ROLE) in ids else ids[-1]
        self.set_selection(ids, focus)

    def _fill_summary(self, result: ResearchResult):
        rows = sorted(result.runs, key=lambda r: (r.cost_mult, r.kind != "strategy", r.id))
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                values = [r.id, f"{r.cost_mult:g}x"] + [fn(r) for _, fn in SUMMARY_COLUMNS[1:]]
                for c, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                                          if c >= 2 else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    if r.kind == "benchmark":
                        item.setForeground(QBrush(QColor(TEXT_MUTED)))
                    if c == 0:
                        item.setToolTip(run_label(r))
                    self.table.setItem(i, c, item)
        finally:
            self.table.setUpdatesEnabled(True)

    # ----------------------------------------------------------------- chart
    def _redraw_chart(self, *_):
        """NAV (top) and drawdown (bottom) of every selected run (docs/ui.md 8.14):
        strategies solid, benchmarks dashed, BM1 in SERIES_BENCH, the rest one
        SERIES_PALETTE hue each in selection order; the focus is thickest, drawn
        last and filled in the drawdown panel. "전체 표시" adds the unselected
        runs as thin grey lines."""
        run = self.selected_run()
        if run is None:
            return
        ax, ax_dd = self.ax, self.ax_dd
        ax.clear()
        ax_dd.clear()
        ax.set_axis_on()
        ax_dd.set_axis_on()
        self._chart_series = []
        self._crosshair = self._crosshair_dd = None
        runs = self._result.runs_at(self._mult)
        chosen = self.selected_runs()
        self.series_colors = self._series_colors(chosen)
        series = []

        def plot(r, color, width, style="-", legend=True):
            if len(r.nav) == 0:
                return
            line, = ax.plot(r.dates, r.nav * 100.0, linestyle=style, linewidth=width, color=color,
                            label=r.id if legend else f"_{r.id}")
            series.append((r.id, line.get_color(), np.asarray(date2num(r.dates), dtype=float),
                           np.asarray(r.nav, dtype=float) * 100.0))

        if self.show_all_cb.isChecked():
            for r in runs:
                if r.id not in self.series_colors:
                    plot(r, TEXT_EMPTY, 0.9, legend=False)
        others = [r for r in chosen if r is not run]
        for r in others:
            plot(r, self.series_colors[r.id], 1.5, "--" if r.kind != "strategy" else "-")
        plot(run, self.series_colors[run.id], 2.4, "--" if run.kind != "strategy" else "-")
        # The readout lists the focus first, then the other selected runs, then the rest.
        by_id = {sr[0]: sr for sr in series}
        order = [run.id] + [r.id for r in others]
        self._chart_series = [by_id[i] for i in order if i in by_id] + [sr for sr in series if sr[0] not in order]

        dd_low = -TARGET_MDD * 100
        for r in others:
            if len(r.nav):
                dd = drawdown_series(r.nav) * 100.0
                ax_dd.plot(r.dates, dd, color=self.series_colors[r.id], linewidth=0.9,
                           linestyle="--" if r.kind != "strategy" else "-")
                dd_low = min(dd_low, float(np.nanmin(dd)))
        if len(run.nav):
            dd = drawdown_series(run.nav) * 100.0
            ax_dd.fill_between(run.dates, dd, 0.0, color=LOSS, alpha=DRAWDOWN_ALPHA, linewidth=0)
            ax_dd.plot(run.dates, dd, color=LOSS, linewidth=0.8)
            dd_low = min(dd_low, float(np.nanmin(dd)))
        ax_dd.set_ylim(dd_low * 1.15, 1.0)

        ax.set_title(tr(f"NAV (시작 = 100) · 비용 {self._mult:g}x", f"NAV rebased to 100 · {self._mult:g}x costs"), fontsize=FONT_BODY, loc="left", color=TEXT)
        ax.legend(fontsize=FONT_BODY - 1, frameon=False, loc="upper left",
                  ncol=max(1, min(4, (len(chosen) + 3) // 4)))
        ax_dd.axhline(-TARGET_MDD * 100, color=SERIES_TARGET, linestyle="--", linewidth=1.0)
        ax_dd.set_ylabel(tr("낙폭 %", "Drawdown %"), fontsize=FONT_BODY - 1, color=TEXT_MUTED)

        for a in (ax, ax_dd):
            a.grid(True, color=LINE, linewidth=0.6)
            a.tick_params(labelsize=FONT_BODY - 1, colors=TEXT_MUTED)
            for spine in ("top", "right"):
                a.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                a.spines[spine].set_color(LINE)
        ax.tick_params(labelbottom=False)
        if self._chart_series:
            x0 = self._chart_series[0][2][-1]
            self._crosshair = ax.axvline(x0, color=TEXT_MUTED, linewidth=0.8, linestyle="--",
                                         label="_nolegend_", visible=False)
            self._crosshair_dd = ax_dd.axvline(x0, color=TEXT_MUTED, linewidth=0.8, linestyle="--",
                                               label="_nolegend_", visible=False)
        self._update_readout(None)
        self.canvas.draw_idle()

    @staticmethod
    def _series_colors(chosen) -> dict:
        """Run id -> line colour: BM1 SERIES_BENCH, the others SERIES_PALETTE in selection order."""
        out, k = {}, 0
        for r in chosen:
            if r.id == "BM1":
                out[r.id] = SERIES_BENCH
            else:
                out[r.id] = SERIES_PALETTE[k % len(SERIES_PALETTE)]
                k += 1
        return out

    # ----------------------------------------------------------- chart hover
    def _on_chart_motion(self, event):
        if event.inaxes not in (self.ax, self.ax_dd) or event.xdata is None or not self._chart_series:
            return
        x = self._update_readout(float(event.xdata))
        if self._crosshair is not None and x is not None:
            for line in (self._crosshair, self._crosshair_dd):
                line.set_xdata([x, x])
                line.set_visible(True)
            self.canvas.draw_idle()

    def _on_chart_leave(self, _event):
        if self._crosshair is not None and self._crosshair.get_visible():
            self._crosshair.set_visible(False)
            self._crosshair_dd.set_visible(False)
            self.canvas.draw_idle()
        self._update_readout(None)

    def _update_readout(self, xdata: float | None) -> float | None:
        """Show every series' NAV at the trading day nearest `xdata` (the last
        day when None) and return that day's x, or None when nothing is plotted."""
        if not self._chart_series:
            self.readout_lbl.setText("")
            return None
        # Snap to the nearest date of the longest series, then read each series at
        # its own last date on or before it (benchmarks may start later or end earlier).
        ref_x = max(self._chart_series, key=lambda s: len(s[2]))[2]
        if xdata is None:
            x = float(ref_x[-1])
        else:
            i = int(np.clip(np.searchsorted(ref_x, xdata), 1, len(ref_x) - 1)) if len(ref_x) > 1 else 0
            if len(ref_x) > 1 and abs(ref_x[i - 1] - xdata) <= abs(ref_x[i] - xdata):
                i -= 1
            x = float(ref_x[i])
        parts = [f'<span style="color:{TEXT_MUTED};">{num2date(x):%Y-%m-%d}</span>']
        for label, color, xs, ys in self._chart_series:
            j = int(np.searchsorted(xs, x, side="right")) - 1
            if j < 0 or not np.isfinite(ys[j]):
                continue
            parts.append(f'<span style="color:{color};">&#9644;</span>&nbsp;'
                         f'<span style="color:{TEXT_MUTED};">{label}</span>&nbsp;'
                         f'<b style="color:{TEXT};">{ys[j]:.1f}</b>')
        self.readout_lbl.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        return x

    # ---------------------------------------------------------------- report
    def _on_save_clicked(self):
        if self._result is None:
            return
        os.makedirs(REPORTS_DIR, exist_ok=True)
        default = os.path.join(REPORTS_DIR, f"trend_following_{SPEC_VERSION}_{datetime.now():%Y%m%d_%H%M}.md")
        path, _ = QFileDialog.getSaveFileName(self, tr("백테스트 리포트 저장", "Save backtest report"), default, "Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                header = [f"- **{_HOLDOUT_NOTE}**"] if self._result.request.allow_holdout else None
                f.write(self._result.to_markdown(header))
            self.status_lbl.setText(tr(f"리포트 저장됨: {path}", f"Report saved: {path}"))
        except Exception as e:
            logger.warning("Failed to save the Trend Following report", exc_info=True)
            self.status_lbl.setText(tr(f"저장 실패: {e}", f"Save failed: {e}"))

    def default_window(self) -> tuple[date, date]:
        return self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate()


# Every label this tab's toolbar buttons can show (the toolbar button width
# shared with Universe, History and Total Assets; ui.common.toolbar_button_width).
register_toolbar_labels(
    *MARKETS, tr("매매전략...", "Trading Strategy..."), tr("중지", "Stop"), tr("백테스트", "Backtest"),
    tr("리포트 저장", "Save Report"), tr("초기화", "Reset"),
)
