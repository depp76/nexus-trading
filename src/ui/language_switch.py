"""ui/language_switch.py — the header's language flags (docs/ui.md 8.20).

Two checkable flag buttons, Taegukgi for Korean and the US flag for English;
the current language is the checked one. Clicking the other flag emits
`language_requested(lang)`; `MainWindow` confirms, saves the choice and
restarts (labels are built once per process, see `i18n`). If the switch is
declined the owner calls `reset()` to re-check the current flag.
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QToolButton, QButtonGroup

import i18n

_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
FLAG_ICONS = {"ko": os.path.join(_ICON_DIR, "flag_kr.svg"), "en": os.path.join(_ICON_DIR, "flag_us.svg")}
FLAG_SIZE = QSize(24, 16)


class LanguageSwitch(QWidget):
    language_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for lang in i18n.LANGUAGES:
            btn = QToolButton()
            btn.setObjectName("langFlag")
            btn.setIcon(QIcon(FLAG_ICONS[lang]))
            btn.setIconSize(FLAG_SIZE)
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(i18n.LANGUAGE_NAMES[lang])
            btn.setAccessibleName(i18n.LANGUAGE_NAMES[lang])
            btn.clicked.connect(lambda _c, lg=lang: self._on_clicked(lg))
            self._group.addButton(btn)
            row.addWidget(btn)
            self.buttons[lang] = btn
        self.reset()

    def reset(self) -> None:
        """Check the flag of the language this process runs in."""
        self.buttons[i18n.language()].setChecked(True)

    def _on_clicked(self, lang: str) -> None:
        if lang != i18n.language():
            self.language_requested.emit(lang)
