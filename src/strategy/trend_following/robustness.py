"""strategy/trend_following/robustness.py — Robustness runner and the checklist-form
report (trend_following.md 6-4 "강건성", 6-5, 9-17; docs/nexus/validation_checklist.md E1-E5, A2).

What it adds on top of ``run_research`` (all on the development window, 1x costs):
  E1  the five weekly check weekdays, worst-weekday CAGR / MDD per variant;
  E2  the pre-registered neighbour set ``config.E2_NEIGHBORS`` (one field at a time, 31 extra
      runs for the full matrix up to v06; N6 / N7 make it 37 since v07 by adding C1-C3, and add
      6 more for the reproduction ids C1L-C3L, spec 10-2 / 10-13) judged
      by |delta Sharpe| > ``config.E2_SHARPE_JUMP``;
  E4  contribution of the top 5% of trades (by net P&L) and the result without them;
  E5  a counter of every strategy backtest executed for the report;
  A2  an optional validation-only "investor flow one session early" contamination comparison;
  ref an optional "cash earns no interest" rerun of each variant at 1x (spec 10-4, reference only).

This is NOT a parameter-picking tool. Neighbour, weekday and E4 results only answer "did the
result jump?"; defaults and the E2 set change only through an [R] -> [S] spec revision. The
runner reads the neighbour list from the config constant and refuses anything else, never takes
``allow_holdout``, and never declares a checklist pass (Validation does, NEX-10 plan, stage 5).
"""
from __future__ import annotations

from i18n import tr

import math
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np

from strategy.trend_following.backtest import TRIM_REASON, WEIGHT_TRIM_REASON, run_backtest
from strategy.trend_following.config import (
    DEV_END, E2_NEIGHBORS, E2_SHARPE_JUMP, E4_TOP_SHARE, PERIODS, StrategyParams, get_variant,
)
from strategy.trend_following.dataset import check_dev_window
from strategy.trend_following.metrics import (
    NAV_BASE, beta_alpha, cagr, max_drawdown, nav_returns, session_rf, sharpe, summarize_run,
)
from strategy.trend_following.research import (
    SUMMARY_COLUMNS, ResearchRequest, ResearchResult, _num, _pct, _table, cash_flow_table, run_research,
)
from strategy.trend_following.scoring import compute_scores
from strategy.trend_following.signals import compute_features

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri")
GROUP_BASE = "6-2 base matrix (variant x cost)"
GROUP_E1 = "E1 weekday runs (extra weekdays)"
GROUP_E2 = "E2 neighbour runs"
GROUP_A2 = "A2 contamination runs"
GROUP_NO_INTEREST = "cash interest 0 runs (reference)"


class UnregisteredNeighborError(ValueError):
    """A (field, value) pair outside ``config.E2_NEIGHBORS`` was requested."""


# ---------------------------------------------------------------------------
# E2 pre-registered neighbour set
# ---------------------------------------------------------------------------
def _is_registered(field_name: str, value) -> bool:
    return any(f == field_name and type(v) is type(value) and v == value for _, f, v, _ in E2_NEIGHBORS)


def neighbor_params(base: StrategyParams, field_name: str, value) -> StrategyParams:
    """`base` with exactly one field moved to a registered neighbour value; anything else raises."""
    if not _is_registered(field_name, value):
        raise UnregisteredNeighborError(
            f"({field_name}, {value!r}) is not in config.E2_NEIGHBORS; the E2 set is pre-registered and "
            "changes only through a spec revision")
    return replace(base, **{field_name: value})


def planned_e2_runs(variant_ids: tuple[str, ...] | None = None) -> list[tuple[str, str, object, str]]:
    """(neighbour id, field, value, variant id) for every E2 run, in table order, restricted to
    `variant_ids` (default: all). The full set has 31 rows; C1-C3 never appear."""
    wanted = set(variant_ids) if variant_ids is not None else None
    return [(nid, f, v, vid) for nid, f, v, vids in E2_NEIGHBORS for vid in vids
            if wanted is None or vid in wanted]


# Sessions of history (up to and including the first evaluation session) each registered field needs
# before its indicator is valid on day one.
_WARMUP_NEEDED = {
    "breakout_window": lambda p: p.breakout_window + 1,                       # prior-N high, shifted by one
    "trend_ma_long": lambda p: p.trend_ma_long + p.trend_slope_lookback,      # MA and its value N sessions ago
    "regime_ma": lambda p: p.regime_ma + p.regime_slope_lookback,
    "score_ma_long": lambda p: p.score_ma_long,                               # 2-5: the MA alone, no slope
}


def warmup_check(ds, base: StrategyParams, field_name: str, value) -> dict:
    """Does the snapshot's warm-up cover the neighbour value on the first evaluation session?
    (NEX-51 plan: if not, do not run it and tell the CIO -- the window is never shortened.)"""
    p = neighbor_params(base, field_name, value)
    needed = int(_WARMUP_NEEDED[field_name](p))
    available = int(ds.start_idx) + 1
    return {"field": field_name, "value": value, "needed_sessions": needed, "available_sessions": available,
            "ok": available >= needed}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PerfStats:
    sharpe: float
    cagr: float
    mdd: float
    n_trades: int = 0


