"""ui/filter_popups.py — the column-filter popups: FilterPopup (Excel-style
checkbox list) and RangeFilterPopup (the Trading Universe's numeric range
filters, docs/ui.md 8.9).

Moved out of ui/widgets.py (2026-11-15, code review #5); ui.widgets still
re-exports both.
"""
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QCheckBox, QScrollArea,
    QWidget, QPushButton, QGridLayout, QLabel,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from i18n import tr
from ui.common import create_font, FONT_BODY, FONT_CAPTION, make_button, _set_field_error
from ui.universe_views import RANGE_FIELDS
from ui.theme import LINE_GROUP, SURFACE


# ---------------------------------------------------------------------------
# FilterPopup  — Excel-style checkbox popup
# ---------------------------------------------------------------------------
class FilterPopup(QFrame):
    """Checkbox popup for Excel-style column filtering."""
    filter_changed = pyqtSignal(int, object)  # col, frozenset | None

    def __init__(self, col, unique_vals, current_filter, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.col = col
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(f"QFrame {{ background: {SURFACE}; border: 1px solid {LINE_GROUP}; border-radius: 4px; }}")

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(8, 8, 8, 8)

        # Search inside popup
        self._search = QLineEdit()
        self._search.setFont(create_font(FONT_BODY, style_name="Semilight"))
        self._search.setPlaceholderText(tr("검색...", "Search..."))
        self._search.textChanged.connect(self._on_search)
        layout.addWidget(self._search)

        # Select All
        self._all_cb = QCheckBox(tr("(전체 선택)", "(Select All)"))
        self._all_cb.setFont(create_font(FONT_BODY, style_name="Semilight"))
        self._all_cb.setTristate(True)
        layout.addWidget(self._all_cb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # Scroll area for values
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(min(240, 28 * len(unique_vals) + 8))
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(2)
        inner_layout.setContentsMargins(2, 2, 2, 2)

        self._checkboxes = {}
        for val in sorted(unique_vals, key=lambda v: (v != "Index", v)):
            cb = QCheckBox(str(val))
            cb.setFont(create_font(FONT_BODY, style_name="Semilight"))
            cb.setChecked(current_filter is None or val in current_filter)
            inner_layout.addWidget(cb)
            self._checkboxes[val] = cb
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        # Buttons
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton(tr("확인", "OK"))
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self._apply)
        clear_btn = QPushButton(tr("지우기", "Clear"))
        clear_btn.clicked.connect(self._clear)
        btn_layout.addWidget(clear_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._updating = False
        self._sync_all()
        self._all_cb.stateChanged.connect(self._on_all_changed)
        for cb in self._checkboxes.values():
            cb.stateChanged.connect(self._on_value_changed)

    def _on_search(self, text):
        text = text.lower()
        for val, cb in self._checkboxes.items():
            cb.setVisible(not text or text in str(val).lower())

    def _sync_all(self):
        self._updating = True
        visible = [cb for cb in self._checkboxes.values() if cb.isVisible()]
        checked_count = sum(1 for cb in visible if cb.isChecked())
        if checked_count == len(visible):
            self._all_cb.setCheckState(Qt.CheckState.Checked)
        elif checked_count == 0:
            self._all_cb.setCheckState(Qt.CheckState.Unchecked)
        else:
            self._all_cb.setCheckState(Qt.CheckState.PartiallyChecked)
        self._updating = False

    def _on_all_changed(self, state):
        if self._updating:
            return
        checked = (state == Qt.CheckState.Checked.value)
        self._updating = True
        for cb in self._checkboxes.values():
            if cb.isVisible():
                cb.setChecked(checked)
        self._updating = False

    def _on_value_changed(self, _):
        if not self._updating:
            self._sync_all()

    def _clear(self):
        self.filter_changed.emit(self.col, None)
        self.close()

    def _apply(self):
        checked = frozenset(val for val, cb in self._checkboxes.items() if cb.isChecked())
        all_vals = frozenset(self._checkboxes.keys())
        self.filter_changed.emit(self.col, None if checked == all_vals else checked)
        self.close()


# ---------------------------------------------------------------------------
# RangeFilterPopup  — numeric range filters for Trading Universe (docs/ui.md 8.9)
# ---------------------------------------------------------------------------
class RangeFilterPopup(QFrame):
    """Min/max inputs for every range-filterable column (ui.universe_views.
    RANGE_FIELDS). Apply emits the ranges dict; Clear empties every field
    and applies. A blank field means "no bound"; an unparsable one is
    outlined in red and blocks Apply."""
    applied = pyqtSignal(dict)

    def __init__(self, ranges: dict, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("DashboardCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        title = QLabel(tr("범위 필터", "Range filters"))
        title.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        v.addWidget(title)
        hint = QLabel(tr("빈 칸은 제한 없음. 필터를 건 열에 값이 없는 종목은 숨깁니다.",
                      "Blank = no limit. Rows with no value in a filtered column are hidden."))
        hint.setObjectName("muted")
        hint.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        hint.setWordWrap(True)
        v.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        self._edits = {}
        for r, f in enumerate(RANGE_FIELDS):
            lo, hi = (list(ranges.get(f.key) or []) + [None, None])[:2]
            lbl = QLabel(f.label)
            unit = QLabel(f.unit)
            unit.setObjectName("faint")
            lo_e, hi_e = QLineEdit(), QLineEdit()
            for e, val, ph in ((lo_e, lo, tr("최소", "min")), (hi_e, hi, tr("최대", "max"))):
                e.setPlaceholderText(ph)
                e.setFixedWidth(80)
                e.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if val is not None:
                    e.setText(f"{val:g}")
                e.returnPressed.connect(self._apply)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(lo_e, r, 1)
            grid.addWidget(QLabel("\u2013"), r, 2)
            grid.addWidget(hi_e, r, 3)
            grid.addWidget(unit, r, 4)
            self._edits[f.key] = (lo_e, hi_e)
        grid.setColumnStretch(5, 1)   # spare width goes past the unit column, not between the inputs
        v.addLayout(grid)

        btns = QHBoxLayout()
        btns.addStretch()
        clear_btn = make_button(tr("지우기", "Clear"), on_click=self._clear)
        apply_btn = make_button(tr("적용", "Apply"), "primary", self._apply)
        btns.addWidget(clear_btn)
        btns.addWidget(apply_btn)
        v.addLayout(btns)

    @staticmethod
    def _parse(edit: QLineEdit):
        text = edit.text().replace(",", "").strip()
        if not text:
            _set_field_error(edit, "")
            return None, True
        try:
            value = float(text)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError
        except ValueError:
            _set_field_error(edit, tr("숫자를 입력하세요", "Enter a number"))
            return None, False
        _set_field_error(edit, "")
        return value, True

    def values(self):
        """(ranges dict, all fields valid)."""
        out, ok = {}, True
        for key, (lo_e, hi_e) in self._edits.items():
            lo, ok_lo = self._parse(lo_e)
            hi, ok_hi = self._parse(hi_e)
            ok = ok and ok_lo and ok_hi
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            if lo is not None or hi is not None:
                out[key] = [lo, hi]
        return out, ok

    def _apply(self):
        ranges, ok = self.values()
        if not ok:
            return
        self.applied.emit(ranges)
        self.close()

    def _clear(self):
        for lo_e, hi_e in self._edits.values():
            lo_e.clear()
            hi_e.clear()
        self._apply()
