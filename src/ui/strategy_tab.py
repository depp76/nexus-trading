"""ui/strategy_tab.py — StrategyTab: the "Strategy" top-level tab. One page
per strategy package under src/strategy/ (only Trend Following for now).

There are no sub-tabs (reports/strategy_run_settings_ui.md 1): each page's
toolbar starts with a strategy selector listing every registered strategy,
and picking one shows that page. Each page keeps its own markets, window,
conditions, last result and worker threads, so switching never mixes them;
this widget creates the pages and merges their collect_threads_to_stop()
for MainWindow.closeEvent.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget

from i18n import tr
from ui.common import ThreadOwnerMixin
from ui.trend_following_tab import TrendFollowingTab

# (selector label, page class), in list order. A new strategy adds its page
# here; the page needs a `strategy_combo` and `set_strategy_choices()`.
STRATEGY_TABS = (
    (tr("추세추종", "Trend Following"), TrendFollowingTab),
)


class StrategyTab(ThreadOwnerMixin, QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.strategy_tabs: list[QWidget] = []
        names = [label for label, _ in STRATEGY_TABS]
        for i, (_, cls) in enumerate(STRATEGY_TABS):
            tab = cls()
            tab.set_strategy_choices(names, i)
            tab.strategy_combo.activated.connect(self.show_strategy)
            self.strategy_tabs.append(tab)
            self.stack.addWidget(tab)
        self.trend_following_tab = self.strategy_tabs[0]
        root.addWidget(self.stack)

    def show_strategy(self, index: int) -> None:
        """Show strategy `index`; every page's selector shows the same name."""
        self.stack.setCurrentIndex(index)
        for tab in self.strategy_tabs:
            tab.strategy_combo.blockSignals(True)
            tab.strategy_combo.setCurrentIndex(index)
            tab.strategy_combo.blockSignals(False)

    def collect_threads_to_stop(self) -> list:
        out = super().collect_threads_to_stop()
        for tab in self.strategy_tabs:
            out.extend(tab.collect_threads_to_stop())
        return out
