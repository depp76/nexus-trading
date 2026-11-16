"""ui/dialogs/add_record.py — AddRecordDialog: the Total Assets "기록 추가"
button's form (week + total assets). It replaced the tab's own input row on
2026-11-16 (user direction; docs/ui.md 8.17), so the tab's toolbar is one row.
The dialog only collects the input; the tab saves the record.

Auto-fill (docs/ui.md 4.5) works as it did in the input row: the latest
Friday gets the live total from Trading History until the user types over it,
and the record is flagged manual when the saved value was typed.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from i18n import tr
from ui.common import button_row, make_button


def parse_amount(text: str) -> float | None:
    """The typed total ("1,234,567") as a finite float, else None."""
    try:
        value = float(text.replace(",", "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None   # float() takes "nan"/"inf" (review_codex.md #6)


class AddRecordDialog(QDialog):
    """`fridays` is [(label, iso_date)] oldest first; the latest is selected.
    `existing` maps iso_date -> saved total (a save over one replaces it).
    After `exec()` is accepted, `values()` is (iso_date, total, manual)."""

    def __init__(self, fridays: list[tuple[str, str]], live_total: float = 0.0,
                 existing: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("기록 추가", "Add Record"))
        self.setMinimumWidth(380)
        self._existing = existing or {}
        self._live_total = live_total
        self._manual = False   # the user typed over the auto-filled value

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        form = QFormLayout()

        self.date_combo = QComboBox()
        for label, iso_date in fridays:
            self.date_combo.addItem(label, iso_date)
        self.date_combo.setCurrentIndex(self.date_combo.count() - 1)
        self.this_week_btn = make_button(tr("이번 주", "This Week"), on_click=self._select_latest_week)
        date_row = QHBoxLayout()
        date_row.setSpacing(6)
        date_row.addWidget(self.date_combo, 1)
        date_row.addWidget(self.this_week_btn)
        form.addRow(tr("날짜", "Date"), date_row)

        self.asset_edit = QLineEdit()
        self.asset_edit.setPlaceholderText(tr("총자산 금액", "Total assets amount"))
        self.asset_edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.asset_edit.textEdited.connect(self._on_typed)
        self.asset_edit.textChanged.connect(self._sync)
        self.asset_edit.returnPressed.connect(self._on_save)
        form.addRow(tr("총자산", "Total Assets"), self.asset_edit)
        root.addLayout(form)

        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self.save_btn = make_button(tr("기록 추가", "Add Record"), "primary", self._on_save)
        cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        root.addLayout(button_row(cancel_btn, self.save_btn))

        self.date_combo.currentIndexChanged.connect(self._on_date_changed)
        self._autofill()
        self._sync()
        self.asset_edit.setFocus()

    # ---state ---
    def _is_latest(self) -> bool:
        return self.date_combo.count() > 0 and self.date_combo.currentIndex() == self.date_combo.count() - 1

    def _select_latest_week(self):
        self.date_combo.setCurrentIndex(self.date_combo.count() - 1)

    def _on_date_changed(self, _index: int):
        self._manual = False
        self.asset_edit.clear()
        self._autofill()
        self._sync()

    def _on_typed(self, text: str):
        self._manual = True
        raw = text.replace(",", "").strip()
        if raw.isdigit():
            formatted = f"{int(raw):,}"
            if formatted != text:
                pos = self.asset_edit.cursorPosition()
                self.asset_edit.setText(formatted)
                self.asset_edit.setCursorPosition(max(0, pos + len(formatted) - len(text)))

    def _autofill(self):
        """The live total for the latest Friday only, while not typed over."""
        if self._is_latest() and not self._manual and self._live_total > 0:
            self.asset_edit.setText(f"{self._live_total:,.0f}")

    def set_live_total(self, value: float):
        """The tab passes on a live update that arrives while the dialog is open."""
        self._live_total = value
        self._autofill()
        self._sync()

    def _sync(self, _text: str = ""):
        self.save_btn.setEnabled(parse_amount(self.asset_edit.text()) is not None)
        date = self.date_combo.currentData()
        if date in self._existing:
            self.hint.setText(tr(f"이 주에는 이미 {self._existing[date]:,.0f} 기록이 있습니다. 저장하면 바뀝니다.",
                                 f"This week already has a record of {self._existing[date]:,.0f}; saving replaces it."))
        elif self._is_latest() and not self._manual and self._live_total > 0:
            self.hint.setText(tr("Trading History의 현재 총자산(실시간)으로 채웠습니다. 고쳐 써도 됩니다.",
                                 "Filled in from Trading History's live total; you can type over it."))
        else:
            self.hint.setText(tr("금요일 기준 주간 총자산을 입력하세요.", "Enter the week's total assets (as of Friday)."))

    def _on_save(self):
        if self.save_btn.isEnabled():
            self.accept()

    def values(self) -> tuple[str, float, bool]:
        return str(self.date_combo.currentData() or ""), parse_amount(self.asset_edit.text()), self._manual
