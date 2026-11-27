"""ui/dialogs/signal_conditions.py — SignalConditionsDialog: the Trading
Universe 신호 포착 panel's "신호 추가" form (docs/ui.md 8.9, user direction
2026-11-19). The min/max grid of every range-filterable column that used to
be the panel itself is now this pop-up: each filled row is one signal, which
then shows as a check box in the 신호 포착 panel. Opened with the signals
already added, so it also edits and removes them (empty a row to remove it).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QGridLayout, QLabel, QLineEdit, QVBoxLayout

from i18n import tr
from ui.common import _set_field_error, button_row, make_button
from ui.universe_views import RANGE_FIELDS


def parse_bound(edit: QLineEdit, mark: bool = True):
    """(value or None, valid). A blank field is (None, True); `mark`
    outlines an unparsable one in red (and clears the outline otherwise)."""
    text = edit.text().replace(",", "").strip()
    if not text:
        if mark:
            _set_field_error(edit, "")
        return None, True
    try:
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError
    except ValueError:
        if mark:
            _set_field_error(edit, tr("숫자를 입력하세요", "Enter a number"))
        return None, False
    if mark:
        _set_field_error(edit, "")
    return value, True


class SignalConditionsDialog(QDialog):
    """`conditions` -- {field_key: [lo | None, hi | None]} -- fills the grid.
    After `exec()` is accepted, `conditions()` is the filled rows (lo <= hi)."""

    def __init__(self, conditions: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("신호 추가", "Add Signal"))
        conditions = conditions or {}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)
        hint = QLabel(tr("값을 넣은 조건이 신호로 추가됩니다. 빈 칸은 제한 없음, 두 칸을 모두 비우면 삭제.",
                         "Each condition with a value becomes a signal. Blank = no limit; "
                         "empty both fields to remove it."))
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self._edits = {}
        for r, f in enumerate(RANGE_FIELDS):
            lo, hi = (list(conditions.get(f.key) or []) + [None, None])[:2]
            unit = QLabel(f.unit)
            unit.setObjectName("faint")
            lo_e, hi_e = QLineEdit(), QLineEdit()
            for e, val, ph in ((lo_e, lo, tr("최소", "min")), (hi_e, hi, tr("최대", "max"))):
                e.setPlaceholderText(ph)
                e.setFixedWidth(80)
                e.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if val is not None:
                    e.setText(f"{val:g}")
                e.returnPressed.connect(self._on_ok)
            grid.addWidget(QLabel(f.label), r, 0)
            grid.addWidget(lo_e, r, 1)
            grid.addWidget(QLabel("–"), r, 2)
            grid.addWidget(hi_e, r, 3)
            grid.addWidget(unit, r, 4)
            self._edits[f.key] = (lo_e, hi_e)
        grid.setColumnStretch(5, 1)
        root.addLayout(grid)

        self.ok_btn = make_button(tr("저장", "Save"), "primary", self._on_ok)
        cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        root.addLayout(button_row(cancel_btn, self.ok_btn))

    def values(self, mark: bool = True) -> tuple[dict, bool]:
        """(filled rows, every field valid)."""
        out, ok = {}, True
        for key, (lo_e, hi_e) in self._edits.items():
            lo, ok_lo = parse_bound(lo_e, mark)
            hi, ok_hi = parse_bound(hi_e, mark)
            ok = ok and ok_lo and ok_hi
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            if lo is not None or hi is not None:
                out[key] = [lo, hi]
        return out, ok

    def conditions(self) -> dict:
        return self.values(mark=False)[0]

    def _on_ok(self):
        if self.values()[1]:
            self.accept()
