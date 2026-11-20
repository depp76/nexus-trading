"""ui/filter_popups.py — the column-filter popups: FilterPopup (Excel-style
checkbox list) and SignalScanPopup (the Trading Universe's 신호 포착 panel:
the added signals as check boxes, docs/ui.md 8.9; the signals themselves are
added in ui/dialogs/signal_conditions.py).

Moved out of ui/widgets.py (2026-11-15, code review #5); ui.widgets still
re-exports both.
"""
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QCheckBox, QScrollArea,
    QWidget, QPushButton, QLabel, QToolButton,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from i18n import tr
from ui.common import create_font, BTN_H, FONT_BODY, FONT_CAPTION, FONT_FAMILY_CSS, make_button
from ui.universe_views import RANGE_FIELDS, describe_ranges
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


# The 신호 포착 panel's "추가" button: 80% of a normal button's text and height.
ADD_BTN_SCALE = 0.8


# ---------------------------------------------------------------------------
# SignalScanPopup  — the Trading Universe's 신호 포착 panel (docs/ui.md 8.9)
# ---------------------------------------------------------------------------
class SignalScanPopup(QFrame):
    """The added signals (range conditions, one per column) as check boxes,
    in RANGE_FIELDS order, each with the rows it alone would show and a
    remove button (user direction 2026-11-19). Ticking, unticking or
    removing (the row's x) applies at once and the panel stays open. Only ticked signals
    filter (AND); an unticked one keeps its bounds. "추가" (next to the
    title) asks the tab
    to open SignalConditionsDialog.

    `changed` carries (ticked, unticked), both {field_key: [lo, hi]}."""
    changed = pyqtSignal(dict, dict)
    add_requested = pyqtSignal()

    def __init__(self, on: dict, off: dict | None = None, counts: dict | None = None, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("DashboardCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._on, self._off = dict(on), dict(off or {})
        counts = counts or {}
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        # Header: the title, and "추가" at the panel's right edge, 80% of a
        # normal button's text and size (user direction 2026-11-19).
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(tr("신호 포착", "Signal Scan"))
        title.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        self.add_btn = make_button(tr("추가", "Add"), "primary", self._on_add,
                                   tooltip=tr("신호 추가: 범위 조건 입력 창을 엽니다",
                                              "Add a signal: opens the range-condition form"))
        self.add_btn.setFixedHeight(round(BTN_H * ADD_BTN_SCALE))
        self.add_btn.setStyleSheet(f"{FONT_FAMILY_CSS} font-size: {FONT_BODY * ADD_BTN_SCALE:g}pt; "
                                   "padding: 4px 9px; min-height: 12px;")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.add_btn)
        v.addLayout(head)

        self._checks = {}
        self._rows = {}
        for f in RANGE_FIELDS:
            bounds = self._on.get(f.key) or self._off.get(f.key)
            if not bounds:
                continue
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            text = describe_ranges({f.key: bounds})
            if f.key in counts:
                text += f"  ({counts[f.key]})"
            cb = QCheckBox(text)
            cb.setChecked(f.key in self._on)
            cb.clicked.connect(lambda checked, k=f.key: self._on_toggled(k, checked))
            rm = QToolButton()
            rm.setText("×")
            rm.setAutoRaise(True)
            rm.setToolTip(tr("이 신호 지우기", "Remove this signal"))
            rm.clicked.connect(lambda _c=False, k=f.key: self._on_removed(k))
            h.addWidget(cb)
            h.addStretch()
            h.addWidget(rm)
            v.addWidget(row)
            self._checks[f.key] = cb
            self._rows[f.key] = row

        self._empty = QLabel(tr("추가한 신호가 없습니다. '추가'로 조건을 넣으세요.",
                                "No signals yet. Use 'Add' to add a condition."))
        self._empty.setObjectName("muted")
        self._empty.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        # Only ever *hide* it here: an explicit show on a child before the
        # Qt.Popup itself is shown makes Windows close the popup right after
        # it opens (found 2026-11-19: the panel never appeared with no signals).
        if self._checks:
            self._empty.hide()
        v.addWidget(self._empty)


    def _emit(self):
        self.changed.emit(dict(self._on), dict(self._off))

    def _on_toggled(self, key: str, checked: bool):
        bounds = self._on.pop(key, None) or self._off.pop(key)
        (self._on if checked else self._off)[key] = bounds
        self._emit()

    def _on_removed(self, key: str):
        self._on.pop(key, None)
        self._off.pop(key, None)
        self._checks.pop(key)
        self._rows.pop(key).hide()
        self._empty.setVisible(not self._checks)
        self.adjustSize()
        self._emit()

    def _on_add(self):
        self.close()
        self.add_requested.emit()
