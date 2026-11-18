"""ui/kis_sync.py — turn KIS executed orders into Trading History rows (pure, no Qt).

The history tab's "KIS 조회" button (docs/ui.md 8.28) fetches the account's
deposit, holdings and executed orders; `plan_kis_import` works out what that
means for the trades table, and the tab shows the plan for confirmation before
`trade_db.apply_kis_import` saves it in one transaction.

Rules:
- An order is identified by "<order date>:<order no>" (KIS order numbers repeat
  across days). `imported` holds how much of each order is already in the
  table (cumulative qty and amount), so a re-run, or an order that filled more
  after the last run, only brings in the difference.
- Fills are summed per (date, ticker, side): several orders, or one order filled
  in pieces, on one day become one row at the volume-weighted price.
- A buy is a new open lot. A sell closes open lots of that ticker first in, first
  out (oldest buy date first); a lot sold only in part is split like a manual
  partial sell (`TradingHistoryTab._split_partial_sell`): the sold part becomes
  the closed row, the rest stays open as its own row.
- A sell larger than the open quantity is skipped whole and reported, and its
  orders are not marked as imported, so it comes back on the next run (add the
  missing buy first).
- Amounts are price x quantity before fees and taxes, as in a manual entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from i18n import tr

_EPS = 1e-9


@dataclass
class PreviewRow:
    date: str
    side: str            # "buy" / "sell"
    ticker: str
    name: str
    qty: float
    price: float
    amount: float
    note: str
    applied: bool = True


@dataclass
class ImportPlan:
    records: list = field(default_factory=list)    # trade dicts to upsert (with orig_key = update)
    imports: list = field(default_factory=list)    # (order_key, cumulative qty, cumulative amount)
    rows: list = field(default_factory=list)       # PreviewRow, in date order
    warnings: list = field(default_factory=list)

    @property
    def applicable(self) -> bool:
        return bool(self.records)


def order_key(execution: dict) -> str:
    return f"{execution['date']}:{execution['order_no']}"


def _new_deltas(executions: list, imported: dict) -> list:
    """Each order's fill not yet in the table: (execution, delta qty, delta amount, key)."""
    out = []
    for e in executions:
        key = order_key(e)
        done_qty, done_amt = imported.get(key, (0.0, 0.0))
        dq = e["qty"] - done_qty
        if dq <= _EPS:
            continue
        da = e["amount"] - done_amt
        if da <= 0:
            da = e["price"] * dq
        out.append((e, dq, da, key))
    return out


def _group_fills(deltas: list) -> list:
    """Sum the deltas per (date, ticker, side); buys before sells on a day."""
    groups: dict = {}
    for e, dq, da, key in deltas:
        g = groups.setdefault((e["date"], e["ticker"], e["side"]),
                              {"date": e["date"], "ticker": e["ticker"], "side": e["side"], "name": e["name"],
                               "qty": 0.0, "amount": 0.0, "imports": []})
        g["qty"] += dq
        g["amount"] += da
        g["imports"].append((key, e["qty"], e["amount"]))
    return sorted(groups.values(), key=lambda g: (g["date"], 0 if g["side"] == "buy" else 1, g["ticker"]))


def _blank_lot(company: str, market: str, ticker: str, date: str, price: float, qty: float, amount: float) -> dict:
    return {"orig_key": None, "company": company, "market": market, "ticker": ticker,
            "buy_date": date, "buy_price": price, "qty": qty, "buy_amount": amount,
            "sell_date": "", "sell_price": 0.0, "sell_qty": 0.0, "sell_amount": 0.0}


def _fmt_qty(q: float) -> str:
    return f"{q:,.0f}" if abs(q - round(q)) < _EPS else f"{q:,.4f}"


