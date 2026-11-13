"""ui/trend_following_tab.py — TrendFollowingTab: the research/backtest UI for
strategy.trend_following (trend_following.md, version in SPEC_VERSION).

One form (window, universe, sizing, variant matrix, cost scenarios, flows,
event study) -> one background run (threads.strategy_threads) -> the result
screen of docs/ui_redesign_2026-11.md P1 (docs/ui.md 8.10):

  run-settings card (chips; "Edit settings" opens the form) -> variant and
  cost segments -> six KPI cards of the selected run -> NAV + drawdown chart
  next to the target-check card -> result tabs (variant comparison, all
  metrics, monthly returns, trades, event study, warnings).

Picking a variant or a cost multiplier redraws from the result already
received; nothing re-runs. "Save Report" writes the markdown the spec asks to
keep per version. Signal generation / research only: nothing here places
orders (docs/ui.md 5.6 banner). The widgets live in ui/strategy_widgets.py.
"""
from __future__ import annotations

import logging
import os

import numpy as np
from matplotlib.dates import date2num, num2date
from datetime import date, datetime

from html import escape

from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QSpinBox, QDateEdit, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
    QSplitter, QFrame, QDialog, QTabWidget, QScrollArea, QSizePolicy, QGridLayout,
)

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

from paths import REPORTS_DIR
from strategy.trend_following import (
    SPEC_VERSION, DEV_END, StrategyParams, VARIANTS, COST_MULTIPLIERS, ResearchRequest, ResearchResult, MARKETS,
)
from strategy.trend_following.research import SUMMARY_COLUMNS
from strategy.trend_following.event_study import HORIZONS
from threads.strategy_threads import TrendFollowingResearchThread, CANCELLED_MESSAGE
from ui.dialogs.variant_picker import VariantPickerDialog
from ui.common import (
    create_font, make_button, setup_table, make_footer_label, install_table_keys, ThreadOwnerMixin,
    TAB_MARGINS, TAB_SPACING, FONT_SMALL, FlowLayout, BTN_H,
)
from ui.colors import LOSS, WARN, SERIES_BENCH, SERIES_TARGET, DRAWDOWN_ALPHA, SERIES_PALETTE
from ui.theme import TEXT, TEXT_MUTED, TEXT_FAINT, TEXT_EMPTY, LINE
from ui.strategy_widgets import (
    TARGET_MDD, SegmentRow, KpiRow, TargetCheckCard, SharpeBarDelegate, COMPARISON_HEADERS, SHARPE_COL,
    MONTH_HEADERS, TRADE_HEADERS, comparison_rows, fill_comparison, fill_monthly, fill_trades,
    drawdown_series, sorted_trades, trade_summary,
)
from ui.dialogs.strategy_trades import StrategyTradesDialog

logger = logging.getLogger(__name__)

_WEEKDAYS = ["월", "화", "수", "목", "금"]
_COST_CHOICES = [("0x / 1x / 2x", COST_MULTIPLIERS), ("1x만", (1.0,))]
_ID_ROLE = Qt.ItemDataRole.UserRole + 1      # run id on every comparison-table item

# Result tabs, in order (docs/ui_redesign_2026-11.md P1-1 item 5).
TAB_COMPARISON, TAB_ALL, TAB_MONTHLY, TAB_TRADES, TAB_EVENTS, TAB_WARNINGS = range(6)

_VARIANTS_LBL_MIN_W = 180
_HOLDOUT_CHIP = "홀드아웃 포함"
_HOLDOUT_NOTE = (f"홀드아웃 구간({DEV_END:%Y-%m-%d} 이후)이 포함된 결과입니다. 스펙 6-5의 홀드아웃은 이 실행으로 열린 것으로 "
                 "봐야 합니다 (trend_following.md 9-23).")
_TRADES_EMPTY = "리서치를 실행하면 선택한 변형의 매매 내역이 나옵니다"
_TRADES_EMPTY_BM = "벤치마크는 매매 내역이 없습니다. 전략 변형을 고르세요"


def _fmt_event(value, pct=True, digits=1) -> str:
    if value is None:
        return "-"
    try:
        if value != value:
            return "-"
    except Exception:
        return "-"
    return f"{value * 100:.{digits}f}%" if pct else str(value)