def stats_from_metrics(m: dict) -> PerfStats:
    return PerfStats(float(m["sharpe"]), float(m["cagr"]), float(m["mdd"]), int(m.get("n_trades", 0)))


def period_stats(nav, dates: list[date], rf, periods=PERIODS) -> dict[str, PerfStats | None]:
    """Sharpe / CAGR / MDD of each calendar-year period, measured from the previous period's last NAV
    (the first period from the seed), with the same functions the 6-4 scorecard uses."""
    nav = np.asarray(nav, dtype=float)
    rf = np.zeros(len(nav)) if rf is None else np.asarray(rf, dtype=float)
    years = np.array([d.year for d in dates])
    out: dict[str, PerfStats | None] = {}
    for label, y0, y1 in periods:
        idx = np.flatnonzero((years >= y0) & (years <= y1))
        if idx.size < 3:
            out[label] = None
            continue
        i0 = int(idx[0])
        base = nav[i0 - 1] if i0 > 0 else NAV_BASE
        if not base > 0:
            out[label] = None
            continue
        seq = nav[idx] / base
        out[label] = PerfStats(sharpe(nav_returns(seq), session_rf(rf[idx], first_session=i0 == 0)),
                               cagr(seq, [dates[i] for i in idx]),
                               max_drawdown(seq))
    return out


@dataclass
class RunCounter:
    """Checklist E5: how many strategy backtests were executed to produce the report."""
    groups: dict[str, int] = field(default_factory=dict)
    prior: int = 0          # runs of earlier reports, supplied by the caller

    def add(self, group: str, n: int = 1) -> None:
        self.groups[group] = self.groups.get(group, 0) + int(n)

    @property
    def this_report(self) -> int:
        return sum(self.groups.values())

    @property
    def total(self) -> int:
        return self.this_report + self.prior


@dataclass
class WeekdayRun:
    variant: str
    weekday: int
    whole: PerfStats
    periods: dict[str, PerfStats | None]


@dataclass
class NeighborRun:
    nid: str
    field: str
    value: object
    variant: str
    whole: PerfStats
    periods: dict[str, PerfStats | None]
    base_sharpe: float

    @property
    def delta_sharpe(self) -> float:
        return self.whole.sharpe - self.base_sharpe

    @property
    def passed(self) -> bool:
        return abs(self.delta_sharpe) <= E2_SHARPE_JUMP


@dataclass
class TopTradeRow:
    variant: str
    n_trades: int
    k: int
    total_pnl: float
    top_pnl: float
    share: float | None                # top_pnl / total_pnl, None when total_pnl <= 0
    ex_pnl: float
    annual_pct: float                  # total_pnl / seed / years (simple, non-compounded approximation)
    ex_annual_pct: float


@dataclass
class ContaminationRow:
    variant: str
    clean: PerfStats
    contaminated: PerfStats


@dataclass
class NoInterestRow:
    """Spec 10-4: one variant at 1x with and without interest on cash (same everything else)."""
    variant: str
    with_interest: PerfStats
    no_interest: PerfStats
    avg_cash_weight: float


def top_trade_stats(variant: str, trades: list, seed: float, years: float,
                    top_share: float = E4_TOP_SHARE) -> TopTradeRow:
    """E4: real trades only (year-end and weight trims are partial sells, not decisions), ranked by net P&L."""
    real = sorted((t for t in trades if t.reason not in (TRIM_REASON, WEIGHT_TRIM_REASON)),
                  key=lambda t: t.pnl, reverse=True)
    n = len(real)
    k = max(1, math.ceil(top_share * n)) if n else 0
    total = float(sum(t.pnl for t in real))
    top = float(sum(t.pnl for t in real[:k]))
    scale = (seed * years) if seed > 0 and years > 0 else float("nan")
    return TopTradeRow(variant, n, k, total, top, (top / total) if total > 0 else None, total - top,
                       total / scale, (total - top) / scale)


# ---------------------------------------------------------------------------
# Request / result
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RobustnessRequest:
    research: ResearchRequest
    run_e1: bool = True
    run_e2: bool = True
    flow_contamination: int = 0     # validation-only (checklist A2): 0 = off (default), 1 = flow one session early
    prior_trials: int = 0           # strategy backtests counted in earlier reports (E5 running total)
    cash_interest_reference: bool = False   # spec 10-4: also rerun each variant at 1x with no cash interest

    def __post_init__(self):
        r = self.research
        if r.allow_holdout:
            raise ValueError("the robustness runner never uses the holdout (allow_holdout must stay False)")
        if r.params != StrategyParams():
            raise ValueError("the E2 neighbour set is registered against StrategyParams() defaults; "
                             "the robustness runner does not accept other base parameters")
        if not any(abs(m - 1.0) < 1e-9 for m in r.cost_multipliers):
            raise ValueError("the base matrix must include the 1x cost scenario (E1/E2/E4 are 1x only)")
        if not r.run_backtests:
            raise ValueError("run_backtests must stay on for the robustness runner")
        if self.flow_contamination not in (0, 1):
            raise ValueError("flow_contamination is 0 (off) or 1 (one session early)")
        check_dev_window(r.end, False, "end date")