def plan_kis_import(executions: list, imported: dict, open_lots: list, markets: dict | None = None,
                    holdings: list | None = None) -> ImportPlan:
    """The plan for `executions` (data.collectors.kis.parse_execution dicts) against
    the table's open lots. `imported`: order_key -> (qty, amount) already saved;
    `markets`: ticker -> market for new lots; `holdings`: KIS holdings
    ({"ticker", "name", "qty"}) to cross-check the open quantity after the plan.
    Never changes its inputs."""
    markets = markets or {}
    plan = ImportPlan()
    # Working copies of the open lots, per ticker, oldest buy first (stable for ties).
    lots: dict = {}
    for rec in open_lots:
        lots.setdefault(rec.get("ticker", ""), []).append(dict(rec))
    for lst in lots.values():
        lst.sort(key=lambda r: r.get("buy_date", ""))
    touched: dict = {}          # id(working lot) -> working lot, to save
    new_lots: list = []

    for g in _group_fills(_new_deltas(executions, imported)):
        qty, amount = g["qty"], g["amount"]
        price = amount / qty
        if g["side"] == "buy":
            lot = _blank_lot(g["name"], markets.get(g["ticker"], ""), g["ticker"], g["date"], price, qty, amount)
            lots.setdefault(g["ticker"], []).append(lot)
            new_lots.append(lot)
            touched[id(lot)] = lot
            plan.imports.extend(g["imports"])
            plan.rows.append(PreviewRow(g["date"], "buy", g["ticker"], g["name"], qty, price, amount,
                                        tr("새 보유", "New position")))
            continue

        # Sell: only open lots bought on or before the sell date.
        avail = [lot for lot in lots.get(g["ticker"], [])
                 if not lot.get("sell_date") and lot.get("buy_date", "") <= g["date"]]
        held = sum(lot.get("qty", 0.0) for lot in avail)
        if held + _EPS < qty:
            msg = tr(f"건너뜀: 보유 {_fmt_qty(held)}주보다 많은 매도",
                     f"Skipped: sells more than the {_fmt_qty(held)} held")
            plan.rows.append(PreviewRow(g["date"], "sell", g["ticker"], g["name"], qty, price, amount, msg, False))
            plan.warnings.append(tr(
                f"{g['date']} {g['name']}({g['ticker']}) 매도 {_fmt_qty(qty)}주: Trading History의 보유는 {_fmt_qty(held)}주라 "
                "건너뛰었습니다. 빠진 매수를 먼저 추가한 뒤 다시 조회하세요.",
                f"{g['date']} {g['name']} ({g['ticker']}) sell of {_fmt_qty(qty)}: Trading History holds "
                f"{_fmt_qty(held)}, so it was skipped. Add the missing buy, then fetch again."))
            continue

        left = qty
        closed_parts = []
        for lot in avail:
            if left <= _EPS:
                break
            lot_qty = lot.get("qty", 0.0)
            take = min(lot_qty, left)
            if lot_qty - take > _EPS:
                # Split: the sold part keeps this row (and its orig_key), the rest is a new open row.
                buy_amount = lot.get("buy_amount", 0.0)
                sold_cost = buy_amount * (take / lot_qty) if lot_qty else 0.0
                rest = dict(lot, orig_key=None, qty=lot_qty - take, buy_amount=buy_amount - sold_cost)
                lst = lots[g["ticker"]]
                at = next(i for i, x in enumerate(lst) if x is lot)     # identity, not dict equality
                lst.insert(at + 1, rest)
                touched[id(rest)] = rest
                lot["qty"], lot["buy_amount"] = take, sold_cost
            lot.update(sell_date=g["date"], sell_price=price, sell_qty=take, sell_amount=price * take)
            touched[id(lot)] = lot
            closed_parts.append(tr(f"{lot.get('buy_date', '')} 매수분 {_fmt_qty(take)}주",
                                   f"{_fmt_qty(take)} bought {lot.get('buy_date', '')}"))
            left -= take
        plan.imports.extend(g["imports"])
        plan.rows.append(PreviewRow(g["date"], "sell", g["ticker"], g["name"], qty, price, amount,
                                    tr("청산: ", "Closes: ") + ", ".join(closed_parts)))

    plan.records = list(touched.values())

    if holdings is not None:
        after = {t: sum(lot.get("qty", 0.0) for lot in lst if not lot.get("sell_date")) for t, lst in lots.items()}
        kis_qty = {h["ticker"]: h["qty"] for h in holdings}
        names = {h["ticker"]: h.get("name", "") for h in holdings}
        names.update({r.ticker: r.name for r in plan.rows})
        for t in sorted(set(kis_qty) | {r.ticker for r in plan.rows}):
            have, want = after.get(t, 0.0), kis_qty.get(t, 0.0)
            if abs(have - want) > _EPS:
                plan.warnings.append(tr(
                    f"{names.get(t, '')}({t}): KIS 잔고 {_fmt_qty(want)}주, 반영 후 Trading History 보유 {_fmt_qty(have)}주.",
                    f"{names.get(t, '')} ({t}): KIS holds {_fmt_qty(want)}, Trading History will hold {_fmt_qty(have)}."))
    return plan
