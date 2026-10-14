"""strategy/trend_following/factor_ic.py — 6-1-2 factor IC and quintile analysis
(trend_following.md v06, C-series pre-validation).

Answers, before the four scored 2-5 indicators are combined, whether each one carries
predictive power and whether its sign matches the hypothesis:

  1. correlation matrix of the four scored indicators plus the reference column R10 (before / after the ATR
     normalisation), average cross-sectional rank correlation, |rho| > 0.8 flagged
  2. daily Rank IC against the forward 5 / 10 / 20-session return, measured from
     the NEXT open (t+1 open -> close of t+h, the event study's convention);
     mean, std, IR, share of IC > 0
  3. the same per development sub-period (config.PERIODS only)
  4. total-score quintiles: forward return by quintile, monotonicity, Q5 - Q1
  5. the 6-1-2 verdict table applied mechanically on the IC_VERDICT_HORIZON (20)
     mean IC of the two periods (R10 is a reference column: no verdict, spec v06 / 9-15)

Universe, liquidity filter and indicator values come from ``scoring.compute_scores``
(nothing is re-implemented here). The analysis never looks past
``config.DEV_END``: rows after it are dropped from the book before anything is
computed, whatever the dataset contains. The verdict stops at "검토 대상" -- no
code here changes a sign, a gate or a weight (spec 6-1-2, 6-5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from strategy.trend_following.config import (
    DEV_END, IC_VERDICT_HORIZON, PERIODS, SPEC_VERSION, StrategyParams,
)
from strategy.trend_following.dataset import PriceBook
from strategy.trend_following.scoring import compute_scores

# Spec 6-1-2 order. Keys are ScoreBook attribute / pct names. R10 is a reference column since v06: it stays in the
# correlation matrix and the IC tables but is not in JUDGED_FACTORS / HYPOTHESES (it left the score, 9-15).
FACTORS: tuple[str, ...] = ("r3", "r10", "range52", "ma20_div", "ma50_div")
JUDGED_FACTORS: tuple[str, ...] = tuple(f for f in FACTORS if f != "r10")
FACTOR_LABELS = {"r3": "R3", "r10": "R10", "range52": "Range52", "ma20_div": "MA20Div", "ma50_div": "MA50Div",
                 "total": "Total"}
IC_HORIZONS: tuple[int, ...] = (5, 10, 20)
CORR_FLAG = 0.8
MIN_NAMES = 10             # a date needs this many names for a rank correlation
MIN_QUINTILE_NAMES = 25    # ... and this many for quintiles (5 per bucket)
ALL_LABEL = "전체 2021~2024"

# 6-1-2 hypothesis table: sign of the expected IC and the action named when the
# IC is opposite in BOTH development periods. MA20Div's hypothesis is "−" (overheat).
HYPOTHESES: dict[str, tuple[int, str]] = {
    "ma50_div": (+1, "게이트·점수에서 제외 검토"),
    "range52": (+1, "`high52_prox`로 교체 검토"),
    "ma20_div": (-1, "과열 게이트 제거"),
    "r3": (-1, "부호 반전 (눌림 가설 기각 → 모멘텀 타이밍으로 전환)"),
}
STATUS_KEEP, STATUS_REVIEW, STATUS_UNSTABLE, STATUS_NA = "유지", "검토 대상", "불안정", "판정 불가"


# ---------------------------------------------------------------------------
# Pure numerics
# ---------------------------------------------------------------------------
def cut_book(book: PriceBook, end: date = DEV_END) -> tuple[PriceBook, int]:
    """The book restricted to sessions on or before `end` and the number of
    sessions dropped. The development window guard of this module."""
    keep = sum(1 for d in book.dates if d <= end)
    if keep == book.T:
        return book, 0
    return PriceBook(
        dates=list(book.dates[:keep]), tickers=list(book.tickers),
        open=book.open[:keep], high=book.high[:keep], low=book.low[:keep],
        close=book.close[:keep], volume=book.volume[:keep],
        markets=dict(book.markets), names=dict(book.names),
    ), book.T - keep


def forward_returns(book: PriceBook, horizon: int) -> np.ndarray:
    """Return a signal on session t would earn if filled at the next open:
    close[t + h] / open[t + 1] - 1 (6-1-2 "t+1 시가 기준"). NaN where either bar
    is missing or the window runs past the last session."""
    T, N = book.T, book.N
    out = np.full((T, N), np.nan)
    if horizon < 1 or T <= horizon:
        return out
    base = book.open[1:T - horizon + 1]
    end = book.close[horizon:]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = end / base - 1.0
    out[:T - horizon] = np.where(np.isfinite(base) & (base > 0) & np.isfinite(end), r, np.nan)
    return out


def _row_rank(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return pd.DataFrame(np.where(mask, values, np.nan)).rank(axis=1, method="average").to_numpy(dtype=float)


def row_rank_corr(x: np.ndarray, y: np.ndarray, mask: np.ndarray | None = None, min_n: int = MIN_NAMES) -> np.ndarray:
    """Spearman correlation of x and y across the columns of every row, over the
    columns where `mask` holds and both are finite. NaN for rows with fewer than
    `min_n` names or no variation."""
    m = np.isfinite(x) & np.isfinite(y)
    if mask is not None:
        m &= mask
    rx, ry = _row_rank(x, m), _row_rank(y, m)
    n = m.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cx = np.where(m, rx - np.where(m, rx, 0.0).sum(axis=1, keepdims=True) / n[:, None], 0.0)
        cy = np.where(m, ry - np.where(m, ry, 0.0).sum(axis=1, keepdims=True) / n[:, None], 0.0)
        sxy = (cx * cy).sum(axis=1)
        sxx = (cx * cx).sum(axis=1)
        syy = (cy * cy).sum(axis=1)
        rho = sxy / np.sqrt(sxx * syy)
    return np.where((n >= min_n) & (sxx > 0) & (syy > 0), rho, np.nan)


def quintile_returns(score: np.ndarray, fwd: np.ndarray, mask: np.ndarray, min_n: int = MIN_QUINTILE_NAMES) -> np.ndarray:
    """T x 5 equal-weight mean forward return of the score quintiles (column 0 =
    lowest score), per date; NaN rows when fewer than `min_n` names qualify."""
    m = mask & np.isfinite(score) & np.isfinite(fwd)
    pct = pd.DataFrame(np.where(m, score, np.nan)).rank(axis=1, pct=True, method="average").to_numpy(dtype=float)
    q = np.clip(np.ceil(pct * 5 - 1e-9), 1, 5)
    out = np.full((score.shape[0], 5), np.nan)
    ok = m.sum(axis=1) >= min_n
    for k in range(1, 6):
        sel = m & (q == k)
        cnt = sel.sum(axis=1)
        tot = np.where(sel, fwd, 0.0).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[:, k - 1] = np.where(ok & (cnt > 0), tot / cnt, np.nan)
    return out


@dataclass(frozen=True)
class ICStats:
    n: int
    mean: float
    std: float
    ir: float
    pos_ratio: float


def ic_stats(series: np.ndarray) -> ICStats:
    s = series[np.isfinite(series)]
    if s.size == 0:
        return ICStats(0, float("nan"), float("nan"), float("nan"), float("nan"))
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if s.size > 1 else float("nan")
    ir = mean / std if np.isfinite(std) and std > 0 else float("nan")
    return ICStats(int(s.size), mean, std, ir, float(np.mean(s > 0)))


@dataclass(frozen=True)
class QuintileStats:
    n_days: int
    means: tuple[float, ...]       # Q1..Q5 average forward return
    spread: float                  # Q5 - Q1
    monotone: bool                 # Q1 < Q2 < Q3 < Q4 < Q5


def quintile_stats(daily: np.ndarray) -> QuintileStats:
    good = np.isfinite(daily).all(axis=1) if daily.size else np.zeros(0, dtype=bool)
    if not good.any():
        return QuintileStats(0, (float("nan"),) * 5, float("nan"), False)
    means = daily[good].mean(axis=0)
    return QuintileStats(int(good.sum()), tuple(float(v) for v in means), float(means[4] - means[0]),
                         bool(np.all(np.diff(means) > 0)))


# ---------------------------------------------------------------------------
# Verdict (6-1-2 table, applied mechanically)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    factor: str
    hypothesis_sign: int
    period_ic: dict[str, float]    # period label -> mean IC at the verdict horizon
    status: str
    action: str = ""


def judge(factor: str, period_ic: dict[str, float]) -> Verdict:
    """Both periods opposite to the hypothesis and in the same direction ->
    "검토 대상" with the table's action; the two signs differ -> "불안정"; both as
    hypothesised -> "유지"; a missing or exactly zero mean -> "판정 불가"."""
    sign, action = HYPOTHESES[factor]
    vals = list(period_ic.values())
    if len(vals) != len(PERIODS) or any((not np.isfinite(v)) or v == 0 for v in vals):
        return Verdict(factor, sign, dict(period_ic), STATUS_NA)
    signs = {1 if v > 0 else -1 for v in vals}
    if len(signs) > 1:
        return Verdict(factor, sign, dict(period_ic), STATUS_UNSTABLE)
    if signs == {sign}:
        return Verdict(factor, sign, dict(period_ic), STATUS_KEEP)
    return Verdict(factor, sign, dict(period_ic), STATUS_REVIEW, action)


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------
@dataclass
class FactorICResult:
    spec_version: str
    params: StrategyParams
    horizons: tuple[int, ...]
    corr_before: np.ndarray                      # 5 x 5, FACTORS order, raw values
    corr_after: np.ndarray                       # 5 x 5, values the score uses (ATR-normalised)
    corr_days: int
    ic: dict[tuple[str, int, str], ICStats]      # (period, horizon, factor incl. "total")
    quintiles: dict[tuple[str, int], QuintileStats]         # liquid universe
    quintiles_gated: dict[tuple[str, int], QuintileStats]   # names passing the 2-5 gate
    verdicts: list[Verdict]
    info: dict = field(default_factory=dict)

    @property
    def period_labels(self) -> list[str]:
        return [ALL_LABEL] + [p[0] for p in PERIODS]

    def flagged_pairs(self, threshold: float = CORR_FLAG) -> list[tuple[str, str, str, float]]:
        out = []
        for tag, mat in (("정규화 전", self.corr_before), ("정규화 후", self.corr_after)):
            for a in range(len(FACTORS)):
                for b in range(a + 1, len(FACTORS)):
                    if np.isfinite(mat[a, b]) and abs(mat[a, b]) > threshold:
                        out.append((FACTORS[a], FACTORS[b], tag, float(mat[a, b])))
        return out

    def to_markdown(self, header: list[str] | None = None) -> str:
        return render_markdown(self, header)


def run_factor_ic(ds, params: StrategyParams | None = None, horizons: tuple[int, ...] = IC_HORIZONS) -> FactorICResult:
    """6-1-2 on a Dataset. Sessions before ``ds.start_idx`` are warm-up and after
    DEV_END are dropped; a signal on session t is only scored when its forward
    window ends inside the development window."""
    p = params or StrategyParams()
    horizons = tuple(sorted(set(horizons) | {IC_VERDICT_HORIZON}))
    book, dropped = cut_book(ds.stocks)
    sb = compute_scores(book, p)
    T = book.T
    t0 = min(ds.start_idx, T)
    years = np.array([d.year for d in book.dates])
    in_eval = np.arange(T) >= t0

    rows = {ALL_LABEL: in_eval}
    for label, y0, y1 in PERIODS:
        rows[label] = in_eval & (years >= y0) & (years <= y1)

    # (1) correlation matrices on a common sample: liquid names with every value finite.
    raw = {"r3": sb.r3, "r10": sb.r10, "range52": sb.range52, "ma20_div": sb.ma20_div, "ma50_div": sb.ma50_div}
    common = sb.liquid.copy()
    for f in FACTORS:
        common &= np.isfinite(raw[f]) & np.isfinite(sb.pct[f])
    corr_b = np.eye(len(FACTORS))
    corr_a = np.eye(len(FACTORS))
    corr_days = 0
    for a in range(len(FACTORS)):
        for b in range(a + 1, len(FACTORS)):
            fa, fb = FACTORS[a], FACTORS[b]
            for mat, src in ((corr_b, raw), (corr_a, sb.pct)):
                daily = row_rank_corr(src[fa], src[fb], common)[in_eval]
                good = daily[np.isfinite(daily)]
                mat[a, b] = mat[b, a] = float(good.mean()) if good.size else float("nan")
                corr_days = max(corr_days, int(good.size))

    # (2)-(4) IC per factor, period and horizon; quintiles of the total score.
    values = {f: sb.pct[f] for f in FACTORS}
    values["total"] = sb.total
    ic: dict[tuple[str, int, str], ICStats] = {}
    quint: dict[tuple[str, int], QuintileStats] = {}
    quint_gated: dict[tuple[str, int], QuintileStats] = {}
    for h in horizons:
        fwd = forward_returns(book, h)
        daily_ic = {f: row_rank_corr(v, fwd, sb.liquid) for f, v in values.items()}
        q_all = quintile_returns(sb.total, fwd, sb.liquid)
        q_gate = quintile_returns(sb.total, fwd, sb.gate)
        for label, sel in rows.items():
            for f, series in daily_ic.items():
                ic[(label, h, f)] = ic_stats(series[sel])
            quint[(label, h)] = quintile_stats(q_all[sel])
            quint_gated[(label, h)] = quintile_stats(q_gate[sel])

    verdicts = [judge(f, {label: ic[(label, IC_VERDICT_HORIZON, f)].mean for label, _, _ in PERIODS})
                for f in JUDGED_FACTORS]
    info = {
        "eval_start": book.dates[t0].isoformat() if t0 < T else "",
        "last_date": book.dates[-1].isoformat() if T else "",
        "n_names": book.N, "n_sessions": T - t0,
        "dropped_after_dev_end": dropped,
        "mean_liquid_names": float(sb.liquid[t0:].sum(axis=1).mean()) if T > t0 else 0.0,
    }
    return FactorICResult(SPEC_VERSION, p, horizons, corr_b, corr_a, corr_days, ic, quint, quint_gated, verdicts, info)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _f(x, digits=3) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x:.{digits}f}"


def _pct(x, digits=2) -> str:
    return "-" if x is None or not np.isfinite(x) else f"{x * 100:.{digits}f}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return (["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
            + ["| " + " | ".join(r) + " |" for r in rows])


def render_markdown(result: FactorICResult, header: list[str] | None = None) -> str:
    """`header` lines (the provenance block from run.report_header) go right under the title."""
    out: list[str] = [f"# Trend Following 팩터 IC·5분위 분석 — spec {result.spec_version} (6-1-2)", ""]
    if header:
        out += list(header) + [""]
    info = result.info
    out += [
        f"- 분석 구간: {info.get('eval_start', '')} ~ {info.get('last_date', '')} "
        f"(세션 {info.get('n_sessions', 0)}개, 종목 {info.get('n_names', 0)}개, 일평균 유동성 충족 종목 "
        f"{info.get('mean_liquid_names', 0.0):.0f}개). 개발 구간 가드 {DEV_END} 이후 세션 "
        f"{info.get('dropped_after_dev_end', 0)}개는 계산 전에 제외했다.",
        f"- 수익률: 신호일 t의 t+1 시가 대비 t+h 종가 수익률, h = {', '.join(map(str, result.horizons))}. "
        f"**판정 horizon은 {IC_VERDICT_HORIZON}일로 사전 고정**, 나머지는 참고용.",
        "- IC는 매일 횡단면 순위 상관(유동성 충족 종목)이며 일별 IC는 겹치는 보유 구간 때문에 자기상관이 있어 "
        "IR·비율은 독립 표본 기준 유의성이 아니다.",
        "- 이 리포트는 '검토 대상' 표시까지만 한다. 부호·게이트·가중치 변경은 스펙 변경 경로(Research → 사용자)로만 가능하다.",
        "",
        "## 1. 지표 상관행렬 (일별 횡단면 순위 상관의 평균)",
        "",
        f"공통 표본: 유동성 충족이고 다섯 열(R10 참고 열 포함)이 모두 있는 종목, 일 {result.corr_days}개 평균. "
        f"|ρ| > {CORR_FLAG} 쌍은 합치거나 하나를 제외 검토. R10은 v06부터 점수에 쓰지 않는 참고 열이다(R10↔MA20Div 0.90으로 제외, 9-15).",
    ]
    labels = [FACTOR_LABELS[f] for f in FACTORS]
    for tag, mat in (("정규화 전 (원값)", result.corr_before), ("정규화 후 (점수에 쓰는 값)", result.corr_after)):
        out += ["", f"### {tag}", ""]
        out += _table([""] + labels, [[labels[a]] + [_f(mat[a, b], 2) for b in range(len(FACTORS))]
                                      for a in range(len(FACTORS))])
    flagged = result.flagged_pairs()
    out += ["", "**|ρ| > 0.8 쌍:** " + (", ".join(f"{FACTOR_LABELS[a]}↔{FACTOR_LABELS[b]} ({tag}, {_f(r, 2)})"
                                                 for a, b, tag, r in flagged) if flagged else "없음")]

    out += ["", "## 2-3. Rank IC (전체 및 구간별)", ""]
    for label in result.period_labels:
        out += [f"### {label}", ""]
        rows = []
        for f in (*FACTORS, "total"):
            for h in result.horizons:
                s = result.ic[(label, h, f)]
                tag = f"{h} (판정)" if h == IC_VERDICT_HORIZON else str(h)
                rows.append([FACTOR_LABELS[f] + (' (참고)' if f == 'r10' else ''), tag, _f(s.mean), _f(s.std), _f(s.ir, 2), _pct(s.pos_ratio, 1), str(s.n)])
        out += _table(["지표", "horizon(일)", "평균 IC", "표준편차", "IR", "IC>0 비율", "일수"], rows) + [""]

    out += ["## 4. 총점 5분위 (이후 수익률, t+1 시가 기준)", "",
            "Q1이 총점 최저, Q5가 최고. '유동성'은 유동성 충족 종목 전체, '게이트'는 2-5 게이트 통과 종목만 대상.", ""]
    for label in result.period_labels:
        out += [f"### {label}", ""]
        rows = []
        for tag, book in (("유동성", result.quintiles), ("게이트", result.quintiles_gated)):
            for h in result.horizons:
                q = book[(label, h)]
                rows.append([tag, str(h)] + [_pct(v) for v in q.means] + [_pct(q.spread), "예" if q.monotone else "아니오",
                                                                           str(q.n_days)])
        out += _table(["대상", "horizon(일)", "Q1", "Q2", "Q3", "Q4", "Q5", "Q5−Q1", "단조 증가", "일수"], rows) + [""]

    out += [f"## 5. 판정 표 (horizon {IC_VERDICT_HORIZON}일 평균 IC, 6-1-2 규칙 기계 적용)", ""]
    plabels = [p[0] for p in PERIODS]
    rows = [[FACTOR_LABELS[v.factor], "+" if v.hypothesis_sign > 0 else "−"]
            + [_f(v.period_ic.get(pl)) for pl in plabels] + [v.status, v.action or "-"] for v in result.verdicts]
    out += _table(["지표", "가설 부호"] + [f"IC {pl}" for pl in plabels] + ["판정", "검토 내용"], rows)
    out += ["",
            "- 유지: 두 구간 모두 가설 부호. 검토 대상: 두 구간 모두 가설과 반대 부호(검토 표시만, 코드는 바꾸지 않음).",
            "- 불안정: 두 구간의 부호가 다름 → 가설 유지, 부호 변경·제외·교체 없이 홀드아웃에서 1회 확인.",
            "- 판정 불가: 어느 구간의 IC가 없거나 정확히 0.",
            "- R10은 참고 열이라 판정하지 않는다(스펙 v06, 상관 규칙으로 점수에서 제외).", ""]
    return "\n".join(out)
