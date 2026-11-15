"""ui/dialogs/trend_conditions.py — the Trend Following "조건..." dialog
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

from strategy.trend_following import COST_MULTIPLIERS, VARIANTS, StrategyParams
from ui.common import FONT_CAPTION, create_font, fit_to_screen
from ui.dialogs.variant_picker import VariantPickerDialog
from ui.strategy_widgets import DEFAULT_TARGET_SHARPE, TARGET_MDD, TARGET_SHARPE_RANGE

WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일"]
COST_CHOICES = [("0·1·2배 비교", COST_MULTIPLIERS), ("1배만", (1.0,))]
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


class TrendConditionsDialog(VariantPickerDialog):
    """The rule picker plus the run conditions; 취소 -> 적용 at the bottom.
    After `exec()`, `conditions()` is the edited value."""

    def __init__(self, conditions: TrendConditions, parent=None):
        super().__init__(conditions.variant_ids, params=conditions.params(), parent=parent)
        self.setWindowTitle("조건 — 추세추종")
        fit_to_screen(self, 900, 700)
        self.ok_btn.setText("적용")

        form = QWidget()
        grid = QGridLayout(form)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)

        def spin(lo, hi, step, value, suffix, tip=""):
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setSingleStep(step)
            sb.setValue(int(value))
            sb.setSuffix(suffix)
            sb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            sb.setMinimumWidth(_SPIN_W)
            sb.setToolTip(tip)
            return sb

        self.universe_spin = spin(20, 300, 10, conditions.universe_size, " 개",
                                  "선택한 시장 전체에서 시가총액(원화 환산) 상위 N개 종목")
        self.liquidity_spin = spin(0, 5000, 10, conditions.min_liquidity_eok, " 억 원", "20일 평균 거래대금 하한")
        self.positions_spin = spin(3, 12, 1, conditions.max_positions, " 종목")
        self.weekday_combo = QComboBox()
        self.weekday_combo.addItems(WEEKDAYS)
        self.weekday_combo.setCurrentIndex(conditions.check_weekday)
        self.weekday_combo.setToolTip("주간 신호 점검 요일")
        self.cost_combo = QComboBox()
        self.cost_combo.addItems([label for label, _ in COST_CHOICES])
        self.cost_combo.setCurrentIndex(conditions.cost_choice)
        self.cost_combo.setToolTip("trend_following.md 4-1 비용 민감도: 0·1·2배를 함께 돌리거나 1배만 돌립니다")
        self.target_spin = QDoubleSpinBox()
        self.target_spin.setRange(*TARGET_SHARPE_RANGE)
        self.target_spin.setDecimals(1)
        self.target_spin.setSingleStep(0.1)
        self.target_spin.setValue(conditions.target_sharpe)
        self.target_spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.target_spin.setMinimumWidth(_SPIN_W)
        self.target_spin.setToolTip("결과를 판정할 Sharpe 목표(기본 1.0). 실행 시점의 값이 그 결과에 적용됩니다. "
                                    f"MDD 목표는 {TARGET_MDD * 100:g}% 고정.")

        # Two rows of three: 종목·보유 조건 / 운용·평가, the name above each input.
        fields = [("시가총액 상위", self.universe_spin), ("20일 평균 거래대금 하한", self.liquidity_spin),
                  ("최대 보유 종목 수", self.positions_spin), ("주간 점검 요일", self.weekday_combo),
                  ("거래비용", self.cost_combo), ("평가 기준 · 목표 Sharpe", self.target_spin)]
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
        self.market_note = QLabel("여러 시장을 고르면 선택한 시장 전체를 원화 시가총액으로 합산한 순위에서 상위 N개를 고릅니다.")
        self.market_note.setObjectName("muted")
        self.market_note.setWordWrap(True)
        grid.addWidget(self.market_note, 4, 0, 1, 3)

        # Above the 취소/적용 row (the last item of the picker's layout).
        root = self.layout()
        root.insertWidget(root.count() - 1, form)

        # The descriptions quote these numbers: keep them current.
        for sig in (self.universe_spin.valueChanged, self.liquidity_spin.valueChanged,
                    self.positions_spin.valueChanged, self.weekday_combo.currentIndexChanged):
            sig.connect(self._refresh_description)

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
