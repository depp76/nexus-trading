"""strategy/signal_api.py — the one seam between the strategy packages and the
screens that show a strategy's *current* signals (2026-11-16).

Trading Universe ("전략 신호": Top / Bottom lists pinned to the table) and the
MA chart dialog ("전략 점검": one stock's conditions) never import a strategy
package. They list the variants of every strategy in strategy.registry that
has a ``signals`` module and run them through this module, so a new strategy
shows up in both screens by registering a ``SignalProvider``.

A provider supplies, per variant id (ids are unique across strategies):
  * ``title(vid)``        short list label (Korean / English via i18n.tr);
  * ``markets(vid)``      the Universe markets it applies to (rows / charts of
                          other markets are not offered the variant);
  * ``needs_universe(vid)`` True when a stock's result is a rank within the
                          universe, so one stock alone cannot be checked;
  * ``labels(vid)``       ``ListLabels``: the words the screens use for the two
                          lists and the market-filter note;
  * ``load(vid, items, progress, should_stop)`` -> opaque inputs (network; use
                          strategy.common.inputs for daily prices / flows);
  * ``pick(vid, inputs, n)`` -> ``SignalPicks``;
  * ``check(vid, ticker, inputs, picks)`` -> ``StockCheck`` (``inputs`` is None
                          for a needs_universe variant, which reads ``picks``);
  * ``templates(vid)``    the largest ``StockCheck`` shapes of the variant (all
                          rows, widest values), so a screen can keep one height.

Optional chart overlays (docs/ma60_unification_2026-11-18.md 1.7; a provider
without them gives none, so other providers need no change):
  * ``chart_overlays(vid, ticker, market, ohlc, provisional_last)`` ->
                          ``list[ChartLine]``: price-panel lines of one stock
                          (trend following: the breakout channel);
  * ``regime_spans(vid, market, dates, index_close, provisional_last)`` ->
                          ``list[RegimeSpan]``: one market index's regime state;
  * ``uses_regime(vid)`` / ``regime_legend(vid)``: whether the variant has a
                          market filter, and its legend label + tooltip.
Inputs are what the screen's thread already loaded (no network here).

Universe runs are shared between the screens through ``SignalCache`` (kept for
the day they were computed on).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from strategy.registry import STRATEGIES


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class SignalName:
    ticker: str
    name: str
    market: str
    close: float
    value: float                # the figure the list is ordered by (see `value_label`)
    value_label: str            # e.g. "RS +12.3%p", "52주 고가의 98.1%", "+1.23"


@dataclass
class SignalPicks:
    variant_id: str
    as_of: date
    top: list[SignalName]
    bottom: list[SignalName]
    regime_on: bool | None      # market filter on the latest session (None: not available)
    uses_regime: bool           # the variant gates new entries on it
    n_universe: int
    n_liquid: int
    n_top_candidates: int       # names that met the Top condition (before the cut to n)
    n_bottom_candidates: int
    detail: object | None = None   # provider-private (trend following C rows: scoring.Recommendation)
    info: dict = field(default_factory=dict)

    def rank_of(self, ticker: str) -> tuple[str, int] | None:
        """("top" | "bottom", 1-based rank) of `ticker`, or None."""
        for side, names in (("top", self.top), ("bottom", self.bottom)):
            for i, s in enumerate(names, 1):
                if s.ticker == ticker:
                    return side, i
        return None


@dataclass
class ListLabels:
    """Display words of one variant's lists (built per call, so they follow
    the UI language)."""
    top_tag: str                # row tag word: "상위"
    bottom_tag: str             # "하위" (a score list) / "이탈" (an exit list)
    top_what: str               # status line: "오늘 진입 신호"
    bottom_what: str            # "추세 이탈(청산) 신호"
    regime_name: str = ""       # "시장 조건(L1)"; empty when the strategy has no market filter


@dataclass
class CheckRow:
    label: str                  # "추세: 종가 > 60일선"
    ok: bool | None             # None: not applicable / no data (a value-only row)
    value: str = ""             # optional figure ("97.3%")


@dataclass
class Metric:
    label: str                  # "MA60Div"
    value: str                  # "0.83" / "-"
    help: str = ""              # one-line caption under the value


@dataclass
class StockCheck:
    """One stock under one variant on the latest session. Every screen draws
    it the same way: verdict, optional score line and metric cards, then the
    entry conditions (Pass / Fail) and exit conditions (Hit / None)."""
    variant_id: str
    ticker: str
    as_of: date | None
    verdict: str                # "entry" | "exit" | "hold" | "none" | "na"
    verdict_text: str           # the headline
    entry: list[CheckRow]
    exit: list[CheckRow]
    note: str = ""
    score: str = ""             # headline figure ("+0.42"); empty: no score line
    score_caption: str = ""     # "주간 평균"
    score_hint: str = ""        # "최근 +0.40 · 상위 3"
    metrics: list[Metric] = field(default_factory=list)


@dataclass
class ChartLine:
    """One price-panel line of a stock chart, on the chart's own dates."""
    key: str                    # "breakout_high" | "exit_low"
    label: str                  # display name (tr() applied): "직전 20일 최고가"
    dates: list[date]
    values: list[float | None]  # None: no value (the line is broken there)
    style: str = "step"         # "step" | "line"
    provisional_from: date | None = None    # values from this date on use an intraday bar
    tooltip: str = ""           # how the line is defined (window, "today excluded")
    short_label: str = ""       # compact name for an end-of-line label ("20일 최고"); "" -> label


