"""strategy/registry.py — the one list of strategy packages (2026-11-16).

Every place that offers "a strategy" reads this list:
  * the Strategy tab builds one backtest page per entry with a ``page``
    (``ui.strategy_tab``; the page class is named by a dotted string so this
    layer never imports ``ui``);
  * strategy.signal_api loads the ``signals`` module's ``PROVIDER`` for the
    Trading Universe "전략 신호" menu and the MA chart's "전략 점검".

A new strategy package adds one ``StrategyEntry`` here (see CLAUDE.md
"Strategy rule").
"""
from __future__ import annotations

from dataclasses import dataclass

from i18n import tr


@dataclass(frozen=True)
class StrategyEntry:
    key: str                    # package name under src/strategy/
    title_ko: str
    title_en: str
    signals: str | None = None  # module exposing PROVIDER (strategy.signal_api.SignalProvider)
    page: str | None = None     # "module:Class" of the Strategy tab page (QWidget)

    def title(self) -> str:
        return tr(self.title_ko, self.title_en)    # evaluated per call: follows a language switch


STRATEGIES: tuple[StrategyEntry, ...] = (
    StrategyEntry("trend_following", "추세추종", "Trend Following",
                  signals="strategy.trend_following.signal_provider",
                  page="ui.trend_following_tab:TrendFollowingTab"),
)
