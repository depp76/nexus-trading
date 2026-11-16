"""ui/dialogs/trend_conditions.py — the Trend Following "매매전략..." dialog (was "조건...")
(reports/strategy_run_settings_ui.md 2): the rule list with its fixed
description pane on top (VariantPickerDialog), and under it the run
conditions: market-cap top N, liquidity floor, max positions, check weekday,
cost scenario and the target Sharpe the result is judged by.

`TrendConditions` is the value the Strategy tab keeps per strategy; the dialog
edits a copy, so 취소 leaves the tab's value as it was.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QDoubleSpinBox, QGridLayout, QLabel, QSpinBox, QWidget

from i18n import tr
from strategy.trend_following import COST_MULTIPLIERS, VARIANTS, StrategyParams
from ui.common import FONT_CAPTION, create_font, fit_to_screen
from ui.dialogs.variant_picker import VariantPickerDialog
from ui.strategy_widgets import DEFAULT_TARGET_SHARPE, TARGET_MDD, TARGET_SHARPE_RANGE

WEEKDAYS = [tr("월요일", "Monday"), tr("화요일", "Tuesday"), tr("수요일", "Wednesday"),
            tr("목요일", "Thursday"), tr("금요일", "Friday")]
COST_CHOICES = [(tr("0·1·2배 비교", "0x / 1x / 2x"), COST_MULTIPLIERS), (tr("1배만", "1x only"), (1.0,))]
_SPIN_W = 110


@dataclass(frozen=True)
class TrendConditions:
    """Everything a Trend Following run takes besides markets and the window."""
    variant_ids: tuple[str, ...] = ()
    universe_size: int = StrategyParams.universe_size
    min_liquidity_eok: int = int(StrategyParams.min_avg_trading_value / 1e8)
    max_positions: int = StrategyParams.max_positions
    check_weekday: int = StrategyParams.check_weekday
    cost_choice: int = 0                  # index into COST_CHOICES
    target_sharpe: float = DEFAULT_TARGET_SHARPE

    def __post_init__(self):
        # VARIANTS order, unknown ids dropped: the request and the summary
        # never depend on the order the boxes were ticked.
        ids = set(self.variant_ids)
        object.__setattr__(self, "variant_ids", tuple(v for v in VARIANTS if v in ids))

    def params(self) -> StrategyParams:
        return StrategyParams(universe_size=int(self.universe_size),
                              min_avg_trading_value=float(self.min_liquidity_eok) * 1e8,
                              max_positions=int(self.max_positions),
                              check_weekday=int(self.check_weekday))

    def cost_multipliers(self) -> tuple[float, ...]:
        return tuple(COST_CHOICES[self.cost_choice][1])

    def cost_label(self) -> str:
        return COST_CHOICES[self.cost_choice][0]


def _make_spin(lo, hi, step, value, suffix, tip=""):
    sb = QSpinBox()
    sb.setRange(lo, hi)
    sb.setSingleStep(step)
    sb.setValue(int(value))
    sb.setSuffix(suffix)
    sb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    sb.setMinimumWidth(_SPIN_W)
    sb.setToolTip(tip)
    return sb


class TrendConditionsDialog(VariantPickerDialog):
    """The rule picker plus the run conditions; 취소 -> 적용 at the bottom.
    After `exec()`, `conditions()` is the edited value."""

    def __init__(self, conditions: TrendConditions, parent=None):
        super().__init__(conditions.variant_ids, params=conditions.params(), parent=parent)
        self.setWindowTitle(tr("매매전략 — 추세추종", "Trading Strategy — Trend Following"))
        fit_to_screen(self, 900, 700)
        self.ok_btn.setText(tr("적용", "Apply"))

        form = QWidget()
        grid = QGridLayout(form)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)

        self._build_inputs(conditions)
        self._lay_out_fields(grid)

        # Above the 취소/적용 row (the last item of the picker's layout).
        root = self.layout()
        root.insertWidget(root.count() - 1, form)

        # The descriptions quote these numbers: keep them current.
        for sig in (self.universe_spin.valueChanged, self.liquidity_spin.valueChanged,
                    self.positions_spin.valueChanged, self.weekday_combo.currentIndexChanged):
            sig.connect(self._refresh_description)

    def _build_inputs(self, conditions: TrendConditions) -> None:
        """The six condition inputs, set from `conditions`."""
        spin = _make_spin
        self.universe_spin = spin(20, 300, 10, conditions.universe_size, tr(" 개", " names"),
                                  tr("선택한 시장 전체에서 시가총액(원화 환산) 상위 N개 종목",
                                     "Top N names by market cap (in KRW) across the selected markets"))
        self.liquidity_spin = spin(0, 5000, 10, conditions.min_liquidity_eok, tr(" 억 원", " ×100M KRW"),
                           tr("20일 평균 거래대금 하한", "Floor on the 20-day average trading value"))
        self.positions_spin = spin(3, 12, 1, conditions.max_positions, tr(" 종목", " names"))
        self.weekday_combo = QComboBox()
        self.weekday_combo.addItems(WEEKDAYS)
        self.weekday_combo.setCurrentIndex(conditions.check_weekday)
        self.weekday_combo.setToolTip(tr("주간 신호 점검 요일", "Weekly signal check day"))
        self.cost_combo = QComboBox()
        self.cost_combo.addItems([label for label, _ in COST_CHOICES])
        self.cost_combo.setCurrentIndex(conditions.cost_choice)
        self.cost_combo.setToolTip(tr("trend_following.md 4-1 비용 민감도: 0·1·2배를 함께 돌리거나 1배만 돌립니다",
                                       "trend_following.md 4-1 cost sensitivity: run 0x, 1x and 2x together, "
                                       "or 1x only"))
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(*TARGET_SHARPE_RANGE)
        self.target_spin.setDecimals(1)
        self.target_spin.setSingleStep(0.1)
        self.target_spin.setValue(conditions.target_sharpe)
        self.target_spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.target_spin.setMinimumWidth(_SPIN_W)
        self.target_spin.setToolTip(tr("결과를 판정할 Sharpe 목표(기본 1.0). 실행 시점의 값이 그 결과에 적용됩니다. "
                                       f"MDD 목표는 {TARGET_MDD * 100:g}% 고정.",
                                       "The Sharpe target the result is judged by (default 1.0); the value at "
                                       f"Run applies to that result. The MDD target is fixed at {TARGET_MDD * 100:g}%."))

    def _lay_out_fields(self, grid: QGridLayout) -> None:
        """Two rows of three: 종목·보유 조건 / 운용·평가, the name above each
        input; the multi-market note under them."""
        fields = [(tr("시가총액 상위", "Market-cap top N"), self.universe_spin),
                  (tr("20일 평균 거래대금 하한", "Min 20-day avg. trading value"), self.liquidity_spin),
                  (tr("최대 보유 종목 수", "Max positions"), self.positions_spin),
                  (tr("주간 점검 요일", "Weekly check day"), self.weekday_combo),
                  (tr("거래비용", "Trading costs"), self.cost_combo),
                  (tr("평가 기준 · 목표 Sharpe", "Evaluation · target Sharpe"), self.target_spin)]
        self.field_names = []
        for k, (name, w) in enumerate(fields):
            r, c = divmod(k, 3)
            lbl = QLabel(name)
            lbl.setObjectName("muted")
            lbl.setFont(create_font(FONT_CAPTION))
            if r:
                lbl.setContentsMargins(0, 8, 0, 0)       # a gap between the two rows
            self.field_names.append(lbl)
            grid.addWidget(lbl, 2 * r, c)
            grid.addWidget(w, 2 * r + 1, c, Qt.AlignmentFlag.AlignLeft)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        self.market_note = QLabel(tr("여러 시장을 고르면 선택한 시장 전체를 원화 시가총액으로 합산한 순위에서 상위 N개를 고릅니다.",
                                   "With several markets, the top N come from one ranking of all selected "
                                   "markets by market cap in KRW."))
        self.market_note.setObjectName("muted")
        self.market_note.setWordWrap(True)
        grid.addWidget(self.market_note, 4, 0, 1, 3)

    def _refresh_description(self, *_):
        self._params = self._current().params()
        self._on_current_changed(self.list.currentItem())

    def _current(self) -> TrendConditions:
        return TrendConditions(
            variant_ids=self.selected_ids(),
            universe_size=self.universe_spin.value(),
            min_liquidity_eok=self.liquidity_spin.value(),
            max_positions=self.positions_spin.value(),
            check_weekday=self.weekday_combo.currentIndex(),
            cost_choice=self.cost_combo.currentIndex(),
            target_sharpe=round(float(self.target_spin.value()), 1),
        )

    def conditions(self) -> TrendConditions:
        return self._current()
