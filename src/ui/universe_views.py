"""ui/universe_views.py — Trading Universe range filters and saved views
(docs/ui.md 2.6 / 8.9). Pure logic, no Qt: StockTable turns each table row
into a plain dict (`row_values`) and both the table's row filtering and the
per-view match counts go through `matches()` here, so a view's count is
always exactly the number of rows the view shows.

A *filter state* (also what a saved view stores) is a dict:
    {"market": "ALL"|"KOSPI"|"KOSDAQ", "status": ""|"On"|"Tg",
     "search": str, "ranges": {field_key: [lo | None, hi | None]}}
Range bounds are kept in the units the user typed (see RANGE_FIELDS).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RangeField:
    key: str          # StockTable COLUMNS key
    label: str
    unit: str         # shown next to the inputs
    scale: float      # typed value * scale = the cell's stored value
    pct_only: bool    # change columns: rows not in % (yields in bp, VIX/WTI levels) never match


# The numeric columns that take a range filter, in table order.
RANGE_FIELDS = (
    RangeField("cap", "Cap", "억원", 1.0, False),
    RangeField("tper", "tPER", "x", 1.0, False),
    RangeField("fper", "fPER", "x", 1.0, False),
    RangeField("chg", "Chg", "%", 1.0, True),
    RangeField("range52w", "52W position", "% of range", 0.01, False),
    RangeField("ma20div", "MA20 Div", "price / MA x100", 1.0, False),
    RangeField("ma50div", "MA50 Div", "price / MA x100", 1.0, False),
    RangeField("d3", "3D", "%", 1.0, True),
    RangeField("d10", "10D", "%", 1.0, True),
    RangeField("d20", "20D", "%", 1.0, True),
    RangeField("d60", "60D", "%", 1.0, True),
    RangeField("d120", "120D", "%", 1.0, True),
)
FIELD_BY_KEY = {f.key: f for f in RANGE_FIELDS}

# The spec's own examples (docs/ui.md 2.6: "저PER", "신고가 근접"); always
# listed, never deleted. Held/Target are the toolbar's status button.
BUILTIN_VIEWS = (
    {"name": "Low tPER", "market": "ALL", "status": "", "search": "", "ranges": {"tper": [0.0, 12.0]}},
    {"name": "Near 52W High", "market": "ALL", "status": "", "search": "", "ranges": {"range52w": [90.0, None]}},
)


def empty_state() -> dict:
    return {"market": "ALL", "status": "", "search": "", "ranges": {}}


def normalize(state: dict) -> dict:
    """A clean copy: known keys only, ranges with at least one bound, bounds
    as floats. Used before saving and comparing."""
    out = empty_state()
    out["market"] = state.get("market") or "ALL"
    out["status"] = state.get("status") or ""
    out["search"] = (state.get("search") or "").strip()
    for key, bounds in (state.get("ranges") or {}).items():
        if key not in FIELD_BY_KEY or not bounds:
            continue
        lo, hi = (list(bounds) + [None, None])[:2]
        lo = None if lo is None else float(lo)
        hi = None if hi is None else float(hi)
        if lo is not None or hi is not None:
            out["ranges"][key] = [lo, hi]
    return out


def same_state(a: dict, b: dict) -> bool:
    na, nb = normalize(a), normalize(b)
    na.pop("name", None)
    nb.pop("name", None)
    return na == nb


def matches(row: dict, state: dict) -> bool:
    """Does a table row pass a filter state? `row` is
    {"market", "status", "name", "ticker", "mode", "values": {key: float | None}}
    where None means the cell has no value (e.g. no PER, no 52W range yet);
    a row with no value never passes a filter on that column."""
    market = state.get("market") or "ALL"
    if market != "ALL" and row.get("market") != market:
        return False
    status = state.get("status") or ""
    if status and row.get("status") != status:
        return False
    search = (state.get("search") or "").strip().lower()
    if search and search not in (row.get("name") or "").lower() and search not in (row.get("ticker") or "").lower():
        return False
    for key, bounds in (state.get("ranges") or {}).items():
        field = FIELD_BY_KEY.get(key)
        if field is None or not bounds:
            continue
        lo, hi = (list(bounds) + [None, None])[:2]
        if lo is None and hi is None:
            continue
        if field.pct_only and row.get("mode", "pct") != "pct":
            return False
        value = (row.get("values") or {}).get(key)
        if value is None:
            return False
        if lo is not None and value < lo * field.scale:
            return False
        if hi is not None and value > hi * field.scale:
            return False
    return True


def count_matches(rows: list, state: dict) -> int:
    return sum(1 for r in rows if matches(r, state))


def describe_view(state: dict) -> str:
    """'KOSPI · Held · "samsung" · tPER 0-12' -- the Views list tooltip."""
    s = normalize(state)
    parts = []
    if s["market"] != "ALL":
        parts.append(s["market"])
    if s["status"]:
        parts.append({"On": "Held", "Tg": "Target"}.get(s["status"], s["status"]))
    if s["search"]:
        parts.append(f'"{s["search"]}"')
    if s["ranges"]:
        parts.append(describe_ranges(s["ranges"]))
    return " \u00b7 ".join(parts) or "No filter"


def describe_ranges(ranges: dict) -> str:
    """'tPER 0-12 · 52W position >= 90' -- for tooltips and the Filters button."""
    parts = []
    for f in RANGE_FIELDS:
        bounds = ranges.get(f.key)
        if not bounds:
            continue
        lo, hi = (list(bounds) + [None, None])[:2]
        if lo is not None and hi is not None:
            parts.append(f"{f.label} {lo:g}–{hi:g}")
        elif lo is not None:
            parts.append(f"{f.label} ≥ {lo:g}")
        elif hi is not None:
            parts.append(f"{f.label} ≤ {hi:g}")
    return " · ".join(parts)