@dataclass
class RobustnessResult:
    request: RobustnessRequest
    research: ResearchResult
    weekday_runs: list[WeekdayRun]
    neighbor_runs: list[NeighborRun]
    e2_checks: list[dict]               # warm-up status of every registered neighbour
    e2_skipped: list[str]
    top_trades: list[TopTradeRow]
    contamination: list[ContaminationRow]
    counter: RunCounter
    data_last_date: date | None
    no_interest: list[NoInterestRow] = field(default_factory=list)

    # -- E1 --------------------------------------------------------------
    def weekday_summary(self) -> list[dict]:
        out = []
        for vid in dict.fromkeys(r.variant for r in self.weekday_runs):
            rows = sorted((r for r in self.weekday_runs if r.variant == vid), key=lambda r: r.weekday)
            worst_c = min(rows, key=lambda r: r.whole.cagr)
            worst_m = min(rows, key=lambda r: r.whole.mdd)
            out.append({"variant": vid, "rows": rows, "worst_cagr": worst_c, "worst_mdd": worst_m,
                        "cagr_spread": max(r.whole.cagr for r in rows) - min(r.whole.cagr for r in rows)})
        return out

    # -- E2 --------------------------------------------------------------
    def e2_verdicts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for vid in dict.fromkeys(r.variant for r in self.neighbor_runs):
            out[vid] = "pass" if all(r.passed for r in self.neighbor_runs if r.variant == vid) else "fail"
        return out

    def to_markdown(self, header: list[str] | None = None) -> str:
        return render_robustness_markdown(self, header)


def _say(progress, msg: str) -> None:
    if progress is not None:
        progress(msg)


def _stop(should_stop) -> None:
    from strategy.trend_following.dataset import ResearchCancelled
    if should_stop is not None and should_stop():
        raise ResearchCancelled("cancelled")


def variant_for_neighbor(vid: str, field_name: str | None):
    """`vid`'s Variant, without its own override of `field_name` when it has one: a v07
    candidate overrides score_ma_long (60), and Variant.apply() runs after the neighbour is put
    into the params, so the override would silently undo N6 / N7 (spec 10-9 item 2)."""
    v = get_variant(vid)
    if field_name is None or field_name not in dict(v.score_overrides):
        return v
    return replace(v, score_overrides=tuple(kv for kv in v.score_overrides if kv[0] != field_name))


def _run_variant(ds, feats, params, vid: str, cm, score_cache: dict, free_field: str | None = None,
                 cash_interest: bool = True):
    """One backtest of `vid`. `free_field`: the E2 field the neighbour params set, kept out of the
    variant's own overrides (variant_for_neighbor)."""
    v = variant_for_neighbor(vid, free_field)
    scores = None
    if v.mode == "score":
        if vid not in score_cache:
            score_cache[vid] = compute_scores(ds.stocks, v.apply(params))
        scores = score_cache[vid]
    return run_backtest(ds, feats, params, v, cm, scores=scores, cash_interest=cash_interest)


def _e1_weekday_runs(ds, p, runnable, base1, cm1, counter, progress, should_stop) -> list[WeekdayRun]:
    """Checklist E1 (spec 6-4, 9-17): every variant on each of the five check weekdays; the
    default weekday reuses the 1x base-matrix run instead of running it again."""
    weekday_runs: list[WeekdayRun] = []
    for w in range(5):
        if w == p.check_weekday:
            for vid in runnable:
                s = base1[vid]
                weekday_runs.append(WeekdayRun(vid, w, stats_from_metrics(s.metrics),
                                               period_stats(s.nav, s.dates, s.rf_period)))
            continue
        pw = replace(p, check_weekday=w)
        featsw = compute_features(ds, pw)
        cache: dict = {}
        for vid in runnable:
            _stop(should_stop)
            _say(progress, tr(f"E1: {vid} 점검 요일 {WEEKDAY_NAMES[w]}...", f"E1: {vid} with check weekday {WEEKDAY_NAMES[w]}..."))
            res = _run_variant(ds, featsw, pw, vid, cm1, cache)
            counter.add(GROUP_E1)
            weekday_runs.append(WeekdayRun(vid, w, stats_from_metrics(summarize_run(res)),
                                           period_stats(res.nav, res.dates, res.rf_period)))
    return weekday_runs


