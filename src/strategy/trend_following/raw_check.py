"""strategy/trend_following/raw_check.py — A5 sample check: adjusted vs unadjusted prices (NEX-19).

The engine runs on split-adjusted daily prices (Naver chart API) but several rules depend on the
price *level*: the tick grid (4-1 (3)), whole-share sizing (2-4), the 20-day trading-value floor
(2-0) and the daily price limits (4-1 (3)). This module measures, per sample ticker, how much
those four rules would differ if the engine had seen the as-traded price
(``data.raw_prices.get_raw_daily_prices``). It only reports; the engine keeps using adjusted data
(checklist A5; decision 2026-10-14, NEX-14). Everything except ``main`` is pure and offline.

  python -m strategy.trend_following.raw_check --snapshot dev_2021_2024_nex19 --out <file.md>
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path

import numpy as np

from strategy.trend_following.config import DEV_END, StrategyParams
from strategy.trend_following.costs import CostModel, tick_size

EVENT_MIN_JUMP = 0.05      # a day-over-day change of the raw/adjusted ratio above 5% is an adjustment event
SAMPLE_SIZE = 10


def _align(dates: list[date], raw_rows: dict[date, tuple]) -> np.ndarray:
    """T x 5 array (open, high, low, close, volume) of raw rows on the calendar, NaN where missing."""
    out = np.full((len(dates), 5), np.nan)
    for i, d in enumerate(dates):
        r = raw_rows.get(d)
        if r is not None:
            out[i] = r
    return out


def adjustment_events(dates: list[date], adj_close: np.ndarray, raw_close: np.ndarray) -> list[tuple[date, float]]:
    """(date, ratio change) wherever raw/adjusted jumps by more than EVENT_MIN_JUMP between sessions
    -- the signature of a split, reverse split or bonus issue between them."""
    ok = np.isfinite(adj_close) & np.isfinite(raw_close) & (adj_close > 0)
    ratio = np.where(ok, raw_close / np.where(adj_close > 0, adj_close, 1.0), np.nan)
    events = []
    last = None
    for i in range(len(dates)):
        if not np.isfinite(ratio[i]):
            continue
        if last is not None and abs(ratio[i] / last - 1.0) > EVENT_MIN_JUMP:
            events.append((dates[i], float(ratio[i] / last)))
        last = ratio[i]
    return events


def _on_grid(d: date, market: str, price: float) -> bool:
    tick = tick_size(d, market, price)
    return abs(price / tick - round(price / tick)) < 1e-6


def compare_ticker(dates: list[date], market: str, adj: dict[str, np.ndarray], raw_rows: dict[date, tuple],
                   params: StrategyParams | None = None, cm: CostModel | None = None,
                   start: date | None = None, end: date = DEV_END) -> dict:
    """Level-dependent rule differences for one ticker over sessions in [start, end] where both
    series have a bar. `adj` holds open/close/volume arrays on `dates`; `raw_rows` maps a date to
    (open, high, low, close, volume) as traded."""
    p, cm = params or StrategyParams(), cm or CostModel()
    raw = _align(dates, raw_rows)
    ro, rc, rv = raw[:, 0], raw[:, 3], raw[:, 4]
    ao, ac, av = adj["open"], adj["close"], adj["volume"]
    win = np.array([(start is None or d >= start) and d <= end for d in dates])
    both = win & np.isfinite(ao) & np.isfinite(ac) & np.isfinite(ro) & np.isfinite(rc) & (ao > 0) & (rc > 0)
    idx = np.flatnonzero(both)
    out: dict = {"sessions": int(idx.size), "events": adjustment_events(
        [dates[i] for i in np.flatnonzero(win)], ac[win], rc[win])}
    if idx.size == 0:
        return out
    ratio = rc[idx] / ac[idx]
    out["ratio_min"], out["ratio_max"] = float(ratio.min()), float(ratio.max())

    # 1. tick grid: share of sessions whose open is off its own tick ladder (the engine snaps every fill)
    out["adj_open_off_grid"] = float(np.mean([not _on_grid(dates[i], market, float(ao[i])) for i in idx]))
    out["raw_open_off_grid"] = float(np.mean([not _on_grid(dates[i], market, float(ro[i])) for i in idx]))

    # 2. whole shares: shares bought for a full 20% slot of the seed, adjusted vs as-traded close
    slot = p.max_weight * p.seed_krw
    qa = np.floor(slot / ac[idx])
    qr = np.floor(slot / rc[idx])
    out["qty_differs"] = float(np.mean(qa != qr))
    out["qty_adj_median"], out["qty_raw_median"] = float(np.median(qa)), float(np.median(qr))
    out["raw_unaffordable"] = float(np.mean(qr < 1))          # one share costs more than a 20% slot
    out["adj_unaffordable"] = float(np.mean(qa < 1))

    # 3. liquidity floor: 20-day mean trading value (close x volume), flag = value >= min_avg_trading_value
    w = p.volume_window
    va = ac * av
    vr = rc * rv
    flag_a, flag_r = np.full(len(dates), np.nan), np.full(len(dates), np.nan)
    for i in idx:
        lo = i - w + 1
        if lo < 0:
            continue
        sa, sr = va[lo:i + 1], vr[lo:i + 1]
        if np.isfinite(sa).all() and np.isfinite(sr).all():
            flag_a[i] = float(sa.mean() >= p.min_avg_trading_value)
            flag_r[i] = float(sr.mean() >= p.min_avg_trading_value)
    m = np.isfinite(flag_a) & np.isfinite(flag_r)
    out["liquid_compared"] = int(m.sum())
    out["liquid_differs"] = float(np.mean(flag_a[m] != flag_r[m])) if m.any() else float("nan")
    out["liquid_adj"] = float(np.mean(flag_a[m])) if m.any() else float("nan")
    out["liquid_raw"] = float(np.mean(flag_r[m])) if m.any() else float("nan")

    # 4. daily limits: open at the limit price vs the previous close (needs the previous session in both)
    # (on an adjustment day the exchange resets the base price, which the raw series does not show: skipped)
    ua = da = ur = dr = diff = skipped = 0
    for i in idx:
        if i == 0 or not (np.isfinite(ac[i - 1]) and np.isfinite(rc[i - 1])):
            continue
        if abs((rc[i] / ac[i]) / (rc[i - 1] / ac[i - 1]) - 1.0) > EVENT_MIN_JUMP:
            skipped += 1
            continue
        a_up, a_dn = cm.is_limit_up(dates[i], market, float(ao[i]), float(ac[i - 1])), \
            cm.is_limit_down(dates[i], market, float(ao[i]), float(ac[i - 1]))
        r_up, r_dn = cm.is_limit_up(dates[i], market, float(ro[i]), float(rc[i - 1])), \
            cm.is_limit_down(dates[i], market, float(ro[i]), float(rc[i - 1]))
        ua += a_up
        da += a_dn
        ur += r_up
        dr += r_dn
        diff += (a_up != r_up) or (a_dn != r_dn)
    out.update(limit_adj=int(ua + da), limit_raw=int(ur + dr), limit_differs=int(diff), limit_event_days_skipped=int(skipped))
    return out


def pick_sample(results: dict[str, dict], size: int = SAMPLE_SIZE) -> list[str]:
    """Tickers with at least one adjustment event, largest total log-factor first (ties by code)."""
    scored = []
    for tk, r in results.items():
        ev = r.get("events") or []
        if ev:
            scored.append((-sum(abs(math.log(f)) for _, f in ev), tk))
    return [tk for _, tk in sorted(scored)[:size]]


def _pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def to_markdown(results: dict[str, dict], names: dict[str, str], sample: list[str], scanned: int,
                snapshot: str, source: str) -> str:
    lines = [
        "# A5 비수정 원자료 표본 대조 (NEX-19)", "",
        f"- 스냅샷: `{snapshot}` · 대조 구간: 개발 구간(평가 시작 ~ {DEV_END}) · 비수정 출처: {source}",
        f"- 전체 {scanned}개 종목의 비수정 일봉을 받아 조정 이벤트(원/수정 비율 {EVENT_MIN_JUMP * 100:.0f}% 초과 변동)가 "
        f"있는 종목 중 변동이 큰 순으로 {len(sample)}개를 표본으로 삼았다. 이벤트가 있는 종목은 총 "
        f"{sum(1 for r in results.values() if r.get('events'))}개.",
        "- 엔진은 계속 수정주가를 쓴다. 아래는 수준 의존 규칙이 비수정 가격에서 달라지는 정도의 측정일 뿐이다.", "",
        "| 종목 | 이벤트(날짜 ×비율) | 비율 범위 | 호가 격자 이탈(수정/원) | 수량 상이 | 원가격 1주>슬롯 | 유동성 판정 상이 | 상·하한 시가(수정/원, 상이) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tk in sample:
        r = results[tk]
        ev = ", ".join(f"{d} ×{f:.3g}" for d, f in r["events"])
        lines.append(
            f"| {tk} {names.get(tk, '')} | {ev} | {r['ratio_min']:.3g}~{r['ratio_max']:.3g} "
            f"| {_pct(r['adj_open_off_grid'])} / {_pct(r['raw_open_off_grid'])} | {_pct(r['qty_differs'])} "
            f"| {_pct(r['raw_unaffordable'])} | {_pct(r['liquid_differs'])} "
            f"| {r['limit_adj']} / {r['limit_raw']}, {r['limit_differs']} |")
    allr = [r for r in results.values() if r.get("sessions")]
    if allr:
        lines += ["", "## 전체 종목(대조군 포함) 요약", "",
                  "| 지표 | 값 |", "|---|---|",
                  f"| 비교한 종목 수 | {len(allr)} |",
                  f"| 호가 격자에서 벗어난 수정 시가 비중(종목 평균) | {_pct(float(np.mean([r['adj_open_off_grid'] for r in allr])))} |",
                  f"| 20% 슬롯 수량이 달라지는 세션 비중(종목 평균) | {_pct(float(np.mean([r['qty_differs'] for r in allr])))} |",
                  f"| 유동성 판정이 달라지는 세션 비중(종목 평균) | {_pct(float(np.nanmean([r['liquid_differs'] for r in allr])))} |",
                  f"| 상·하한 판정이 달라진 세션 합계 | {sum(r['limit_differs'] for r in allr)} |"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strategy.trend_following.raw_check", description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    from data.raw_prices import get_raw_daily_prices
    from strategy.trend_following.run import resolve_snapshot
    from strategy.trend_following.snapshot import load_snapshot

    folder = resolve_snapshot(args.snapshot)
    ds = load_snapshot(folder)
    b = ds.stocks
    start = ds.eval_dates[0]
    results: dict[str, dict] = {}
    for n, tk in enumerate(b.tickers, 1):
        try:
            fr = get_raw_daily_prices(tk, b.dates[0], DEV_END)
        except Exception as e:      # one failed ticker must not sink the table; it is listed as missing
            print(f"[{n}/{b.N}] {tk} raw fetch failed: {e}", flush=True)
            continue
        raw_rows = {r[0]: r[1:] for r in fr.select(["Date", "Open", "High", "Low", "Close", "Volume"]).iter_rows()}
        j = b.col(tk)
        results[tk] = compare_ticker(
            b.dates, b.market_of(tk), {"open": b.open[:, j], "close": b.close[:, j], "volume": b.volume[:, j]},
            raw_rows, start=start)
        print(f"[{n}/{b.N}] {tk} events={len(results[tk]['events'])}", flush=True)
    sample = pick_sample(results)
    md = to_markdown(results, b.names, sample, len(results), folder.name,
                     "KIS inquire-daily-itemchartprice, FID_ORG_ADJ_PRC=1 (원주가)")
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"saved {args.out}; sample={sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
