"""strategy/trend_following/universe_signals.py — today's buy candidates and
exit signals of one strategy variant over the Trading Universe rows (the
Universe "전략 신호" button, user direction 2026-11-16; spec 9-4 records the
display rules).

Every list comes from the same signal code the backtest uses, so the screen
and a backtest of the variant cannot drift apart:

* C1-C3 (2-5 scoring): the weekly Top / Bottom of ``scoring.weekly_recommendation``
  with the variant's ``score_overrides`` applied -- what the button showed
  before, now for C2 / C3 too.
* A0-A5 (2-3 breakout): Top = names with an entry signal on the latest session
  (trend_ok & breakout & liquid & traded, plus V / F when the row uses them),
  ordered as ``TrendFollowingPolicy`` picks among candidates (rank score of
  flow strength and RS, then RS). Usually only a few names a day -- the list
  is not padded to ten.
* B (52-week-high proximity): Top = trend_ok & liquid & traded names by
  proximity to the 52-week high, the order the weekly rebalance holds them in.
* A0-A5 and B, Bottom: names with the trend-break exit on the latest session
  (close < the prior ``exit_window``-session low; A5 also the FX flow exit),
  weakest relative strength first.

The L1 market regime is reported (``regime_on``) but does not empty the lists:
with the regime off a backtest of an L1 row would not open new positions,
which the caller states next to the list.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import numpy as np

from i18n import tr
from strategy.trend_following.config import VARIANTS, StrategyParams
from strategy.trend_following.dataset import PriceBook, _check_stop, _flow_arrays, _say
from strategy.trend_following.scoring import (
    Recommendation, compute_scores, load_score_inputs, weekly_recommendation,
)
from strategy.trend_following.signals import compute_features, rank_desc_score

logger = logging.getLogger(__name__)

DEFAULT_VARIANT = "C1"          # what the button showed before it got a strategy menu


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
    regime_on: bool | None      # L1 on the latest session (None without index data)
    uses_regime: bool           # the row gates new entries on L1
    n_universe: int
    n_liquid: int
    n_top_candidates: int       # names that met the Top condition (before the cut to n)
    n_bottom_candidates: int
    top_kind: str               # "score" | "entry" | "high52"
    bottom_kind: str            # "score" | "exit"
    recommendation: Recommendation | None = None   # C rows: the scoring result (MA chart detail panel)
    info: dict = field(default_factory=dict)


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%p"


def pick_signals(variant_id: str, book: PriceBook, index_close: np.ndarray | None,
                 flow_fi: np.ndarray | None = None, params: StrategyParams | None = None,
                 n: int = 10) -> SignalPicks:
    """The variant's Top / Bottom lists on the last session of `book` (pure)."""
    v = VARIANTS[variant_id]
    p = v.apply(params or StrategyParams())
    if book.T == 0:
        raise ValueError("No sessions to score")

    if v.mode == "score":
        sb = compute_scores(book, p)
        rec = weekly_recommendation(sb, book, p, n_top=n, index_close=index_close)

        def conv(s):
            return SignalName(s.ticker, s.name, s.market, s.close, s.week_avg, f"{s.week_avg:+.2f}")
        return SignalPicks(
            variant_id, rec.as_of, [conv(s) for s in rec.top], [conv(s) for s in rec.bottom], rec.regime_on,
            v.regime, rec.n_universe, rec.n_liquid, rec.n_gated, rec.n_liquid, "score", "score",
            recommendation=rec)

    idx = np.asarray(index_close, dtype=float) if index_close is not None else np.full(book.T, np.nan)
    ds = SimpleNamespace(stocks=book, index_close=idx, flow_fi=flow_fi, index_by_market=None)
    f = compute_features(ds, p)
    t = book.T - 1
    base = f.liquid[t] & f.traded[t]

    def name_of(j: int, value: float, label: str) -> SignalName:
        tk = book.tickers[j]
        return SignalName(tk, book.names.get(tk, tk), book.market_of(tk), float(book.close[t, j]), value, label)

    rs = f.rs[t]
    rs_sort = np.where(np.isfinite(rs), rs, -np.inf)
    if v.mode == "high52":
        prox = f.high52_prox[t]
        cand = np.flatnonzero(base & f.trend_ok[t] & np.isfinite(prox))
        order = cand[np.argsort(-prox[cand], kind="stable")]
        top = [name_of(j, float(prox[j]), tr(f"52주 고가의 {prox[j] * 100:.1f}%", f"{prox[j] * 100:.1f}% of 52w high"))
               for j in order[:n]]
        top_kind = "high52"
    else:
        mask = base & f.trend_ok[t] & f.breakout[t]
        if v.volume:
            mask &= f.vol_ok[t]
        if v.flow:
            mask &= f.flow_ok[t]
        cand = np.flatnonzero(mask)
        if cand.size:
            score = rank_desc_score(f.flow_strength[t, cand], rs[cand])
            cand = cand[np.lexsort((-rs_sort[cand], -score))]      # the backtest's candidate order
        order = cand
        top = [name_of(j, float(rs[j]), "RS " + (_pct(rs[j]) if np.isfinite(rs[j]) else "-")) for j in order[:n]]
        top_kind = "entry"

    exit_mask = base & f.exit_channel[t]
    if v.flow_exit:
        exit_mask |= base & f.flow_exit[t]
    ex = np.flatnonzero(exit_mask)
    ex = ex[np.argsort(rs_sort[ex], kind="stable")]               # weakest relative strength first
    bottom = [name_of(j, float(rs[j]), "RS " + (_pct(rs[j]) if np.isfinite(rs[j]) else "-")) for j in ex[:n]]

    regime_on = bool(f.regime_on[t]) if index_close is not None else None
    return SignalPicks(variant_id, book.dates[t], top, bottom, regime_on, v.regime, book.N, int(f.liquid[t].sum()),
                       int(len(order)), int(len(ex)), top_kind, "exit",
                       info={"flows": bool(f.has_flows), "flow_coverage": float(f.flow_coverage)})