def _e2_neighbor_runs(ds, p, variant_ids, base1, cm1, counter, progress, should_stop
                      ) -> tuple[list[NeighborRun], list[dict], list[str]]:
    """Checklist E2 (spec 6-4, 9-17): (runs, warm-up checks, skipped notes) over the
    pre-registered ``E2_NEIGHBORS``; a neighbour the snapshot warm-up cannot cover is skipped."""
    neighbor_runs: list[NeighborRun] = []
    checks: list[dict] = []
    skipped: list[str] = []
    for nid, f, v, _vids in E2_NEIGHBORS:
        wanted = [vid for n2, _, _, vid in planned_e2_runs(variant_ids) if n2 == nid and vid in base1]
        chk = warmup_check(ds, p, f, v)
        chk["id"] = nid
        checks.append(chk)
        if not wanted:
            continue
        if not chk["ok"]:
            skipped.append(f"{nid} ({f}={v}): snapshot warm-up has {chk['available_sessions']} sessions, "
                           f"{chk['needed_sessions']} needed; not run (the window is never shortened) -- report to the CIO")
            continue
        pn = neighbor_params(p, f, v)
        featsn = compute_features(ds, pn)
        for vid in wanted:
            _stop(should_stop)
            _say(progress, tr(f"E2: {vid} {f}={v} ({nid})...", f"E2: {vid} with {f}={v} ({nid})..."))
            res = _run_variant(ds, featsn, pn, vid, cm1, {}, free_field=f)
            counter.add(GROUP_E2)
            neighbor_runs.append(NeighborRun(
                nid, f, v, vid, stats_from_metrics(summarize_run(res)),
                period_stats(res.nav, res.dates, res.rf_period), float(base1[vid].metrics["sharpe"])))
    return neighbor_runs, checks, skipped


def _e4_top_trade_rows(runnable, base1, seed_krw) -> list[TopTradeRow]:
    """Checklist E4 (spec 6-4): top-trade share of each variant's 1x base run."""
    top_rows: list[TopTradeRow] = []
    for vid in runnable:
        s = base1[vid]
        yrs = max((s.dates[-1] - s.dates[0]).days / 365.0, 1e-9) if len(s.dates) > 1 else 0.0
        top_rows.append(top_trade_stats(vid, s.trades, seed_krw, yrs))
    return top_rows


def _a2_contamination_runs(ds, p, flow_lead, runnable, base1, cm1, counter, progress, should_stop
                           ) -> list[ContaminationRow]:
    """Checklist A2 (validation only, spec 9-17): flow variants rerun with flows `flow_lead`
    sessions early, next to the clean 1x base run; nothing when the dataset has no flows."""
    contam: list[ContaminationRow] = []
    featsc = compute_features(ds, p, flow_lead=flow_lead)
    if featsc.has_flows:
        for vid in runnable:
            if not get_variant(vid).needs_flows:
                continue
            _stop(should_stop)
            _say(progress, tr(f"A2: {vid} 수급 {flow_lead}세션 앞당김 (검증 전용)...", f"A2: {vid} with flows {flow_lead} session early (validation only)..."))
            res = _run_variant(ds, featsc, p, vid, cm1, {})
            counter.add(GROUP_A2)
            contam.append(ContaminationRow(vid, stats_from_metrics(base1[vid].metrics),
                                           stats_from_metrics(summarize_run(res))))
    return contam


def _no_interest_runs(ds, p, runnable, base1, cm1, counter, progress, should_stop) -> list[NoInterestRow]:
    """Spec 10-4 (reference only, never a verdict): each variant at 1x with the cash earning
    nothing, next to its 1x base run. The metrics still deduct the risk-free rate."""
    rows: list[NoInterestRow] = []
    feats = compute_features(ds, p)
    cache: dict = {}
    for vid in runnable:
        _stop(should_stop)
        _say(progress, tr(f"현금 이자 0 (참고): {vid}...", f"Cash interest 0 (reference): {vid}..."))
        res = _run_variant(ds, feats, p, vid, cm1, cache, cash_interest=False)
        counter.add(GROUP_NO_INTEREST)
        m = summarize_run(res)
        rows.append(NoInterestRow(vid, stats_from_metrics(base1[vid].metrics), stats_from_metrics(m),
                                  float(base1[vid].metrics.get("avg_cash_weight", float("nan")))))
    return rows


def run_robustness(ds, req: RobustnessRequest, progress=None, should_stop=None) -> RobustnessResult:
    r = req.research
    p = r.params
    cm1 = r.cost_model.scaled(1.0)
    counter = RunCounter(prior=int(req.prior_trials))

    research = run_research(ds, r, progress=progress, should_stop=should_stop)
    base1 = {x.id: x for x in research.runs_at(1.0, "strategy")}
    counter.add(GROUP_BASE, sum(1 for x in research.runs if x.kind == "strategy"))
    runnable = [vid for vid in r.variant_ids if vid in base1]       # flow variants drop out without flows

    weekday_runs: list[WeekdayRun] = []
    if req.run_e1:
        weekday_runs = _e1_weekday_runs(ds, p, runnable, base1, cm1, counter, progress, should_stop)

    neighbor_runs: list[NeighborRun] = []
    checks: list[dict] = []
    skipped: list[str] = []
    if req.run_e2:
        neighbor_runs, checks, skipped = _e2_neighbor_runs(ds, p, r.variant_ids, base1, cm1, counter,
                                                           progress, should_stop)

    top_rows = _e4_top_trade_rows(runnable, base1, p.seed_krw)

    contam: list[ContaminationRow] = []
    if req.flow_contamination:
        contam = _a2_contamination_runs(ds, p, req.flow_contamination, runnable, base1, cm1, counter,
                                        progress, should_stop)
    no_interest: list[NoInterestRow] = []
    if req.cash_interest_reference:
        no_interest = _no_interest_runs(ds, p, runnable, base1, cm1, counter, progress, should_stop)
    return RobustnessResult(req, research, weekday_runs, neighbor_runs, checks, skipped, top_rows, contam,
                            counter, ds.dates[-1] if ds.dates else None, no_interest)


