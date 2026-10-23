"""ui/assets_report.py — Pure maths for the Total Assets "Report" (no Qt).

Reconstructs, from the Trading History trade log, what was held and how much
cash there was at the start and the middle of every month, valued at that
day's close next to the KOSPI close. Fed by AssetReportThread (which does the
fetching) and rendered by ui.dialogs.AssetsReportDialog.

Cash has no dated history in the app (Trading History keeps only the current
Deposit and the cumulative Withdrawal in QSettings), so it is rolled back
from an anchor: `end_cash` = current deposit + cumulative withdrawals, i.e.
the cash there would be had nothing been withdrawn, which then gets every buy
dated after a point added back and every sell after it taken out. The result
is the same "withdrawals added back" view as Trading History's Cumulative
Assets; external deposits/withdrawals dated in between are not modelled.
"""
from bisect import bisect_right


def report_dates(trading_days: list[str], start: str, end: str) -> list[tuple[str, str]]:
    """[(date, "early"|"mid"), ...] — each month's first trading day and its
    last trading day on or before the 15th, for trading days in [start, end].

    `trading_days` are ISO dates (KOSPI's calendar), any order.
    """
    by_month: dict[str, list[str]] = {}
    for d in sorted(set(trading_days)):
        if start <= d <= end:
            by_month.setdefault(d[:7], []).append(d)
    out = []
    for month, days in sorted(by_month.items()):
        out.append((days[0], "early"))
        mid = [d for d in days if d <= f"{month}-15"]
        if mid and mid[-1] != days[0]:
            out.append((mid[-1], "mid"))
    return out


def _is_open_on(trade: dict, d: str) -> bool:
    """Bought on or before d and not yet sold by the end of d."""
    bd = str(trade.get("buy_date") or "")[:10]
    sd = str(trade.get("sell_date") or "")[:10]
    return bool(bd) and bd <= d and not (sd and sd <= d)


def holdings_on(trades: list[dict], d: str) -> dict[str, dict]:
    """{ticker: {"company", "market", "qty", "cost"}} open at the close of d."""
    pos: dict[str, dict] = {}
    for t in trades:
        if not _is_open_on(t, d):
            continue
        key = t.get("ticker") or t.get("company", "")
        p = pos.setdefault(key, {"company": t.get("company", ""), "market": t.get("market", ""),
                                 "qty": 0.0, "cost": 0.0})
        p["qty"] += float(t.get("qty") or 0.0)
        p["cost"] += float(t.get("buy_amount") or 0.0)
    return pos


def cash_on(trades: list[dict], end_cash: float, d: str) -> float:
    """Cash at the close of d, rolled back from `end_cash` (cash after the
    last trade): undo every buy and sell dated after d."""
    cash = float(end_cash)
    for t in trades:
        bd = str(t.get("buy_date") or "")[:10]
        sd = str(t.get("sell_date") or "")[:10]
        if bd and bd > d:
            cash += float(t.get("buy_amount") or 0.0)
        if sd and sd > d:
            cash -= float(t.get("sell_amount") or 0.0)
    return cash


def close_on(series: list[tuple[str, float]], d: str) -> float:
    """Last close dated on or before d from an ascending [(date, close)]; 0.0 if none."""
    if not series:
        return 0.0
    i = bisect_right([x[0] for x in series], d)
    return float(series[i - 1][1]) if i else 0.0


def build_report(trades: list[dict], end_cash: float, points: list[tuple[str, str]],
                 closes: dict[str, list[tuple[str, float]]],
                 kospi: list[tuple[str, float]],
                 usd_krw: dict[str, float]) -> list[dict]:
    """One row per (date, kind) point.

    closes: {ticker: ascending [(date, close)]}, in the instrument's own
    currency; `usd_krw[date]` converts US-market closes to KRW (positions
    without a close are valued at cost and flagged `priced=False`).
    """
    rows = []
    base_total = base_kospi = None
    for d, kind in points:
        holdings = []
        mv_total = cost_total = 0.0
        all_priced = True
        for tk, p in holdings_on(trades, d).items():
            px = close_on(closes.get(tk, []), d)
            if px and str(p["market"]).upper() == "US":
                fx = usd_krw.get(d, 0.0)
                px = px * fx if fx else 0.0
            priced = px > 0
            mv = px * p["qty"] if priced else p["cost"]
            all_priced &= priced
            holdings.append({"ticker": tk, "company": p["company"], "qty": p["qty"],
                             "cost": p["cost"], "close": px, "value": mv, "priced": priced})
            mv_total += mv
            cost_total += p["cost"]
        holdings.sort(key=lambda h: h["value"], reverse=True)
        cash = cash_on(trades, end_cash, d)
        total = mv_total + cash
        k = close_on(kospi, d)
        if base_total is None:
            base_total, base_kospi = total, k
        rows.append({
            "date": d, "kind": kind, "kospi": k,
            "kospi_cum_pct": (k / base_kospi - 1) * 100 if base_kospi and k else None,
            "cost": cost_total, "value": mv_total, "cash": cash, "total": total,
            "total_cum_pct": (total / base_total - 1) * 100 if base_total else None,
            "holdings": holdings, "priced": all_priced,
        })
    return rows
