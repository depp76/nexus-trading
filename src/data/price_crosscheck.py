"""data/price_crosscheck.py — compare the app's KR daily prices (Naver) against KIS.

Two independently sourced split-adjusted series should agree. A date present on one side only,
or a price gap above ``PRICE_TOLERANCE_PCT``, points at a data error or a differently applied
corporate-action adjustment. Halted days (no volume on either side) compare the close only: KIS
repeats the close into open/high/low where Naver reports 0, a notation gap and not a price gap.
This only reports: nothing switches source because of it, and
``get_historical_data`` keeps routing KR codes to Naver. Rows after the development window are
dropped from both sides before any comparison, so the holdout year is never looked at.

  python -m data.price_crosscheck --start 2021-01-01 --out <file.md>
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from data.raw_prices import RAW_DEV_END

PRICE_FIELDS = ("Open", "High", "Low", "Close")
PRICE_TOLERANCE_PCT = 0.5      # rounding of adjusted prices stays well under this
VOLUME_TOLERANCE_PCT = 1.0
MAX_LISTED_DATES = 5
# Large caps from both boards. 035720 (Kakao) split 5:1 in April 2021 and 373220 listed in 2022,
# so the default sample exercises the adjustment and a mid-window start.
DEFAULT_TICKERS = ("005930", "000660", "035420", "035720", "373220", "247540", "091990")


@dataclass
class FieldDiff:
    mismatches: int = 0
    max_pct: float = 0.0
    worst_date: date | None = None


@dataclass
class CrossCheck:
    ticker: str
    common: int = 0
    only_kis: list[date] = field(default_factory=list)
    only_ref: list[date] = field(default_factory=list)
    diffs: dict[str, FieldDiff] = field(default_factory=dict)
    error: str | None = None

    @property
    def clean(self) -> bool:
        return (self.error is None and self.common > 0 and not self.only_kis and not self.only_ref
                and not any(d.mismatches for d in self.diffs.values()))


def _rows(frame: pl.DataFrame | None, start: date, stop: date) -> dict[date, dict]:
    """{day: row} inside start..stop; Date may arrive as a date or a datetime."""
    if frame is None or frame.is_empty():
        return {}
    out: dict[date, dict] = {}
    for row in frame.iter_rows(named=True):
        d = row["Date"]
        d = d.date() if hasattr(d, "date") else d
        if start <= d <= stop:
            out[d] = row
    return out


def compare_frames(ticker: str, kis: pl.DataFrame | None, ref: pl.DataFrame | None, start: date,
                   end: date | None = None, price_tol: float = PRICE_TOLERANCE_PCT,
                   volume_tol: float = VOLUME_TOLERANCE_PCT) -> CrossCheck:
    """Compare OHLCV day by day; gaps are percent of the reference value. ``end`` is capped at
    the development window whatever the caller passes."""
    stop = min(end or RAW_DEV_END, RAW_DEV_END)
    a, b = _rows(kis, start, stop), _rows(ref, start, stop)
    res = CrossCheck(ticker, only_kis=sorted(a.keys() - b.keys()), only_ref=sorted(b.keys() - a.keys()))
    shared = sorted(a.keys() & b.keys())
    res.common = len(shared)
    for col, tol in [(c, price_tol) for c in PRICE_FIELDS] + [("Volume", volume_tol)]:
        diff = FieldDiff()
        for d in shared:
            x, y = a[d].get(col), b[d].get(col)
            if x is None or y is None:
                continue
            if col in ("Open", "High", "Low") and not a[d].get("Volume") and not b[d].get("Volume"):
                continue
            if y == 0:
                pct = 0.0 if x == 0 else float("inf")
            else:
                pct = abs(float(x) - float(y)) / abs(float(y)) * 100.0
            if pct > tol:
                diff.mismatches += 1
            if pct > diff.max_pct:
                diff.max_pct, diff.worst_date = pct, d
        res.diffs[col] = diff
    return res


def _dates(ds: list[date]) -> str:
    if not ds:
        return "-"
    shown = ", ".join(d.isoformat() for d in ds[:MAX_LISTED_DATES])
    return shown + (f" 외 {len(ds) - MAX_LISTED_DATES}건" if len(ds) > MAX_LISTED_DATES else "")


def render_markdown(results: list[CrossCheck], start: date, end: date) -> str:
    lines = [
        "# KIS vs Naver 일봉 대조",
        "",
        f"- 구간: {start} ~ {end} (개발 구간 밖은 비교하지 않음)",
        f"- 허용 오차: 가격 {PRICE_TOLERANCE_PCT}%, 거래량 {VOLUME_TOLERANCE_PCT}%",
        f"- 일치: {sum(r.clean for r in results)} / {len(results)}종목",
        "",
        "| 종목 | 공통 일수 | KIS만 | Naver만 | 가격 불일치(일·필드) | 가격 최대 차이 | 거래량 불일치 | 거래량 최대 차이 | 결과 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.ticker} | - | - | - | - | - | - | - | 조회 실패: {r.error} |")
            continue
        prices = [r.diffs[c] for c in PRICE_FIELDS]
        worst = max(prices, key=lambda d: d.max_pct)
        vol = r.diffs["Volume"]
        prices_ok = not any(d.mismatches for d in prices) and not r.only_kis and not r.only_ref
        verdict = ("일치" if r.clean else "데이터 없음" if r.common == 0
                   else "거래량만 차이" if prices_ok else "차이 있음")
        lines.append(
            f"| {r.ticker} | {r.common} | {len(r.only_kis)} | {len(r.only_ref)} | "
            f"{sum(d.mismatches for d in prices)} | {worst.max_pct:.2f}% ({worst.worst_date or '-'}) | "
            f"{vol.mismatches} | {vol.max_pct:.2f}% ({vol.worst_date or '-'}) | {verdict} |")
    one_sided = [r for r in results if r.only_kis or r.only_ref]
    if one_sided:
        lines += ["", "## 한쪽에만 있는 날짜", ""]
        for r in one_sided:
            lines.append(f"- {r.ticker}: KIS만 {_dates(r.only_kis)} / Naver만 {_dates(r.only_ref)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m data.price_crosscheck", description=__doc__.split("\n")[0])
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS), help="comma-separated KRX codes")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=RAW_DEV_END, help="capped at the development window")
    ap.add_argument("--out", help="write the markdown report here instead of printing it")
    args = ap.parse_args(argv)

    from data.history import get_historical_data
    from data.raw_prices import get_adjusted_daily_prices

    stop = min(args.end, RAW_DEV_END)
    results = []
    for tk in (t.strip() for t in args.tickers.split(",") if t.strip()):
        try:
            kis = get_adjusted_daily_prices(tk, args.start, stop)
            ref = get_historical_data(tk, args.start.isoformat())
            results.append(compare_frames(tk, kis, ref, args.start, stop))
        except Exception as e:      # one failed ticker must not sink the table; it is listed as failed
            results.append(CrossCheck(tk, error=str(e)))
        print(f"{tk} done", file=sys.stderr, flush=True)
    md = render_markdown(results, args.start, stop)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"saved {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(md)
    return 0 if all(r.clean for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