# ---------------------------------------------------------------------------
# Report (checklist form: header, conclusion, warnings, A-H table, performance, requests)
# ---------------------------------------------------------------------------
def _stat_cells(s: PerfStats | None) -> list[str]:
    return ["-", "-", "-"] if s is None else [_num(s.sharpe), _pct(s.cagr), _pct(s.mdd)]


def _won(x: float | None) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x:,.0f}"


def _checklist_rows(res: RobustnessResult) -> list[list[str]]:
    outside = "이 리포트 범위 밖 — Validation 직접 검사"
    last = res.data_last_date
    d4 = (f"데이터 마지막 날짜 {last} {'≤' if last is not None and last <= DEV_END else '>'} {DEV_END}; "
          "구간별 표는 '성과표' 참고") if last else "데이터 없음"
    e2 = res.e2_verdicts()
    e2_txt = (", ".join(f"{k}: {'통과' if v == 'pass' else '실패'}" for k, v in e2.items())
              if e2 else ("실행 없음(웜업 부족, 상세 참고)" if res.e2_skipped else "실행 없음"))
    cm = res.research.request.cost_multipliers
    return [
        ["A1", "t+1 시가 체결", outside + " (회귀 테스트 `test_lookahead.py`)"],
        ["A2", "수급 t일 마감 후 사용", ("오염 비교 표 제공 (아래 'A2 참고', 결론에 사용하지 않음)"
                                      if res.contamination else "오염 옵션 꺼짐(기본) — 원장 확인은 Validation")],
        ["A3-A6", "지표 절단·백분위·수정주가·체결 불가", outside],
        ["B1-B3", "유니버스·생존편향", "머리말·경고 참고 — " + outside],
        ["C1-C2, C4-C5", "세율·호가단위·연초 리셋·상한", outside],
        ["C3", "비용 0 / 1× / 2×", "비용 배수 " + " / ".join(f"{m:g}×" for m in cm) + " 실행 — 성과표 참고"],
        ["D1-D3", "BM1~BM4 비교, TR, 지표", "성과표(BM 포함, 동일 엔진; 알파는 'Alpha' 열과 개발 구간별 알파 표) 참고"],
        ["D4", "개발 구간별 결과, 종료일 ≤ 2024-12-31", d4],
        ["D5", "홀드아웃 1회", "홀드아웃 데이터 미사용(가드 유지)"],
        ["E1", "점검 요일 5개", "요일 5개 실행, 최악 요일 CAGR/MDD — E1 절" if res.weekday_runs else "실행 안 함"],
        ["E2", "파라미터 주변값 (Sharpe 변화 > 0.5 실패)", e2_txt + " — E2 절"],
        ["E3", "비용 2× 우위 유지", "E3 표 참고" if any(abs(m - 2.0) < 1e-9 for m in cm) else "2× 미실행"],
        ["E4", "상위 5% 거래 기여도", "E4 절"],
        ["E5", "시도 횟수·다중검정", f"이 리포트 전략 백테스트 {res.counter.this_report}회 — E5 절"],
        ["E6", "결과 후 파라미터 변경 여부", outside + " (이슈·커밋 이력 대조)"],
        ["F", "홀드아웃", "해당 없음(개발 구간 실행)"],
        ["G1-G4", "코드 품질", outside + " (pytest·ruff)"],
        ["H", "Sharpe ≥ 1.5, MDD ≤ 15%", "수치만 제공(성과표). 달성 여부 판정은 Validation"],
    ]


def _e1_section(res: RobustnessResult) -> list[str]:
    out = ["", "### E1 점검 요일 (월~금, 비용 1×, 개발 구간 전체)", ""]
    summ = res.weekday_summary()
    if not summ:
        return out + ["실행하지 않았다."]
    out.append(_table(
        ["Variant"] + [f"{n} CAGR / MDD" for n in WEEKDAY_NAMES]
        + ["최악 CAGR 요일", "최악 MDD 요일", "CAGR 범위"],
        [[s["variant"]] + [f"{_pct(r.whole.cagr)} / {_pct(r.whole.mdd)}" for r in s["rows"]]
         + [f"{WEEKDAY_NAMES[s['worst_cagr'].weekday]} {_pct(s['worst_cagr'].whole.cagr)}",
            f"{WEEKDAY_NAMES[s['worst_mdd'].weekday]} {_pct(s['worst_mdd'].whole.mdd)}",
            _pct(s["cagr_spread"])] for s in summ]))
    out += ["", "요일별 Sharpe (참고):", "",
            _table(["Variant"] + list(WEEKDAY_NAMES),
                   [[s["variant"]] + [_num(r.whole.sharpe) for r in s["rows"]] for s in summ])]
    out += ["", f"각 변형 요일 수: {sorted({len(s['rows']) for s in summ})} (5개 모두 실행). "
                "점검 요일은 L1 국면·B 리밸런싱·C 정책·주간 비중 트림의 점검일만 바꾼다."]
    return out


