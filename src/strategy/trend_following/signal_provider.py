"""strategy/trend_following/signal_provider.py — the trend-following
package's current-signal provider (strategy.signal_api, registered in
strategy.registry): today's buy candidates and exit signals of one variant
over the Trading Universe rows ("전략 신호"), and one stock's conditions
under a variant (the MA chart dialog's "전략 점검"). Spec 9-29 records the
display rules.

Every list and check comes from the same signal code the backtest uses, so
the screens and a backtest of the variant cannot drift apart:

* C1-C3 (2-5 scoring): the weekly Top / Bottom of ``scoring.weekly_recommendation``
  with the variant's ``score_overrides`` applied; a stock's check is its row
  of that run (the scores are ranks within the universe, so a universe run is
  needed).
* A0-A5 (2-3 breakout): Top = names with an entry signal on the latest session
  (trend_ok & breakout & liquid & traded, plus V / F when the row uses them),
  ordered as ``TrendFollowingPolicy`` picks among candidates (rank score of
  flow strength and RS, then RS). Not padded to ten.
* B (52-week-high proximity): Top = trend_ok & liquid & traded names by
  proximity to the 52-week high, the order the weekly rebalance holds them in.
* A0-A5 and B, Bottom: names with the trend-break exit on the latest session
  (close < the prior ``exit_window``-session low; A5 also the FX flow exit),
  weakest relative strength first.

The L1 market regime is reported (``regime_on``) but does not empty the lists.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np

from i18n import tr
from strategy.common.inputs import DailyInputs, load_daily_inputs
from strategy.common.prices import PriceBook, check_stop, say
from strategy.signal_api import CheckRow, ListLabels, Metric, SignalName, SignalPicks, StockCheck
from strategy.trend_following.config import VARIANTS, StrategyParams
from strategy.trend_following.scoring import compute_scores, weekly_recommendation
from strategy.trend_following.signals import compute_features, rank_desc_score, rolling, shift
from strategy.trend_following.variant_info import describe_variant

logger = logging.getLogger(__name__)

DEFAULT_VARIANT = "C1"          # what the Universe button showed before it got a strategy menu
MARKETS = ("KOSPI", "KOSDAQ")   # spec 2-0: KR stocks (the scoring and the flows are KR-only)
INDEX_TICKER = "KS11"           # KOSPI: the calendar and the L1 regime input

__all__ = ["pick_signals", "check_stock", "run_universe_signals", "load_inputs",
           "DEFAULT_VARIANT", "MARKETS", "PROVIDER"]


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%p"


def _f2(x) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) and np.isfinite(x) else "-"


def _features(book: PriceBook, index_close, flow_fi, p: StrategyParams):
    idx = np.asarray(index_close, dtype=float) if index_close is not None else np.full(book.T, np.nan)
    ds = SimpleNamespace(stocks=book, index_close=idx, flow_fi=flow_fi, index_by_market=None)
    return compute_features(ds, p)


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
            v.regime, rec.n_universe, rec.n_liquid, rec.n_gated, rec.n_liquid, detail=rec)

    f = _features(book, index_close, flow_fi, p)
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

    exit_mask = base & f.exit_channel[t]
    if v.flow_exit:
        exit_mask |= base & f.flow_exit[t]
    ex = np.flatnonzero(exit_mask)
    ex = ex[np.argsort(rs_sort[ex], kind="stable")]               # weakest relative strength first
    bottom = [name_of(j, float(rs[j]), "RS " + (_pct(rs[j]) if np.isfinite(rs[j]) else "-")) for j in ex[:n]]

    regime_on = bool(f.regime_on[t]) if index_close is not None else None
    return SignalPicks(variant_id, book.dates[t], top, bottom, regime_on, v.regime, book.N, int(f.liquid[t].sum()),
                       int(len(order)), int(len(ex)),
                       info={"flows": bool(f.has_flows), "flow_coverage": float(f.flow_coverage)})


# ---------------------------------------------------------------------------
# One stock under one variant (the MA chart's "전략 점검")
# ---------------------------------------------------------------------------
def _score_check(vid: str, ticker: str, picks: SignalPicks | None, p: StrategyParams) -> StockCheck:
    """C rows: the stock's row of a universe run of the same variant (spec 9-4
    breakdown: weekly average, the three latest percentiles, the four gates).
    Every row and card is there without a run too ("-"), so the block keeps
    one shape."""
    rec = picks.detail if picks is not None and picks.variant_id == vid else None
    s = rec.scored.get(ticker) if rec is not None else None
    gates = [(tr("유동성", "Liquidity"), lambda x: bool(x.liquid)),
             ("MA50Div > 0", lambda x: bool(np.isfinite(x.ma50_div) and x.ma50_div > 0)),
             (f"Range52 ≥ {p.gate_range_min:.2f}",
              lambda x: bool(np.isfinite(x.range52) and x.range52 >= p.gate_range_min)),
             (tr("과열 아님", "Not overheated"), lambda x: not x.overheated)]
    metric_defs = (("MA50Div", tr("추세 방향 +", "Trend direction +"), "pct_ma50"),
                   ("Range52", tr("추세 위치 +", "Trend position +"), "pct_range52"),
                   ("R3", tr("최근 3일 −", "Last 3 days −"), "pct_r3"))
    metrics = [Metric(label, _f2(getattr(s, attr)) if s is not None else "-", help_)
               for label, help_, attr in metric_defs]
    entry = [CheckRow(label, test(s) if s is not None else None) for label, test in gates]
    caption = tr("주간 평균", "Weekly avg")
    if rec is None:
        return StockCheck(vid, ticker, None, "na", tr("유니버스 계산 필요", "Needs a universe run"), entry, [],
                          note=tr("점수는 유니버스 안 순위라 전체 종목 계산이 필요합니다",
                                  "Scores are ranks within the universe, so the whole universe is computed"),
                          score="-", score_caption=caption, metrics=metrics)
    if s is None:
        return StockCheck(vid, ticker, rec.as_of, "na", tr("채점 제외", "Not scored"), entry, [],
                          note=tr("유동성 부족, 이력 부족 또는 국내 종목 아님",
                                  "Illiquid, too little history, or not a KR stock"),
                          score="-", score_caption=caption, metrics=metrics)
    rank = picks.rank_of(ticker)
    if rank and rank[0] == "top":
        verdict, text = "entry", tr(f"매수 후보 (상위 {rank[1]})", f"Buy candidate (Top {rank[1]})")
    elif rank:
        verdict, text = "exit", tr(f"약세 (하위 {rank[1]})", f"Weak (Bottom {rank[1]})")
    else:
        verdict, text = "none", tr("상위·하위 아님", "Neither top nor bottom")
    hint = tr(f"최근 {s.latest_total:+.2f}", f"latest {s.latest_total:+.2f}")
    if rank:
        hint += " · " + (tr(f"상위 {rank[1]}", f"Top {rank[1]}") if rank[0] == "top"
                         else tr(f"하위 {rank[1]}", f"Bottom {rank[1]}"))
    return StockCheck(vid, ticker, rec.as_of, verdict, text, entry, [], score=f"{s.week_avg:+.2f}",
                      score_caption=caption, score_hint=hint, metrics=metrics)


def check_stock(variant_id: str, ticker: str, book: PriceBook | None, index_close: np.ndarray | None,
                flow_fi: np.ndarray | None = None, params: StrategyParams | None = None,
                picks: SignalPicks | None = None) -> StockCheck:
    """`ticker`'s entry and exit conditions under `variant_id` on the last
    session of `book` (pure). `picks` is a universe run of the same variant
    when one exists: C rows need it (ranks), B uses it for the holding rank."""
    v = VARIANTS[variant_id]
    p = v.apply(params or StrategyParams())
    if v.mode == "score":
        return _score_check(variant_id, ticker, picks, p)
    if book is None or ticker not in book._col or book.T == 0:
        return StockCheck(variant_id, ticker, None, "na", tr("데이터 없음", "No data"), [], [])

    f = _features(book, index_close, flow_fi, p)
    t, j = book.T - 1, book.col(ticker)
    close = book.close[:, j]
    ma_s = rolling(close, p.trend_ma_short, "mean")
    ma_l = rolling(close, p.trend_ma_long, "mean")
    above = close[t] > ma_l[t]
    rising = ma_l[t] > shift(ma_l, p.trend_slope_lookback)[t]
    stacked = ma_s[t] > ma_l[t]

    entry: list[CheckRow] = []
    regime_ok = None
    if v.regime:
        regime_ok = bool(f.regime_on[t]) if index_close is not None else None
        entry.append(CheckRow(tr(f"시장 조건: KOSPI > {p.regime_ma}일선, 상승", f"Regime: KOSPI > MA{p.regime_ma}, rising"),
                              regime_ok))
    adv = f.adv[t, j]
    entry.append(CheckRow(tr("유동성: 20일 평균 거래대금 ≥ 100억", "Liquidity: 20-day avg value ≥ 10B"),
                          bool(f.liquid[t, j]),
                          tr(f"{adv / 1e8:,.0f}억", f"{adv / 1e9:,.1f}B") if np.isfinite(adv) else ""))
    entry.append(CheckRow(tr(f"추세: 종가 > {p.trend_ma_long}일선", f"Trend: close > MA{p.trend_ma_long}"), bool(above)))
    entry.append(CheckRow(tr(f"추세: {p.trend_ma_long}일선 상승", f"Trend: MA{p.trend_ma_long} rising"), bool(rising)))
    entry.append(CheckRow(tr(f"추세: {p.trend_ma_short}일선 > {p.trend_ma_long}일선",
                             f"Trend: MA{p.trend_ma_short} > MA{p.trend_ma_long}"), bool(stacked)))
    rs = f.rs[t, j]
    rs_text = ("RS " + _pct(rs)) if np.isfinite(rs) else ""

    if v.mode == "high52":
        prox = f.high52_prox[t, j]
        entry.append(CheckRow(tr("52주 고가 근접도", "52w-high proximity"), None,
                              f"{prox * 100:.1f}%" if np.isfinite(prox) else "-"))
        eligible = bool(f.trend_ok[t, j] and f.liquid[t, j] and f.traded[t, j] and np.isfinite(prox))
    else:
        entry.append(CheckRow(tr(f"{p.breakout_window}일 신고가 돌파", f"{p.breakout_window}-day high breakout"),
                              bool(f.breakout[t, j]), rs_text))
        if v.volume:
            entry.append(CheckRow(tr(f"거래량 ≥ {p.volume_ratio_min:g}배", f"Volume ≥ {p.volume_ratio_min:g}x"),
                                  bool(f.vol_ok[t, j])))
        if v.flow:
            entry.append(CheckRow(tr(f"수급: 외국인+기관 {p.flow_long_window}일·{p.flow_short_window}일 순매수",
                                     f"Flows: foreign+inst net buy {p.flow_long_window}d & {p.flow_short_window}d"),
                                  bool(f.flow_ok[t, j]) if f.has_flows else None))
        # The backtest's entry mask (_breakout_entries) for this column; L1 is reported apart.
        eligible = bool(f.trend_ok[t, j] and f.breakout[t, j] and f.liquid[t, j] and f.traded[t, j]
                        and (not v.volume or f.vol_ok[t, j]) and (not v.flow or f.flow_ok[t, j]))

    exits = [CheckRow(tr(f"추세 이탈: 종가 < 직전 {p.exit_window}일 최저가",
                         f"Trend break: close < prior {p.exit_window}-day low"),
                      bool(f.exit_channel[t, j]))]
    if v.flow_exit:
        exits.append(CheckRow(tr(f"수급 청산: {p.flow_exit_window}일 순매도 & 종가 < {p.trend_ma_short}일선",
                                 f"Flow exit: {p.flow_exit_window}d net sell & close < MA{p.trend_ma_short}"),
                              bool(f.flow_exit[t, j]) if f.has_flows else None))

    regime_note = "" if regime_ok is not False else tr(" · 시장 조건 꺼짐: 신규 매수 보류", " · regime off: no new entries")
    if any(r.ok for r in exits):
        verdict, text = "exit", tr("청산 신호", "Exit signal")
    elif v.mode == "high52":
        rank = picks.rank_of(ticker) if picks is not None and picks.variant_id == variant_id else None
        if eligible and rank and rank[0] == "top" and rank[1] <= p.max_positions:
            verdict, text = "hold", tr(f"보유 대상 (상위 {rank[1]})", f"Held (Top {rank[1]})")
        elif eligible:
            verdict = "none"
            text = (tr(f"조건 통과 · 상위 {rank[1]}", f"Eligible · Top {rank[1]}") if rank and rank[0] == "top"
                    else tr("조건 통과 (순위는 유니버스 계산)", "Eligible (rank needs a universe run)") if picks is None
                    else tr("조건 통과 · 상위 10 밖", "Eligible · outside the top 10"))
        else:
            verdict, text = "none", tr("조건 미달", "Not eligible")
        text += regime_note
    elif eligible:
        verdict, text = "entry", tr("진입 신호", "Entry signal") + regime_note
    else:
        verdict, text = "none", tr("신호 없음", "No signal")
    return StockCheck(variant_id, ticker, book.dates[t], verdict, text, entry, exits)


# ---------------------------------------------------------------------------
# Network side + provider
# ---------------------------------------------------------------------------
def load_inputs(variant_id: str, items: list[dict], progress=None, should_stop=None) -> DailyInputs:
    """KR daily prices on the KOSPI calendar (+ investor flows for the F/FX rows)."""
    return load_daily_inputs(items, INDEX_TICKER, VARIANTS[variant_id].needs_flows, progress, should_stop)


def run_universe_signals(items: list[dict], variant_id: str = DEFAULT_VARIANT,
                         params: StrategyParams | None = None, n: int = 10,
                         progress=None, should_stop=None) -> SignalPicks:
    """Load the rows' history (and flows when the row needs them) and pick."""
    inp = load_inputs(variant_id, items, progress, should_stop)
    check_stop(should_stop)
    say(progress, tr(f"{variant_id} 신호 계산 중...", f"Computing {variant_id} signals..."))
    return pick_signals(variant_id, inp.book, inp.index_close, inp.flow_fi, params, n)