def _load_flow_fi(book: PriceBook, start: str, progress=None, should_stop=None) -> np.ndarray | None:
    """KRW foreigner + institution net buys (spec 2-3) for every name of `book`."""
    from data.flows import FLOW_FETCH_CONCURRENCY, get_investor_flows
    frames = {}
    done = 0
    with ThreadPoolExecutor(max_workers=FLOW_FETCH_CONCURRENCY) as exe:
        futures = {exe.submit(get_investor_flows, tk, start): tk for tk in book.tickers}
        for fut in as_completed(futures):
            _check_stop(should_stop, futures)
            tk = futures[fut]
            done += 1
            try:
                df = fut.result()
            except Exception:
                # boundary: one name's flows failing leaves it without the F filter, not the run.
                logger.warning("investor flows failed for %s", tk, exc_info=True)
                df = None
            if df is not None and not df.is_empty():
                frames[tk] = df
            if done % 25 == 0 or done == len(book.tickers):
                _say(progress, tr(f"투자자 수급 {done}/{len(book.tickers)}", f"Investor flows {done}/{len(book.tickers)}"))
    flow_fi, _retail = _flow_arrays(book, list(book.dates), frames)
    return flow_fi


def run_universe_signals(items: list[dict], variant_id: str = DEFAULT_VARIANT,
                         params: StrategyParams | None = None, n: int = 10,
                         progress=None, should_stop=None) -> SignalPicks:
    """Load the Universe rows' history (and flows when the row needs them) and pick."""
    from data.cache import start_date
    v = VARIANTS[variant_id]
    book, index_close = load_score_inputs(items, progress=progress, should_stop=should_stop)
    _check_stop(should_stop)
    flow_fi = None
    if v.needs_flows:
        _say(progress, tr("투자자 수급 불러오는 중...", "Loading investor flows..."))
        flow_fi = _load_flow_fi(book, start_date(), progress, should_stop)
    _check_stop(should_stop)
    _say(progress, tr(f"{variant_id} 신호 계산 중...", f"Computing {variant_id} signals..."))
    return pick_signals(variant_id, book, index_close, flow_fi, params, n)
