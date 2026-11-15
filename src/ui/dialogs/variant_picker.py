"""ui/dialogs/variant_picker.py — VariantPickerDialog: choose which 6-2 matrix
rows (A0-A5, B, C1-C3) a Trend Following research run backtests, with each
row's rules shown next to the list (strategy.trend_following.describe_variant).

Opened from the Strategy tab's "전략 선택..." button. Every row can be ticked;
the tab loads investor flows when a ticked row needs them (A3-A5).

The list labels and descriptions are Korean (user direction, 2026-11-13);
the buttons stay English like the rest of the app's chrome.
"""
from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QSplitter, QTextBrowser, QVBoxLayout,
)

from strategy.trend_following import VARIANTS, StrategyParams, describe_variant
from ui.common import make_button, button_row, fit_to_screen
from ui.theme import TEXT, TEXT_MUTED, TEXT_SUB

# List groups, in VARIANTS order within each (spec 6-2).
_GROUPS = (
    ("A 계열 — Donchian 돌파", lambda v: v.mode == "breakout"),
    ("B — 52주 고가 보유", lambda v: v.mode == "high52"),
    ("C 계열 — 추세 + 눌림 점수", lambda v: v.mode == "score"),
)
_VID_ROLE = Qt.ItemDataRole.UserRole


def description_html(vid: str, params: StrategyParams | None = None) -> str:
    d = describe_variant(vid, params)
    out = [f'<h3 style="color:{TEXT}; margin:0 0 4px 0;">{escape(d.id)} — {escape(d.title)}</h3>',
           f'<p style="color:{TEXT_SUB}; margin:0 0 8px 0;">{escape(d.summary)}</p>']
    for heading, lines in d.sections:
        out.append(f'<p style="color:{TEXT_MUTED}; margin:8px 0 2px 0;"><b>{escape(heading)}</b></p>')
        out.append('<ul style="margin:0 0 0 -16px;">'
                   + "".join(f'<li style="color:{TEXT};">{escape(line)}</li>' for line in lines) + "</ul>")
    return "".join(out)


class VariantPickerDialog(QDialog):
    """`selected`: the ids ticked on open. `params`: the numbers quoted in the
    descriptions (the tab's current form). After `exec()`, `selected_ids()` is the choice in
    VARIANTS order."""

    def __init__(self, selected, params: StrategyParams | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("전략 선택")
        fit_to_screen(self, 880, 540)
        self._params = params

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        # Bulk selection sits on the list it acts on; the bottom row is only
        # 취소 → 확인 (docs/ui.md 8.17).
        bulk = QHBoxLayout()
        bulk.setSpacing(6)
        bulk.addWidget(make_button("전체 선택", on_click=lambda: self._set_all(True)))
        bulk.addWidget(make_button("전체 해제", on_click=lambda: self._set_all(False)))
        bulk.addStretch()
        root.addLayout(bulk)
        root.setSpacing(8)

        self.list = QListWidget()
        self._items: dict[str, QListWidgetItem] = {}
        chosen = set(selected)
        for heading, belongs in _GROUPS:
            ids = [vid for vid, v in VARIANTS.items() if belongs(v)]
            if not ids:
                continue
            head = QListWidgetItem(heading)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            font = QFont(self.list.font())
            font.setBold(True)
            head.setFont(font)
            head.setForeground(QBrush(QColor(TEXT_SUB)))
            self.list.addItem(head)
            for vid in ids:
                self.list.addItem(self._make_item(vid, vid in chosen))

        # Wide enough for the longest row, so no label is cut off.
        self.list.setMinimumWidth(self.list.sizeHintForColumn(0) + 2 * self.list.frameWidth() + 24)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.list)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("muted")
        root.addWidget(self.count_lbl)

        self.ok_btn = make_button("확인", "primary", self.accept)
        root.addLayout(button_row(make_button("취소", on_click=self.reject), self.ok_btn))

        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemChanged.connect(self._update_count)
        first = next(iter(self._items.values()), None)
        if first is not None:
            self.list.setCurrentItem(first)
        self._update_count()

    def _make_item(self, vid: str, checked: bool) -> QListWidgetItem:
        d = describe_variant(vid, self._params)
        item = QListWidgetItem(f"{vid}  {d.title}")
        item.setData(_VID_ROLE, vid)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._items[vid] = item
        return item

    def _set_all(self, on: bool):
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for item in self._items.values():
            item.setCheckState(state)

    def _on_current_changed(self, item, _prev=None):
        vid = item.data(_VID_ROLE) if item is not None else None
        if vid:
            self.detail.setHtml(description_html(vid, self._params))

    def _update_count(self, *_):
        self.count_lbl.setText(f"{len(self._items)}개 중 {len(self.selected_ids())}개 선택")

    def select(self, vid: str):
        """Show `vid`'s description (as a click on its row would)."""
        self.list.setCurrentItem(self._items[vid])

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(vid for vid, item in self._items.items() if item.checkState() == Qt.CheckState.Checked)
