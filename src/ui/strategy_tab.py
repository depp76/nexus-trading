"""ui/strategy_tab.py — StrategyTab: the "Strategy" top-level tab. One page
per strategy in strategy.registry with a page (only Trend Following for now).

There are no sub-tabs (reports/strategy_run_settings_ui.md 1): each page's
toolbar starts with a strategy selector listing every registered strategy,
and picking one shows that page. Each page keeps its own markets, window,
conditions, last result and worker threads, so switching never mixes them;
this widget creates the pages and merges their collect_threads_to_stop()
for MainWindow.closeEvent.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget

import importlib

from strategy.registry import STRATEGIES
from ui.common import ThreadOwnerMixin


def _page_class(spec: str):
    module, _, cls = spec.partition(":")
    return getattr(importlib.import_module(module), cls)


# (selector label, page class), in strategy.registry order -- the one list of
# strategies (2026-11-16). A page needs a `strategy_combo` and `set_strategy_choices()`.
STRATEGY_TABS = tuple((e.title(), _page_class(e.page)) for e in STRATEGIES if e.page)


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


    # ---Language switch (docs/ui.md 8.20): MainWindow rebuilds every tab in
    # the new language and carries this state over. Plain values only (the
    # ui classes themselves are re-imported).
    def is_running(self) -> bool:
        return any(getattr(tab, "is_running", lambda: False)() for tab in self.strategy_tabs)

    def export_state(self) -> dict:
        return {"index": self.stack.currentIndex(),
                "pages": [tab.export_state() if hasattr(tab, "export_state") else None
                          for tab in self.strategy_tabs]}

    def import_state(self, state: dict) -> None:
        for tab, page in zip(self.strategy_tabs, state.get("pages", [])):
            if page is not None and hasattr(tab, "import_state"):
                tab.import_state(page)
        self.show_strategy(state.get("index", 0))

    def collect_threads_to_stop(self) -> list:
        out = super().collect_threads_to_stop()
        for tab in self.strategy_tabs:
            out.extend(tab.collect_threads_to_stop())
        return out
