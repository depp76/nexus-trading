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

Chart overlays (strategy.signal_api 1.7, docs/ma60_unification_2026-11-18.md
[1b]): ``breakout_channel`` -- the prior ``breakout_window``-session high and
prior ``exit_window``-session low, today excluded, exactly the 2-3 / 3-1
arrays of ``compute_features`` -- and ``regime_spans_of`` -- ``regime_state``
over one market's own index, cut into spans. Both reuse the signals.py code;
nothing here changes a signal or a parameter.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np

from i18n import tr
from strategy.common.inputs import DailyInputs, load_daily_inputs
from strategy.common.prices import PriceBook, check_stop, say, to_date
from strategy.signal_api import (
    ChartLine, CheckRow, ListLabels, Metric, RegimeSpan, SignalName, SignalPicks, StockCheck,
)
from strategy.trend_following.config import VARIANTS, StrategyParams
from strategy.trend_following.scoring import compute_scores, weekly_recommendation
from strategy.trend_following.signals import compute_features, rank_desc_score, regime_state, rolling, shift
from strategy.trend_following.variant_info import describe_variant

logger = logging.getLogger(__name__)

DEFAULT_VARIANT = "C1"          # what the Universe button showed before it got a strategy menu
MARKETS = ("KOSPI", "KOSDAQ")   # spec 2-0: KR stocks (the scoring and the flows are KR-only)
INDEX_TICKER = "KS11"           # KOSPI: the calendar and the L1 regime input

__all__ = ["pick_signals", "check_stock", "layout_templates", "run_universe_signals", "load_inputs",
           "breakout_channel", "regime_spans_of", "DEFAULT_VARIANT", "MARKETS", "PROVIDER"]


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


def _stock_rows(v, p: StrategyParams, x) -> tuple[list[CheckRow], list[CheckRow]]:
    """A0-A5 / B entry and exit rows from the values in `x` (each may be None:
    the layout template). One place for the row list, so a check and its
    template always have the same rows."""
    entry: list[CheckRow] = []
    if v.regime:
        entry.append(CheckRow(tr(f"시장 조건: KOSPI > {p.regime_ma}일선, 상승", f"Regime: KOSPI > MA{p.regime_ma}, rising"),
                              x.regime))
    entry.append(CheckRow(tr("유동성: 20일 평균 거래대금 ≥ 100억", "Liquidity: 20-day avg value ≥ 10B"),
                          x.liquid, x.adv_text))
    entry.append(CheckRow(tr(f"추세: 종가 > {p.trend_ma_long}일선", f"Trend: close > MA{p.trend_ma_long}"), x.above))
    entry.append(CheckRow(tr(f"추세: {p.trend_ma_long}일선 상승", f"Trend: MA{p.trend_ma_long} rising"), x.rising))
    entry.append(CheckRow(tr(f"추세: {p.trend_ma_short}일선 > {p.trend_ma_long}일선",
                             f"Trend: MA{p.trend_ma_short} > MA{p.trend_ma_long}"), x.stacked))
    if v.mode == "high52":
        entry.append(CheckRow(tr("52주 고가 근접도", "52w-high proximity"), None, x.prox_text))
    else:
        entry.append(CheckRow(tr(f"{p.breakout_window}일 신고가 돌파", f"{p.breakout_window}-day high breakout"),
                              x.breakout, x.rs_text))
        if v.volume:
            entry.append(CheckRow(tr(f"거래량 ≥ {p.volume_ratio_min:g}배", f"Volume ≥ {p.volume_ratio_min:g}x"), x.vol))
        if v.flow:
            entry.append(CheckRow(tr(f"수급: 외국인+기관 {p.flow_long_window}일·{p.flow_short_window}일 순매수",
                                     f"Flows: foreign+inst net buy {p.flow_long_window}d & {p.flow_short_window}d"),
                                  x.flow))
    exits = [CheckRow(tr(f"추세 이탈: 종가 < 직전 {p.exit_window}일 최저가",
                         f"Trend break: close < prior {p.exit_window}-day low"), x.exit_channel)]
    if v.flow_exit:
        exits.append(CheckRow(tr(f"수급 청산: {p.flow_exit_window}일 순매도 & 종가 < {p.trend_ma_short}일선",
                                 f"Flow exit: {p.flow_exit_window}d net sell & close < MA{p.trend_ma_short}"),
                              x.flow_exit))
    return entry, exits