def _registered_base(field_name: str, vids) -> object:
    """The base value a neighbour row moves away from: the variants' own value of the field
    (C1S-C3S pin score_ma_long at 50 while the StrategyParams() default is 60 since v07)."""
    vals = {getattr(get_variant(v).apply(StrategyParams()), field_name) for v in vids}
    return vals.pop() if len(vals) == 1 else "/".join(str(x) for x in sorted(vals))


def _e2_section(res: RobustnessResult) -> list[str]:
    out = ["", f"### E2 파라미터 주변값 (사전 등록, NEX-51 승인; 판정: |ΔSharpe| > {E2_SHARPE_JUMP:g} 이면 실패)", ""]
    out.append(_table(["ID", "필드", "기본 → 주변값", "적용 변형"],
                      [[nid, f, f"{_registered_base(f, vids)} → {v}", ", ".join(vids)]
                       for nid, f, v, vids in E2_NEIGHBORS]))
    out += ["", "웜업 확인 (첫 평가 세션에서 지표가 유효한지):", "",
            _table(["ID", "필드=값", "필요 세션", "보유 세션", "결과"],
                   [[c["id"], f"{c['field']}={c['value']}", str(c["needed_sessions"]), str(c["available_sessions"]),
                     "충분" if c["ok"] else "부족 — 실행 안 함, CIO 보고"] for c in res.e2_checks])]
    if res.e2_skipped:
        out += [""] + [f"- ⚠ {s}" for s in res.e2_skipped]
    if not res.neighbor_runs:
        return out + ["", "주변값 실행 결과가 없다."]
    base_by_v = {r.variant: r.base_sharpe for r in res.neighbor_runs}
    labels = [lab for lab, _, _ in PERIODS]
    rows = []
    for r in res.neighbor_runs:
        row = [r.variant, r.nid, f"{r.field}={r.value}", _num(base_by_v[r.variant]), _num(r.whole.sharpe),
               f"{r.delta_sharpe:+.2f}", "통과" if r.passed else "실패"]
        for lab in labels:
            row += _stat_cells(r.periods.get(lab))
        rows.append(row)
    heads = ["Variant", "ID", "주변값", "Sharpe(기준)", "Sharpe(주변값)", "ΔSharpe", "판정"]
    for lab in labels:
        heads += [f"{lab} Sharpe", f"{lab} CAGR", f"{lab} MDD"]
    out += ["", _table(heads, rows), "",
            "구간별 Sharpe·CAGR·MDD는 참고 열이며 판정에 쓰지 않았다. 기준 = 6-2 본 매트릭스의 비용 1× 행."]
    verdicts = res.e2_verdicts()
    out += ["", "변형별 E2 판정 (하나라도 실패면 실패): " + ", ".join(
        f"{k} {'통과' if v == 'pass' else '실패'}" for k, v in verdicts.items())]
    failed = [k for k, v in verdicts.items() if v == "fail"]
    out.append("**E2 실패 변형: " + (", ".join(failed) if failed else "없음") + "**")
    return out


def _e4_section(res: RobustnessResult) -> list[str]:
    out = ["", f"### E4 수익 집중도 (상위 {E4_TOP_SHARE:.0%} 거래, 비용 1×, 순손익 기준)", ""]
    if not res.top_trades:
        return out + ["거래가 없다."]
    out.append(_table(
        ["Variant", "거래 수", "상위 n", "총손익(원)", "상위 손익(원)", "상위 기여도", "상위 제외 손익(원)",
         "연환산 수익률(시드 대비)", "상위 제외 시"],
        [[t.variant, str(t.n_trades), str(t.k), _won(t.total_pnl), _won(t.top_pnl),
          _pct(t.share, 0) if t.share is not None else "- (총손익 ≤ 0)", _won(t.ex_pnl),
          _pct(t.annual_pct), _pct(t.ex_annual_pct)] for t in res.top_trades]))
    out += ["", "거래 = 연말·주간 비중 트림을 제외한 청산 단위(`trade_stats`와 같은 정의). 연환산 수익률은 총손익 ÷ 시드 ÷ 기간(년)의 "
                "단순 근사(복리·연초 리셋 미반영)이며 NAV 기반 CAGR과 다르다. 상위 거래를 뺀 NAV는 재계산하지 않았다."]
    return out


def _e5_section(res: RobustnessResult) -> list[str]:
    c = res.counter
    out = ["", "### E5 시도 횟수와 다중검정 위험", ""]
    out.append(_table(["구분", "전략 백테스트 수"], [[g, str(n)] for g, n in c.groups.items()]
                      + [["이 리포트 합계", str(c.this_report)], ["이전 리포트 누적(입력)", str(c.prior)],
                         ["누적 합계", str(c.total)]]))
    out += ["", "벤치마크 실행과 이벤트 스터디는 세지 않았다. 시도한 변형·요일·주변값이 많을수록 우연히 좋은 결과가 하나쯤 "
                "나올 확률이 커지므로, 이 표의 어떤 단일 행도 시도 횟수에 대한 보정 없이 우위의 증거로 읽지 않는다."]
    return out