class _TrendFollowingSignals:
    """strategy.signal_api.SignalProvider for this package (A0-A5, B, C1-C3)."""
    name = "trend_following"
    default_variant = DEFAULT_VARIANT

    def variants(self) -> list[str]:
        return list(VARIANTS)

    def title(self, vid: str) -> str:
        return describe_variant(vid).title

    def markets(self, vid: str) -> tuple[str, ...]:
        return MARKETS

    def needs_universe(self, vid: str) -> bool:
        return VARIANTS[vid].mode == "score"

    def labels(self, vid: str) -> ListLabels:
        mode = VARIANTS[vid].mode
        regime = tr("시장 조건(L1)", "L1 regime")
        if mode == "score":
            return ListLabels(tr("상위", "Top"), tr("하위", "Bottom"), tr("점수 상위", "top score"),
                              tr("점수 하위", "bottom score"), regime)
        top_what = (tr("52주 고가 근접", "52w-high proximity") if mode == "high52"
                    else tr("오늘 진입 신호", "entry signals today"))
        return ListLabels(tr("상위", "Top"), tr("이탈", "Exit"), top_what,
                          tr("추세 이탈(청산) 신호", "trend-break exits"), regime)

    def load(self, vid, items, progress=None, should_stop=None) -> DailyInputs:
        return load_inputs(vid, items, progress, should_stop)

    def pick(self, vid, inputs: DailyInputs, n: int = 10) -> SignalPicks:
        return pick_signals(vid, inputs.book, inputs.index_close, inputs.flow_fi, None, n)

    def check(self, vid, ticker, inputs: DailyInputs | None, picks=None) -> StockCheck:
        if inputs is None:
            return check_stock(vid, ticker, None, None, None, None, picks)
        return check_stock(vid, ticker, inputs.book, inputs.index_close, inputs.flow_fi, None, picks)


PROVIDER = _TrendFollowingSignals()