@dataclass
class RegimeSpan:
    """A run of sessions with one market-regime state, as judged at each
    session's close (it applies to orders from the next session)."""
    start: date
    end: date                   # inclusive
    on: bool                    # risk-on
    provisional: bool = False   # judged on an intraday bar or an unfinished week


class SignalProvider(Protocol):
    name: str
    default_variant: str        # optional: the variant the screens start on

    def variants(self) -> list[str]: ...
    def title(self, vid: str) -> str: ...
    def markets(self, vid: str) -> tuple[str, ...]: ...
    def needs_universe(self, vid: str) -> bool: ...
    def labels(self, vid: str) -> ListLabels: ...
    def load(self, vid: str, items: list[dict], progress=None, should_stop=None): ...
    def pick(self, vid: str, inputs, n: int = 10) -> SignalPicks: ...
    def check(self, vid: str, ticker: str, inputs, picks: SignalPicks | None = None) -> StockCheck: ...
    def templates(self, vid: str) -> list[StockCheck]: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PROVIDERS: dict[str, SignalProvider] | None = None


def providers() -> dict[str, SignalProvider]:
    """variant id -> provider, over every strategy in strategy.registry."""
    global _PROVIDERS
    if _PROVIDERS is None:
        out: dict[str, SignalProvider] = {}
        for entry in STRATEGIES:
            if not entry.signals:
                continue
            prov = importlib.import_module(entry.signals).PROVIDER
            for vid in prov.variants():
                if vid in out:
                    raise ValueError(f"variant id {vid!r} is offered by two strategies")
                out[vid] = prov
        _PROVIDERS = out
    return _PROVIDERS


def default_variant() -> str:
    """The variant the screens start on: the first registered strategy's
    ``default_variant`` (trend following: C1), else its first variant."""
    prov = next(iter(providers().values()))
    return getattr(prov, "default_variant", None) or prov.variants()[0]


def variants(market: str | None = None) -> list[str]:
    """Every variant id, or those that apply to `market`."""
    return [v for v, p in providers().items() if market is None or market in p.markets(v)]


def provider(vid: str) -> SignalProvider:
    return providers()[vid]


def title(vid: str) -> str:
    return provider(vid).title(vid)


def markets(vid: str) -> tuple[str, ...]:
    return tuple(provider(vid).markets(vid))


def needs_universe(vid: str) -> bool:
    return provider(vid).needs_universe(vid)


def labels(vid: str) -> ListLabels:
    return provider(vid).labels(vid)


def templates(vid: str) -> list[StockCheck]:
    """The variant's largest check shapes (empty when the provider has none)."""
    fn = getattr(provider(vid), "templates", None)
    return list(fn(vid)) if fn is not None else []