def _e3_section(res: RobustnessResult) -> list[str]:
    r1 = {x.id: x for x in res.research.runs_at(1.0, "strategy")}
    r2 = {x.id: x for x in res.research.runs_at(2.0, "strategy")}
    if not r2:
        return []
    rows = []
    for vid, a in r1.items():
        b = r2.get(vid)
        if b is None:
            continue
        rows.append([vid, _pct(a.metrics.get("bm1_excess_cagr")), "yes" if a.metrics.get("bm1_beats_risk_adjusted") else "no",
                     _pct(b.metrics.get("bm1_excess_cagr")), "yes" if b.metrics.get("bm1_beats_risk_adjusted") else "no",
                     "yes" if b.metrics.get("bm3_beats_risk_adjusted") else "no"])
    return ["", "### E3 비용 2× 에서의 BM 대비 우위", "",
            _table(["Variant", "1× vs BM1 CAGR", "1× Sharpe&Calmar > BM1", "2× vs BM1 CAGR",
                    "2× Sharpe&Calmar > BM1", "2× Sharpe&Calmar > BM3"], rows)]


def _a2_section(res: RobustnessResult) -> list[str]:
    if not res.request.flow_contamination:
        return []
    out = ["", f"### A2 참고: 수급 {res.request.flow_contamination}일 앞당김(오염) 비교 — 검증 전용, 결론에 쓰지 않음", ""]
    if not res.contamination:
        return out + ["수급 데이터가 없어 실행하지 않았다."]
    out.append(_table(
        ["Variant", "현행 Sharpe", "오염 Sharpe", "현행 CAGR", "오염 CAGR", "현행 MDD", "오염 MDD", "현행 거래", "오염 거래"],
        [[c.variant, _num(c.clean.sharpe), _num(c.contaminated.sharpe), _pct(c.clean.cagr), _pct(c.contaminated.cagr),
          _pct(c.clean.mdd), _pct(c.contaminated.mdd), str(c.clean.n_trades), str(c.contaminated.n_trades)]
         for c in res.contamination]))
    out += ["", "오염 버전은 t행에 t+1일 수급을 넣은 look-ahead이며 실전 신호가 아니다. 체크리스트 A2 (2)의 '차이 보고'용이다."]
    return out


def _period_table(res: RobustnessResult) -> list[str]:
    runs = res.research.runs_at(1.0)
    if not runs:
        return []
    labels = [lab for lab, _, _ in PERIODS]
    heads = ["ID"]
    for lab in labels:
        heads += [f"{lab} Sharpe", f"{lab} CAGR", f"{lab} MDD"]
    rows = []
    for x in runs:
        ps = period_stats(x.nav, x.dates, x.rf_period)
        row = [x.id]
        for lab in labels:
            row += _stat_cells(ps.get(lab))
        rows.append(row)
    return ["", "### 개발 구간별 (비용 1×; 전략과 BM1~BM4)", "", _table(heads, rows)]


def _period_alpha_table(res: RobustnessResult) -> list[str]:
    """Checklist D3: annualised alpha / beta vs BM1 inside each development period, per cost multiplier.
    Same regression as the 6-4 scorecard (`beta_alpha`), applied to the period's daily returns."""
    labels = [lab for lab, _, _ in PERIODS]
    heads = ["ID", "Cost"]
    for lab in labels:
        heads += [f"{lab} Alpha", f"{lab} Beta"]
    rows = []
    for mult in res.request.research.cost_multipliers:
        runs = res.research.runs_at(mult)
        bm1 = next((x for x in runs if x.id == "BM1"), None)
        if bm1 is None:
            continue
        for x in runs:
            if x.kind != "strategy":
                continue
            row = [x.id, f"{mult:g}x"]
            for _lab, y0, y1 in PERIODS:
                years = np.array([d.year for d in x.dates])
                idx = np.flatnonzero((years >= y0) & (years <= y1))
                if idx.size < 4 or len(bm1.nav) != len(x.nav):
                    row += ["-", "-"]
                    continue
                rf = np.zeros(len(x.nav)) if x.rf_period is None else np.asarray(x.rf_period, dtype=float)
                i0 = int(idx[0])
                xn, bn = np.asarray(x.nav, dtype=float), np.asarray(bm1.nav, dtype=float)
                # Each period's first return is measured from the previous period's
                # last NAV (the seed for the first period), as in period_stats.
                beta, alpha = beta_alpha(nav_returns(xn[idx], xn[i0 - 1] if i0 > 0 else NAV_BASE),
                                         nav_returns(bn[idx], bn[i0 - 1] if i0 > 0 else NAV_BASE),
                                         session_rf(rf[idx], first_session=i0 == 0))
                row += [_pct(alpha), _num(beta)]
            rows.append(row)
    if not rows:
        return []
    return ["", "### 개발 구간별 BM1 대비 알파·베타 (체크리스트 D3; 연율화 일별 초과수익 회귀)", "", _table(heads, rows)]