def layout_templates(variant_id: str, params: StrategyParams | None = None) -> list[StockCheck]:
    """The largest shapes a check of `variant_id` can take (every row, the
    widest sample values, the longest verdict and note), for a screen that
    keeps one height whatever it shows (MA chart "전략 점검", 2026-11-16)."""
    v = VARIANTS[variant_id]
    p = v.apply(params or StrategyParams())
    if v.mode == "score":
        out = [_score_check(variant_id, "", None, p)]                      # no run: note + "-" rows
        scored = _score_check(variant_id, "", None, p)
        scored.verdict_text = tr("매수 후보 (상위 10)", "Buy candidate (Top 10)")
        scored.score, scored.score_hint = "-0.00", tr("최근 -0.00 · 하위 10", "latest -0.00 · Bottom 10")
        scored.note = ""
        out.append(scored)
        unscored = _score_check(variant_id, "", None, p)
        unscored.note = tr("유동성 부족, 이력 부족 또는 국내 종목 아님", "Illiquid, too little history, or not a KR stock")
        out.append(unscored)
        return out
    no = False      # every row with a badge: the widest a row gets
    x = SimpleNamespace(regime=no, liquid=no, adv_text=tr("999,999억", "99,999.9B"), above=no, rising=no,
                        stacked=no, prox_text="100.0%", breakout=no, rs_text="RS +999.9%p", vol=no, flow=no,
                        exit_channel=no, flow_exit=no)
    entry, exits = _stock_rows(v, p, x)
    verdict = (tr("조건 통과 (순위는 유니버스 계산) · 시장 조건 꺼짐: 신규 매수 보류",
                  "Eligible (rank needs a universe run) · regime off: no new entries") if v.mode == "high52"
               else tr("진입 신호 · 시장 조건 꺼짐: 신규 매수 보류", "Entry signal · regime off: no new entries"))
    return [StockCheck(variant_id, "", None, "none", verdict, entry, exits)]


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

    rs = f.rs[t, j]
    adv = f.adv[t, j]
    prox = f.high52_prox[t, j]
    regime_ok = (bool(f.regime_on[t]) if index_close is not None else None) if v.regime else None
    x = SimpleNamespace(
        regime=regime_ok, liquid=bool(f.liquid[t, j]),
        adv_text=tr(f"{adv / 1e8:,.0f}억", f"{adv / 1e9:,.1f}B") if np.isfinite(adv) else "",
        above=bool(above), rising=bool(rising), stacked=bool(stacked),
        prox_text=f"{prox * 100:.1f}%" if np.isfinite(prox) else "-",
        breakout=bool(f.breakout[t, j]), rs_text=("RS " + _pct(rs)) if np.isfinite(rs) else "",
        vol=bool(f.vol_ok[t, j]), flow=bool(f.flow_ok[t, j]) if f.has_flows else None,
        exit_channel=bool(f.exit_channel[t, j]), flow_exit=bool(f.flow_exit[t, j]) if f.has_flows else None)
    entry, exits = _stock_rows(v, p, x)
    if v.mode == "high52":
        eligible = bool(f.trend_ok[t, j] and f.liquid[t, j] and f.traded[t, j] and np.isfinite(prox))
    else:
        # The backtest's entry mask (_breakout_entries) for this column; L1 is reported apart.
        eligible = bool(f.trend_ok[t, j] and f.breakout[t, j] and f.liquid[t, j] and f.traded[t, j]
                        and (not v.volume or f.vol_ok[t, j]) and (not v.flow or f.flow_ok[t, j]))

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
# Chart overlays (signal_api 1.7)
# ---------------------------------------------------------------------------
def _values(arr: np.ndarray) -> list[float | None]:
    return [float(v) if np.isfinite(v) else None for v in arr]


def breakout_channel(dates, high, low, params: StrategyParams,
                     provisional_last: bool = False) -> list[ChartLine]:
    """The 2-3 breakout level and the 3-1 exit level on each session: the
    highest high of the prior ``breakout_window`` sessions and the lowest low
    of the prior ``exit_window`` sessions, the session itself excluded (the
    same ``shift(rolling(...), 1)`` as compute_features). A window holding a
    missing bar has no value. With `provisional_last` the last session's bar is
    intraday, so a close compared with its level is not final yet."""
    p = params
    days = [to_date(d) for d in dates]
    hi = shift(rolling(np.asarray(high, dtype=float), p.breakout_window, "max"), 1)
    lo = shift(rolling(np.asarray(low, dtype=float), p.exit_window, "min"), 1)
    prov = days[-1] if provisional_last and days else None
    bw, ew = p.breakout_window, p.exit_window
    return [
        ChartLine("breakout_high", tr(f"직전 {bw}일 최고가", f"Prior {bw}-day high"), days, _values(hi),
                  provisional_from=prov,
                  tooltip=tr(f"당일 제외, 직전 {bw}거래일 고가의 최고값 — 종가가 넘으면 돌파(진입 조건)",
                             f"Highest high of the prior {bw} sessions, today excluded; "
                             "a close above it is a breakout"),
                  short_label=tr(f"{bw}일 최고", f"{bw}d high")),
        ChartLine("exit_low", tr(f"직전 {ew}일 최저가", f"Prior {ew}-day low"), days, _values(lo),
                  provisional_from=prov,
                  tooltip=tr(f"당일 제외, 직전 {ew}거래일 저가의 최저값 — 종가가 밑돌면 추세 이탈(청산)",
                             f"Lowest low of the prior {ew} sessions, today excluded; "
                             "a close below it is a trend-break exit"),
                  short_label=tr(f"{ew}일 최저", f"{ew}d low")),
    ]