def _mult_key(m: float) -> str:
    return f"{m:g}"


class TrendFollowingTab(ThreadOwnerMixin, QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: ResearchResult | None = None
        # The user's variant choice. Flow-dependent ones (A3-A5) stay in it while
        # Investor flows is off but are not sent; ticking flows brings them back.
        self._chosen_variants: set[str] = set(VARIANTS)
        self._research_thread = None
        # What the result screen shows: one cost multiplier and one run id of it.
        self._mult: float | None = None
        # Several runs may be selected (docs/ui.md 8.14): `_sel_ids` in selection
        # order for the chart and the comparison table; `_sel_id` is the focus
        # the cards, the target check and the monthly / trade tabs show.
        self._sel_ids: list[str] = []
        self._sel_id: str | None = None
        self._syncing = False
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
        self._run_tooltip = (f"Research backtest per trend_following.md {SPEC_VERSION}: variants "
                             f"{', '.join(VARIANTS)} against BM1–BM4 with the 0x/1x/2x cost scenarios "
                             f"and the 6-1 event study.")

        root.addWidget(self._build_settings_card())
        self.selector_row = self._build_selector_row()
        root.addWidget(self.selector_row)

        self.kpi_row = KpiRow()
        root.addWidget(self.kpi_row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(TAB_SPACING)
        top_layout.addWidget(self._build_chart(), 2)
        self.target_card = TargetCheckCard()
        top_layout.addWidget(self.target_card, 1)
        splitter.addWidget(top)
        splitter.addWidget(self._build_result_tabs())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        # Footer: status and errors in their own line, never over the results
        # (docs/ui.md 5.2), under the results like every tab's footer (8.4).
        self.status_lbl = make_footer_label("Ready")
        root.addWidget(self.status_lbl)

        banner = QFrame()
        banner.setObjectName("DashboardCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        notice = QLabel("Research and backtesting signal generator, not investment advice. Backtests use today's "
                        "KOSPI constituents (survivorship bias) and the cost assumptions of trend_following.md 4-1; "
                        "nothing here places orders.")
        notice.setWordWrap(True)
        notice.setFont(create_font(FONT_SMALL))
        # Information, not an error: muted text on the card, not DANGER red (docs/ui.md 8.3).
        notice.setObjectName("muted")
        banner_layout.addWidget(notice)
        root.addWidget(banner)

        for sig in (self.start_edit.dateChanged, self.end_edit.dateChanged, self.universe_spin.valueChanged,
                    self.liquidity_spin.valueChanged, self.positions_spin.valueChanged,
                    self.weekday_combo.currentIndexChanged, self.cost_combo.currentIndexChanged,
                    self.flows_cb.toggled, self.event_cb.toggled):
            sig.connect(self._update_chips)
        # A3-A5 cannot run without investor flows: the selection shown and sent
        # is what a run will actually do (review_codex.md S1).
        self.flows_cb.toggled.connect(self._update_variants_label)
        self._update_variants_label()

    def _build_settings_card(self) -> QFrame:
        """The run settings in one card: folded, a line of chips summing up the
        form; unfolded ("설정 편집"), the form itself where the chips were: two
        lines on a normal window, more when it is narrow (FlowLayout). The
        actions sit on the right either way, Run (the one primary) last
        (docs/ui.md 8.4 / 8.12)."""
        card = QFrame()
        card.setObjectName("DashboardCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(10, 6, 10, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        title = QLabel("실행 설정")
        title.setObjectName("kpiLabel")
        title.setFixedHeight(BTN_H)          # level with the first line of controls
        grid.addWidget(title, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.chips: list[QLabel] = []
        self.chips_box = QWidget()
        # Chips summarise the form; on a narrow window the last ones are cut
        # rather than widening the whole tab.
        self.chips_box.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._chips_layout = QHBoxLayout(self.chips_box)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(4)
        self._chips_layout.addStretch()
        grid.addWidget(self.chips_box, 0, 1)
        # The form takes the place of the chips and wraps: its minimum width is
        # one group of controls, not a whole row. It spans the card to the right
        # edge, with the first line kept clear of the buttons laid over that
        # corner, so the lines after it run under them.
        self.form_box = self._build_form()
        grid.addWidget(self.form_box, 0, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        # Open by default, and it stays open after a run so the next choice is
        # one click away (user direction 2026-11-13; docs/ui.md 8.12). The
        # user folds it to the chip line with this button.
        self.edit_btn = make_button("설정 편집", on_click=self._on_edit_toggled, checkable=True, checked=True,
                                    tooltip="실행 설정 입력란을 펼치거나 접습니다")
        # Stop cancels a run, it does not destroy anything, so it stays neutral (8.1).
        self.run_btn = make_button("리서치 실행", "primary", self._on_run_clicked, tooltip=self._run_tooltip)
        self.stop_btn = make_button("중지", on_click=self._on_stop_clicked)
        self.stop_btn.setEnabled(False)
        self.save_btn = make_button("리포트 저장", on_click=self._on_save_clicked)
        self.save_btn.setEnabled(False)
        for btn in (self.edit_btn, self.save_btn, self.stop_btn, self.run_btn):
            buttons.addWidget(btn)
        grid.addLayout(buttons, 0, 2, Qt.AlignmentFlag.AlignTop)
        self.form_flow.set_first_line_inset(
            sum(b.sizeHint().width() for b in (self.edit_btn, self.save_btn, self.stop_btn, self.run_btn))
            + 3 * buttons.spacing() + grid.horizontalSpacing())
        self.chips_box.setVisible(False)
        return card

    def _build_form(self) -> QWidget:
        """Groups of controls in a FlowLayout (docs/ui.md 8.12): the window, the
        markets, the universe and portfolio settings, costs, the two data
        switches and the variants, wrapping line by line."""
        # The window ends today by default. Anything after DEV_END is the
        # spec's hold-out (6-5): such a request carries allow_holdout and the
        # chip, the form and the report say so (spec 9-23).
        today = QDate.currentDate()
        self.start_edit = QDateEdit(QDate(2021, 1, 1))
        self.end_edit = QDateEdit(today)
        for de in (self.start_edit, self.end_edit):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
            de.setMaximumDate(today)
        self.end_edit.setToolTip(f"{DEV_END:%Y-%m-%d} 이후는 홀드아웃 구간입니다 (trend_following.md 6-5). "
                                 "넘기면 홀드아웃을 포함해 실행하고 결과에 표시합니다.")
        self.holdout_lbl = QLabel("홀드아웃 포함")
        self.holdout_lbl.setObjectName("warn")
        self.holdout_lbl.setToolTip(self.end_edit.toolTip())

        self.universe_spin = QSpinBox()
        self.universe_spin.setRange(20, 300)
        self.universe_spin.setSingleStep(10)
        self.universe_spin.setValue(StrategyParams.universe_size)
        self.universe_spin.setToolTip("선택한 시장 전체에서 시가총액(원화 환산) 상위 N개 종목")

        # Markets the universe is drawn from (spec 9-25): any combination, at least one.
        # Several markets are ranked together by KRW market cap.
        self.market_buttons = {}
        for m in MARKETS:
            btn = make_button(m, checkable=True, checked=(m == "KOSPI"),
                              tooltip="여러 시장을 고르면 선택한 시장 전체에서 시가총액 순으로 종목을 고릅니다. "
                                      "미국 종목은 원화로 환산하고, 국면 필터는 종목별 자기 시장 지수로 판정합니다.")
            btn.clicked.connect(lambda checked, mk=m: self._on_market_toggled(mk, checked))
            self.market_buttons[m] = btn

        self.liquidity_spin = QSpinBox()
        self.liquidity_spin.setRange(0, 5000)
        self.liquidity_spin.setSingleStep(10)
        self.liquidity_spin.setValue(int(StrategyParams.min_avg_trading_value / 1e8))
        self.liquidity_spin.setSuffix(" 억")
        self.liquidity_spin.setToolTip("20일 평균 거래대금 하한")

        self.positions_spin = QSpinBox()
        self.positions_spin.setRange(3, 12)
        self.positions_spin.setValue(StrategyParams.max_positions)

        self.weekday_combo = QComboBox()
        self.weekday_combo.addItems(_WEEKDAYS)
        self.weekday_combo.setCurrentIndex(StrategyParams.check_weekday)
        self.weekday_combo.setToolTip("주간 신호 점검 요일")

        self.cost_combo = QComboBox()
        for label, _ in _COST_CHOICES:
            self.cost_combo.addItem(label)
        self.cost_combo.setToolTip("trend_following.md 4-1 비용 민감도")

        self.flows_cb = QCheckBox("투자자 수급")
        self.flows_cb.setToolTip("네이버 투자자별 매매 동향으로 F/FX 신호 계산(한국 종목만). 처음 실행은 느림")
        self.event_cb = QCheckBox("이벤트 스터디")
        self.event_cb.setToolTip("trend_following.md 6-1 돌파 이벤트 스터디")
        self.event_cb.setChecked(True)

        # The variants are picked in a dialog that also explains each one
        # (ui.dialogs.VariantPickerDialog); the label shows what is selected.
        self.variants_btn = make_button("변형 선택...", on_click=self._on_variants_clicked,
                                        tooltip="백테스트할 전략 변형을 고르고 각 규칙 설명을 봅니다")
        self.variants_lbl = QLabel("")
        # A long selection ("A0, A1, ... need flows") must not set the minimum
        # width of the tab: it counts as _VARIANTS_LBL_MIN_W in the flow, takes the
        # rest of its line, is clipped beyond that and has the full text on its tooltip.
        self.variants_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.variants_lbl.setMinimumWidth(_VARIANTS_LBL_MIN_W)

        def _group(*widgets) -> QWidget:
            """A label and its controls that stay together when the form wraps."""
            box = QWidget()
            line = QHBoxLayout(box)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(6)
            for w in widgets:
                line.addWidget(QLabel(w) if isinstance(w, str) else w)
            return box

        form = QWidget()
        self.form_flow = FlowLayout(form)
        self.form_groups = [
            _group("기간", self.start_edit, "~", self.end_edit, self.holdout_lbl),
            _group("시장", *self.market_buttons.values()),
            _group("유니버스", self.universe_spin),
            _group("유동성 하한", self.liquidity_spin),
            _group("최대 보유", self.positions_spin),
            _group("점검 요일", self.weekday_combo),
            _group("비용", self.cost_combo),
            _group(self.flows_cb, self.event_cb),
            self.variants_btn,
            self.variants_lbl,
        ]
        for g in self.form_groups:
            self.form_flow.addWidget(g)
        self.end_edit.dateChanged.connect(self._update_holdout)
        self._update_holdout()
        return form

    def selected_markets(self) -> tuple[str, ...]:
        return tuple(m for m in MARKETS if self.market_buttons[m].isChecked())

    def _on_market_toggled(self, market: str, checked: bool):
        if not checked and not self.selected_markets():
            self.market_buttons[market].setChecked(True)     # at least one market stays selected
            return
        self._update_chips()

    def includes_holdout(self) -> bool:
        return self.end_edit.date().toPyDate() > DEV_END

    def _update_holdout(self, *_):
        self.holdout_lbl.setVisible(self.includes_holdout())

    def _build_selector_row(self) -> QWidget:
        """Variant segments (one per run id of the shown cost) and the cost
        segments; hidden until a result exists."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        lbl = QLabel("변형")
        lbl.setObjectName("muted")
        lbl.setToolTip("여러 개를 고를 수 있습니다(전략과 BM 모두). 차트·비교표에 함께 표시되고, 진한 테두리의 "
                       "기준 변형이 카드·목표 점검·월별 수익률·매매 내역에 보입니다. "
                       "선택 안 된 버튼: 추가하고 기준으로 · 선택된 버튼: 기준으로 · 기준 버튼: 선택 해제. "
                       "비교표에서는 Ctrl/Shift 클릭으로 여러 행을 고릅니다.")
        row.addWidget(lbl)
        self.variant_seg = SegmentRow(width=52, multi=True)
        self.variant_seg.toggled_key.connect(self._on_variant_toggled)
        row.addWidget(self.variant_seg)
        row.addStretch()
        lbl = QLabel("비용")
        lbl.setObjectName("muted")
        row.addWidget(lbl)
        self.cost_seg = SegmentRow(width=40)
        self.cost_seg.selected.connect(self._on_cost_picked)
        row.addWidget(self.cost_seg)
        box.setVisible(False)
        return box

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
        self.readout_lbl.setFont(create_font(FONT_SMALL))
        self.readout_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.readout_lbl.setWordWrap(True)
        self.show_all_cb = QCheckBox("전체 표시")
        self.show_all_cb.setToolTip("이 비용의 다른 변형과 벤치마크도 회색 가는 선으로 그립니다")
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

        self.compare_table = setup_table(QTableWidget(0, len(COMPARISON_HEADERS)),
                                         empty_text="리서치를 실행하면 변형을 비교합니다")
        self.compare_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Ctrl / Shift-click selects several rows: the same selection as the segments.
        self.compare_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.compare_table.setHorizontalHeaderLabels(COMPARISON_HEADERS)
        for c, w in enumerate((50, 190, 70, 64, 170, 70, 60, 84, 76)):
            self.compare_table.setColumnWidth(c, w)
        # The description column takes the spare width, not the narrow verdict column.
        self.compare_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._sharpe_delegate = SharpeBarDelegate(self.compare_table)
        self.compare_table.setItemDelegateForColumn(SHARPE_COL, self._sharpe_delegate)
        self.compare_table.itemSelectionChanged.connect(self._on_compare_selection_changed)
        install_table_keys(self.compare_table)
        self.result_tabs.addTab(self.compare_table, "변형 비교")

        # Every scorecard column for every run and cost (the table this tab had
        # before the 2026-11 redesign, unchanged).
        self.table = setup_table(QTableWidget(), empty_text="리서치를 실행하면 성과표가 채워집니다")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        headers = ["ID", "Cost"] + [h for h, _ in SUMMARY_COLUMNS[1:]]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(72)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 50)
        install_table_keys(self.table)      # Ctrl+C copies the selected rows as TSV (docs/ui.md 8.6)
        self.result_tabs.addTab(self.table, "전체 지표")

        self.monthly_table = setup_table(QTableWidget(0, len(MONTH_HEADERS)),
                                         empty_text="리서치를 실행하면 선택한 변형의 월별 수익률(%)이 나옵니다")
        self.monthly_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.monthly_table.setHorizontalHeaderLabels(MONTH_HEADERS)
        self.monthly_table.horizontalHeader().setDefaultSectionSize(58)
        self.monthly_table.setColumnWidth(len(MONTH_HEADERS) - 1, 80)
        install_table_keys(self.monthly_table)
        self.result_tabs.addTab(self.monthly_table, "월별 수익률")

        # The compact list, with a summary line and the full list in a popup
        # (StrategyTradesDialog: "자세히 보기", a double-click or Enter on a row).
        trades_box = QWidget()
        trades_v = QVBoxLayout(trades_box)
        trades_v.setContentsMargins(0, 6, 0, 0)
        trades_v.setSpacing(4)
        trades_bar = QHBoxLayout()
        trades_bar.setContentsMargins(6, 0, 6, 0)
        self.trades_summary_lbl = QLabel("")
        self.trades_summary_lbl.setObjectName("muted")
        trades_bar.addWidget(self.trades_summary_lbl, 1)
        self.trades_detail_btn = make_button("자세히 보기", on_click=self.open_trades_dialog,
                                             tooltip="선택한 변형의 매매 내역을 수량·금액·손익·비용까지 큰 표로 봅니다")
        self.trades_detail_btn.setEnabled(False)
        trades_bar.addWidget(self.trades_detail_btn)
        trades_v.addLayout(trades_bar)
        self.trades_table = setup_table(QTableWidget(0, len(TRADE_HEADERS)), empty_text=_TRADES_EMPTY)
        self.trades_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.trades_table.setHorizontalHeaderLabels(TRADE_HEADERS)
        for c, w in enumerate((92, 92, 140, 70, 84, 84, 70, 50)):
            self.trades_table.setColumnWidth(c, w)
        self.trades_table.horizontalHeader().setStretchLastSection(True)
        self.trades_table.cellDoubleClicked.connect(self._on_trade_activated)
        install_table_keys(self.trades_table, on_activate=self._on_trade_activated)
        trades_v.addWidget(self.trades_table, 1)
        self.result_tabs.addTab(trades_box, "매매 내역")

        self.event_table = setup_table(QTableWidget(), empty_text="실행하면 6-1 이벤트 스터디가 여기에 나옵니다")
        self.event_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        ev_headers = ["Breakout group", "n"] + [f"{s} {h}d" for h in HORIZONS for s in ("mean", "hit", "vs KOSPI")]
        self.event_table.setColumnCount(len(ev_headers))
        self.event_table.setHorizontalHeaderLabels(ev_headers)
        self.event_table.setColumnWidth(0, 300)
        install_table_keys(self.event_table)
        self.result_tabs.addTab(self.event_table, "이벤트 스터디")

        # The shown result's caveats (benchmark substitutions, flow coverage,
        # skipped variants), readable here instead of only in the saved report
        # (review_codex.md S3).
        self.warn_lbl = QLabel("경고 없음.")
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
        self.result_tabs.addTab(scroll, "경고 (0)")
        return self.result_tabs

    def _draw_empty_chart(self):
        for ax in (self.ax, self.ax_dd):
            ax.clear()
            ax.set_axis_off()
        # Empty state (docs/ui.md 8.2): a hint instead of a bare 0-1 axis.
        self.ax.text(0.5, 0.5, "리서치를 실행하면 선택한 변형의 NAV와 낙폭을 BM1과 함께 그립니다",
                     ha="center", va="center", transform=self.ax.transAxes, color=TEXT_FAINT,
                     fontsize=FONT_SMALL)

    # ------------------------------------------------------- settings chips
    def _chip_texts(self) -> list[str]:
        start = self.start_edit.date().toPyDate()
        end = self.end_edit.date().toPyDate()
        mults = _COST_CHOICES[self.cost_combo.currentIndex()][1]
        n = len(self._selected_variants())
        variants = f"변형 {len(VARIANTS)}개 전체" if n == len(VARIANTS) else f"변형 {len(VARIANTS)}개 중 {n}개"
        chips = [
            f"{start:%Y-%m-%d} → {end:%Y-%m-%d}",
            f"{'+'.join(self.selected_markets())} 상위 {self.universe_spin.value()}",
            f"유동성 ≥ {self.liquidity_spin.value()}억",
            f"최대 {self.positions_spin.value()}종목",
            f"점검: {self.weekday_combo.currentText()}",
            "비용 " + "/".join(f"{m:g}x" for m in mults),
            variants,
        ]
        if self.includes_holdout():
            chips.append(_HOLDOUT_CHIP)
        if self.flows_cb.isChecked():
            chips.append("투자자 수급")
        if not self.event_cb.isChecked():
            chips.append("이벤트 스터디 제외")
        return chips

    def _update_chips(self, *_):
        texts = self._chip_texts()
        while len(self.chips) < len(texts):
            chip = QLabel()
            chip.setObjectName("settingsChip")
            self._chips_layout.insertWidget(len(self.chips), chip)   # before the trailing stretch
            self.chips.append(chip)
        for i, chip in enumerate(self.chips):
            chip.setVisible(i < len(texts))
            if i < len(texts):
                chip.setText(texts[i])
                warn = texts[i] == _HOLDOUT_CHIP
                if bool(chip.property("warn")) != warn:
                    chip.setProperty("warn", warn)
                    chip.style().unpolish(chip)
                    chip.style().polish(chip)

    def _on_edit_toggled(self, checked: bool):
        self.form_box.setVisible(checked)
        self.chips_box.setVisible(not checked)

    # --------------------------------------------------------------- request
    def _selected_variants(self) -> tuple[str, ...]:
        """The variants a run will backtest, in VARIANTS order: the user's
        choice minus the flow-dependent ones while Investor flows is off."""
        flows_on = self.flows_cb.isChecked()
        return tuple(vid for vid, v in VARIANTS.items()
                     if vid in self._chosen_variants and (flows_on or not v.needs_flows))

    def set_variant_choice(self, ids) -> None:
        """Apply a choice from the variant picker. Flow-dependent variants the
        picker could not show as ticked (flows off) keep their earlier state."""
        chosen = {vid for vid in ids if vid in VARIANTS}
        if not self.flows_cb.isChecked():
            chosen |= {vid for vid in self._chosen_variants if VARIANTS[vid].needs_flows}
        self._chosen_variants = chosen
        self._update_variants_label()

    def _update_variants_label(self, *_):
        selected = self._selected_variants()
        if len(selected) == len(VARIANTS):
            text = f"변형 {len(VARIANTS)}개 전체"
        elif not selected:
            text = "변형 없음 (이벤트 스터디만)"
        else:
            text = f"{', '.join(selected)}  ({len(VARIANTS)}개 중 {len(selected)}개)"
        waiting = [vid for vid, v in VARIANTS.items() if v.needs_flows and vid in self._chosen_variants
                   and not self.flows_cb.isChecked()]
        if waiting:
            text += f" · {', '.join(waiting)}은(는) 투자자 수급 필요"
        self.variants_lbl.setText(text)
        self.variants_lbl.setToolTip(text)
        self._update_chips()

    def _on_variants_clicked(self):
        dlg = VariantPickerDialog(self._selected_variants(), self.flows_cb.isChecked(),
                                  params=self._form_params(), parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.set_variant_choice(dlg.selected_ids())

    def _form_params(self) -> StrategyParams:
        return StrategyParams(
            universe_size=int(self.universe_spin.value()),
            min_avg_trading_value=float(self.liquidity_spin.value()) * 1e8,
            max_positions=int(self.positions_spin.value()),
            check_weekday=int(self.weekday_combo.currentIndex()),
        )

    def build_request(self) -> ResearchRequest:
        start = self.start_edit.date().toPyDate()
        end = self.end_edit.date().toPyDate()
        params = self._form_params()
        mults = _COST_CHOICES[self.cost_combo.currentIndex()][1]
        return ResearchRequest(
            start=start, end=end, params=params, variant_ids=self._selected_variants(),
            cost_multipliers=tuple(mults), include_flows=self.flows_cb.isChecked(),
            run_backtests=True, run_event_study=self.event_cb.isChecked(),
            allow_holdout=end > DEV_END, markets=self.selected_markets(),
        )

    def _on_run_clicked(self):
        if self.start_edit.date() >= self.end_edit.date():
            QMessageBox.warning(self, "Strategy", "시작일이 종료일보다 앞이어야 합니다.")
            return
        if not self._selected_variants() and not self.event_cb.isChecked():
            QMessageBox.warning(self, "Strategy", "변형을 하나 이상 고르거나 이벤트 스터디를 켜세요.")
            return
        req = self.build_request()
        thread = TrendFollowingResearchThread(req)
        thread.progress.connect(self._on_progress)
        thread.finished.connect(self._on_research_finished)
        self._track_thread(thread, "_research_thread")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Starting...")
        thread.start()

    def _on_stop_clicked(self):
        """Ask the running research thread to stop at its next check
        (review_agy.md 3.4, 2026-09-29); _on_research_finished re-enables Run."""
        thread = self._research_thread
        if thread is not None and thread.isRunning():
            thread.cancel()
            self.stop_btn.setEnabled(False)
            self.status_lbl.setText("Stopping after the current step...")

    def _on_progress(self, text: str):
        self.status_lbl.setText(text)

    def _on_research_finished(self, result, error: str):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if result is None:
            self.status_lbl.setText(error if error == CANCELLED_MESSAGE else f"Run failed: {error}")
            return
        self._result = result
        self.save_btn.setEnabled(True)
        self.populate(result)
        self._show_warnings(result)
        n_runs = len(result.runs)
        skipped = f" · skipped {', '.join(result.skipped)}" if result.skipped else ""
        self.status_lbl.setText(
            f"Done {result.created_at:%H:%M:%S} · {n_runs} runs · {result.dataset_info.get('n_tickers', 0)} names · "
            f"{result.dataset_info.get('eval_start', '')} → {result.dataset_info.get('eval_end', '')}{skipped}")

    # -------------------------------------------------------------- warnings
    def _show_warnings(self, result: ResearchResult):
        """Fill the Warnings tab from `result` (not from the current form)."""
        lines = [f"{escape(vid)} not run: {escape(why)}" for vid, why in result.skipped.items()]
        lines += [escape(w) for w in result.warnings]
        if result.request.allow_holdout:
            lines.insert(0, escape(_HOLDOUT_NOTE))
        self.warn_lbl.setText("<br>".join(f"• {line}" for line in lines) if lines else "경고 없음.")
        self.result_tabs.setTabText(TAB_WARNINGS, f"경고 ({len(lines)})")
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
        self._fill_events(result)
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
            fill_trades(self.trades_table, None)
            self._chart_series = []
            self._draw_empty_chart()
            self._update_readout(None)
            self.canvas.draw_idle()
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
        self.variant_seg.set_keys([r.id for r in rows], tooltips={r.id: r.label for r in rows})
        self._syncing = True
        try:
            fill_comparison(self.compare_table, rows)
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
        """A segment click: an unselected run is added and focused, a selected
        one that is not the focus becomes the focus, and the focus is removed
        (never the last selected run)."""
        if on:
            self.select_variant(run_id)
            return
        if run_id != self._sel_id:
            self.select_variant(run_id)
            self._sync_selection_widgets()      # the click unchecked it; it stays selected
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
        self._redraw_chart()

    def _sync_selection_widgets(self):
        self.variant_seg.set_checked(self._sel_ids)
        self.variant_seg.set_focus(self._sel_id)
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

    def _show_focus(self, run):
        self.kpi_row.show_run(run)
        self.target_card.show_run(run)
        fill_monthly(self.monthly_table, run)
        self.trades_table._empty_hint.text = _TRADES_EMPTY if run.kind == "strategy" else _TRADES_EMPTY_BM
        fill_trades(self.trades_table, run, self._result.dataset_info.get("names"))
        trades = sorted_trades(run)
        self.trades_summary_lbl.setText(trade_summary(trades) if run.kind == "strategy" else "")
        self.trades_detail_btn.setEnabled(bool(trades))

    # ------------------------------------------------------- trade list popup
    def open_trades_dialog(self, *_, row: int | None = None):
        """The selected run's trades in StrategyTradesDialog; `row` (a row of the
        compact list) is pre-selected there. Modeless, one per run shown."""
        run = self.selected_run()
        trades = sorted_trades(run)
        if not trades:
            return None
        pick = trades[row] if row is not None and 0 <= row < len(trades) else None
        info = self._result.dataset_info
        dlg = StrategyTradesDialog(
            f"{run.id} · 비용 {run.cost_mult:g}x · {run.label}", trades, info.get("names"), info.get("ticker_markets"),
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
                        item.setToolTip(r.label)
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

        ax.set_title(f"NAV rebased to 100 · {self._mult:g}x costs", fontsize=FONT_SMALL, loc="left", color=TEXT)
        ax.legend(fontsize=FONT_SMALL - 1, frameon=False, loc="upper left",
                  ncol=max(1, min(4, (len(chosen) + 3) // 4)))
        ax_dd.axhline(-TARGET_MDD * 100, color=SERIES_TARGET, linestyle="--", linewidth=1.0)
        ax_dd.set_ylabel("Drawdown %", fontsize=FONT_SMALL - 1, color=TEXT_MUTED)

        for a in (ax, ax_dd):
            a.grid(True, color=LINE, linewidth=0.6)
            a.tick_params(labelsize=FONT_SMALL - 1, colors=TEXT_MUTED)
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

    def _fill_events(self, result: ResearchResult):
        rows = result.event_study or []
        self.event_table.setUpdatesEnabled(False)
        try:
            self.event_table.setRowCount(len(rows))
            for i, e in enumerate(rows):
                values = [e.get("group", ""), _fmt_event(e.get("n", 0), pct=False)]
                for h in HORIZONS:
                    values += [_fmt_event(e.get(f"mean_{h}")), _fmt_event(e.get(f"hit_{h}"), digits=0),
                               _fmt_event(e.get(f"excess_{h}"))]
                for c, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                                          if c >= 1 else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self.event_table.setItem(i, c, item)
        finally:
            self.event_table.setUpdatesEnabled(True)

    # ---------------------------------------------------------------- report
    def _on_save_clicked(self):
        if self._result is None:
            return
        os.makedirs(REPORTS_DIR, exist_ok=True)
        default = os.path.join(REPORTS_DIR, f"trend_following_{SPEC_VERSION}_{datetime.now():%Y%m%d_%H%M}.md")
        path, _ = QFileDialog.getSaveFileName(self, "Save backtest report", default, "Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                header = [f"- **{_HOLDOUT_NOTE}**"] if self._result.request.allow_holdout else None
                f.write(self._result.to_markdown(header))
            self.status_lbl.setText(f"Report saved: {path}")
        except Exception as e:
            logger.warning("Failed to save the Trend Following report", exc_info=True)
            self.status_lbl.setText(f"Save failed: {e}")

    def default_window(self) -> tuple[date, date]:
        return self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate()