def _no_interest_section(res: RobustnessResult) -> list[str]:
    """Spec 10-4: the cash-interest-0 reference table (no verdict)."""
    if not res.no_interest:
        return []
    rows = [[r.variant, _pct(r.avg_cash_weight, 0), _pct(r.with_interest.cagr), _pct(r.no_interest.cagr),
             f"{(r.no_interest.cagr - r.with_interest.cagr) * 100:+.1f}%p", _num(r.with_interest.sharpe),
             _num(r.no_interest.sharpe), _pct(r.with_interest.mdd), _pct(r.no_interest.mdd)]
            for r in res.no_interest]
    return ["", "### 현금 이자 0 (참고, 스펙 10-4)", "",
            "같은 데이터·비용 1×에서 현금 이자 적립만 0으로 바꾼 재실행이다. 지표의 무위험수익률 차감(Sharpe)은 그대로이며 판정에 쓰지 않는다.", "",
            _table(["변형", "평균 현금", "CAGR (이자 3%)", "CAGR (이자 0)", "차이", "Sharpe (이자)", "Sharpe (0)",
                    "MDD (이자)", "MDD (0)"], rows)]


def render_robustness_markdown(res: RobustnessResult, header: list[str] | None = None) -> str:
    from strategy.trend_following.config import SPEC_VERSION
    req = res.request
    rr = res.research
    out: list[str] = [f"# [D] NEX-52 강건성 리포트 — 스펙 {SPEC_VERSION} (체크리스트 양식)", ""]
    if header:
        out += list(header) + [""]
    if not header:      # the run.py provenance block already carries commit, spec version and data window
        out += [f"- 스펙 버전: {SPEC_VERSION}",
                f"- 데이터 기간: {rr.dataset_info.get('eval_start', '')} → {rr.dataset_info.get('eval_end', '')} "
                f"(개발 구간 가드 {DEV_END})"]
    out += [
        f"- 실행 일시: {rr.created_at:%Y-%m-%d %H:%M}",
        f"- 변형: {', '.join(req.research.variant_ids)}; 비용 배수 {', '.join(f'{m:g}x' for m in req.research.cost_multipliers)}"
        "; E1·E2·E4는 비용 1×만",
        "",
        "## 결론",
        "",
        "**판정 불가 — Dev 실행기 산출물이며 통과/보완 요청은 Validation(V5)이 정한다.** 기계 집계는 아래 E2 절의 "
        "변형별 통과/실패와 E5의 시도 횟수뿐이다. 이 도구의 결과로 기본값이나 E2 세트를 바꾸지 않는다.",
    ]
    verdicts = res.e2_verdicts()
    out.append("")
    if verdicts:
        failed = [k for k, v in verdicts.items() if v == "fail"]
        out.append(f"- E2 실패 변형: {', '.join(failed) if failed else '없음'} (기준 |ΔSharpe| > {E2_SHARPE_JUMP:g})")
    if res.e2_skipped:
        out.append("- ⚠ E2 일부 미실행(웜업 부족) — E2 절 참고")
    out += ["", "## 편향·한계 경고", ""]
    warn = list(rr.warnings) + [
        "강건성 결과(요일·주변값·E4)는 같은 데이터에서 반복 실행한 것이라 서로 독립이 아니다.",
        "실데이터 성과의 해석은 이 리포트의 몫이 아니다.",
    ]
    out += [f"- {w}" for w in warn]
    out += ["", "## 체크리스트 결과표 (A~H)", "",
            "상태 칸에는 통과/실패를 적지 않는다(E2 변형별 기계 판정 제외). 'Validation 직접 검사'는 이 도구가 값을 내지 않는 항목이다.",
            "", _table(["#", "항목", "이 리포트가 제공하는 것"], _checklist_rows(res))]
    out += _e1_section(res) if req.run_e1 else []
    out += _e2_section(res) if req.run_e2 else []
    out += _e3_section(res)
    out += _e4_section(res)
    out += _e5_section(res)
    out += _a2_section(res)
    out += _no_interest_section(res)

    out += ["", "## 성과표"]
    for mult in req.research.cost_multipliers:
        rows = rr.runs_at(mult)
        if not rows:
            continue
        out += ["", f"### {mult:g}x costs" + (" (base case)" if abs(mult - 1.0) < 1e-9 else ""), "",
                _table([h for h, _ in SUMMARY_COLUMNS], [[fn(x) for _, fn in SUMMARY_COLUMNS] for x in rows])]
    out += _period_table(res)
    out += _period_alpha_table(res)
    flows = cash_flow_table(rr.runs_at(1.0), korean=True)
    if flows:
        out += ["", "### 연도별 인출·입금 (비용 1×, 만 원; 스펙 9-2·9-27)", "",
                f"매년 {req.research.params.seed_krw / 1e4:,.0f}만 원으로 시작해 연말 마지막 거래일 종가로 초과분 인출(+, 연말 트림 "
                "매도 비용 반영)·부족분 입금(−). 마지막 해는 리셋하지 않아 '평가 종료 차액 = 마지막 평가액 − 시드'. "
                "손익 = 인출 합 − 입금 합 + 평가 종료 차액. 잔여 = 마지막 리셋 직후 평가액 − 시드(정수 주). BM4 제외.",
                ""] + flows
    out += ["", "## 보완 요청 목록", "", "(Validation이 항목 번호·재현 방법·기대 결과로 기입한다.)", ""]
    return "\n".join(out)