def regime_spans_of(dates, index_close, params: StrategyParams,
                    provisional_last: bool = False) -> list[RegimeSpan]:
    """``regime_state`` of one index (close > MA60, MA60 above its value 5
    sessions back, judged on the weekly check day and held to the next one)
    as spans of equal state. A span is provisional when its state came from an
    intraday last bar, or from a check day ``weekly_check_days`` only picked
    because the current week has no later session yet (the check weekday has
    not come, so the week's real check may still change it)."""
    p = params
    days = [to_date(d) for d in dates]
    n = len(days)
    if n == 0:
        return []
    state, check = regime_state(np.asarray(index_close, dtype=float), days, p)
    prov = np.zeros(n, dtype=bool)
    if provisional_last and (check[-1] or not check.any()):
        prov[-1] = True
    last_week = days[-1].isocalendar()[:2]
    for k in range(n - 1, -1, -1):
        if days[k].isocalendar()[:2] != last_week:
            break
        if check[k] and days[k].weekday() < min(max(p.check_weekday, 0), 4):
            prov[k:] = True             # a fallback check in an unfinished week
            break
    spans: list[RegimeSpan] = []
    start = 0
    for t in range(1, n + 1):
        if t == n or state[t] != state[start] or prov[t] != prov[start]:
            spans.append(RegimeSpan(days[start], days[t - 1], bool(state[start]), bool(prov[start])))
            start = t
    return spans


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

    def templates(self, vid) -> list[StockCheck]:
        return layout_templates(vid)

    def check(self, vid, ticker, inputs: DailyInputs | None, picks=None) -> StockCheck:
        if inputs is None:
            return check_stock(vid, ticker, None, None, None, None, picks)
        return check_stock(vid, ticker, inputs.book, inputs.index_close, inputs.flow_fi, None, picks)

    # Chart overlays (signal_api 1.7): KR markets only, like the lists.
    def uses_regime(self, vid) -> bool:
        return bool(VARIANTS[vid].regime)

    def regime_legend(self, vid) -> tuple[str, str]:
        p = VARIANTS[vid].apply(StrategyParams())
        ma, lb = p.regime_ma, p.regime_slope_lookback
        return (tr("L1 Risk-on(종가 판단)", "L1 risk-on (judged at the close)"),
                tr(f"지수 종가 > {ma}일선이고 {ma}일선이 {lb}거래일 전보다 높음. 매주 점검일 종가로 판단해 "
                   "다음 점검일까지 유지하며, 다음 거래일부터의 신규 매수 허용 조건 중 하나다(추세·돌파 등 다른 "
                   "매수 조건도 따로 있고, A0은 이 조건을 쓰지 않는다). 옅은 칸은 장중 값이나 끝나지 않은 주로 "
                   "판단한 잠정 구간.",
                   f"Index close > MA{ma} and MA{ma} above its value {lb} sessions back, judged at the weekly "
                   "check day's close and held to the next check; one of the conditions for new entries from "
                   "the next session (trend, breakout and the rest still apply; A0 does not use it). Paler "
                   "cells: provisional (intraday bar or unfinished week)."))

    def chart_overlays(self, vid, ticker, market, ohlc, provisional_last=False) -> list[ChartLine]:
        if market not in MARKETS:
            return []
        p = VARIANTS[vid].apply(StrategyParams())
        return breakout_channel(list(ohlc["Date"]), ohlc["High"], ohlc["Low"], p, provisional_last)

    def regime_spans(self, vid, market, dates, index_close, provisional_last=False) -> list[RegimeSpan]:
        if market not in MARKETS or not VARIANTS[vid].regime:
            return []
        p = VARIANTS[vid].apply(StrategyParams())
        return regime_spans_of(dates, index_close, p, provisional_last)


PROVIDER = _TrendFollowingSignals()