def uses_regime(vid: str) -> bool:
    """True when `vid` gates new entries on a market regime (False when the
    provider does not say)."""
    fn = getattr(provider(vid), "uses_regime", None)
    return bool(fn(vid)) if fn is not None else False


def regime_legend(vid: str) -> tuple[str, str]:
    """(legend label, tooltip) of `vid`'s regime shading; ("", "") if none."""
    fn = getattr(provider(vid), "regime_legend", None)
    return tuple(fn(vid)) if fn is not None else ("", "")


def chart_overlays(vid: str, ticker: str, market: str, ohlc, provisional_last: bool = False) -> list[ChartLine]:
    """Price-panel lines `vid`'s provider draws on `ticker`'s chart; `ohlc`
    maps "Date" / "High" / "Low" / "Close" to equal-length columns (a polars
    frame works). [] when the provider has none or the market is not its own."""
    fn = getattr(provider(vid), "chart_overlays", None)
    return list(fn(vid, ticker, market, ohlc, provisional_last)) if fn is not None else []


def regime_spans(vid: str, market: str, dates: list[date], index_close,
                 provisional_last: bool = False) -> list[RegimeSpan]:
    """`market`'s regime state under `vid` as spans over `dates`, from that
    market's own index closes. [] when the provider has none, the variant
    uses no regime or the market is not one it covers."""
    fn = getattr(provider(vid), "regime_spans", None)
    return list(fn(vid, market, dates, index_close, provisional_last)) if fn is not None else []


def universe_rows(items: list[dict], vid: str) -> list[dict]:
    """The Universe rows a universe run of `vid` takes: its markets, no index rows."""
    ms = markets(vid)
    return [d for d in items if d.get("market") in ms and not d.get("is_index")]


# ---------------------------------------------------------------------------
# Runs (network; called from worker threads)
# ---------------------------------------------------------------------------
def run_picks(items: list[dict], vid: str, n: int = 10, progress=None, should_stop=None) -> SignalPicks:
    """A universe run of `vid` over the rows of `items` it applies to."""
    prov = provider(vid)
    rows = universe_rows(items, vid)
    if not rows:
        raise ValueError(f"No {'/'.join(markets(vid))} rows for {vid}")
    return prov.pick(vid, prov.load(vid, rows, progress, should_stop), n)


def check_from_picks(vid: str, ticker: str, picks: SignalPicks | None) -> StockCheck:
    """A needs_universe variant's check: read from a universe run (no I/O)."""
    return provider(vid).check(vid, ticker, None, picks)


def run_check(item: dict, vid: str, picks: SignalPicks | None = None, progress=None,
              should_stop=None) -> StockCheck:
    """One stock (`item`: ticker / name / market) under `vid`; `picks` is a
    universe run of the same variant when one exists (for ranks)."""
    prov = provider(vid)
    ticker = str(item.get("ticker", ""))
    if prov.needs_universe(vid):
        return prov.check(vid, ticker, None, picks)
    return prov.check(vid, ticker, prov.load(vid, [item], progress, should_stop), picks)


# ---------------------------------------------------------------------------
# Shared universe runs
# ---------------------------------------------------------------------------
class SignalCache:
    """Universe runs by variant id, shared by the Universe tab and the MA
    charts it opens. A run counts only on the day it was computed (prices
    move overnight); the Universe tab also clears it when a refresh brings
    new prices."""

    def __init__(self, today=date.today):
        self._today = today
        self._runs: dict[str, tuple[SignalPicks, date]] = {}

    def put(self, picks: SignalPicks) -> None:
        self._runs[picks.variant_id] = (picks, self._today())

    def get(self, vid: str) -> SignalPicks | None:
        hit = self._runs.get(vid)
        if hit is None:
            return None
        picks, day = hit
        if day != self._today():
            del self._runs[vid]
            return None
        return picks

    def __contains__(self, vid: str) -> bool:
        return self.get(vid) is not None

    def clear(self, keep: SignalPicks | None = None) -> None:
        """Drop every run (except `keep`, e.g. the run pinned to the table)."""
        kept = self._runs.get(keep.variant_id) if keep is not None else None
        self._runs.clear()
        if kept is not None and kept[0] is keep:
            self._runs[keep.variant_id] = kept
