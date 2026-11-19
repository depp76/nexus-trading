"""ui/universe_views.py — Trading Universe filter state, range filters and
their presets (docs/ui.md 2.6 / 8.9). Pure logic, no Qt: StockTable turns
each table row into a plain dict (`row_values`) and both the table's row
filtering and the per-preset match counts go through `matches()` here, so a
preset's count is always exactly the number of rows it shows.

A *filter state* is a dict:
    {"market": "ALL" | [market, ...], "status": ""|"On"|"Tg",
     "search": str, "ranges": {field_key: [lo | None, hi | None]}}
`market` is the list of markets switched on in the toolbar (MARKETS keys),
or "ALL" for a state that leaves the market selection alone; index rows are
never hidden by it. A single market string reads as a one-market list.
Range bounds are kept in the units the user typed (see RANGE_FIELDS).
The saved views were folded into the Filters panel on 2026-11-19 (user
direction): what is left of them are FILTER_PRESETS, its quick picks.
"""
from __future__ import annotations

from dataclasses import dataclass

from i18n import tr


@dataclass(frozen=True)
class RangeField:
    key: str          # StockTable COLUMNS key
    label: str
    unit: str         # shown next to the inputs
    scale: float      # typed value * scale = the cell's stored value
    pct_only: bool    # change columns: rows not in % (yields in bp, VIX/WTI levels) never match


# The numeric columns that take a range filter, in table order.
RANGE_FIELDS = (
    RangeField("cap", tr("시총", "Cap"), tr("억원", "100M KRW"), 1.0, False),
    RangeField("tper", "tPER", "x", 1.0, False),
    RangeField("fper", "fPER", "x", 1.0, False),
    RangeField("chg", tr("등락", "Chg"), "%", 1.0, True),
    RangeField("range52w", tr("52주 위치", "52W position"), tr("범위 대비 %", "% of range"), 0.01, False),
    RangeField("ma20div", "MA20 Div", tr("가격 / MA x100", "price / MA x100"), 1.0, False),
    RangeField("ma60div", "MA60 Div", tr("가격 / MA x100", "price / MA x100"), 1.0, False),
    RangeField("d3", "3D", "%", 1.0, True),
    RangeField("d10", "10D", "%", 1.0, True),
    RangeField("d20", "20D", "%", 1.0, True),
    RangeField("d60", "60D", "%", 1.0, True),
    RangeField("d120", "120D", "%", 1.0, True),
)
FIELD_BY_KEY = {f.key: f for f in RANGE_FIELDS}
# Range keys that were renamed: a filter with the old key is moved to the new one
# when it is read. MA50 Div became MA60 Div on 2026-11-18 (spec v07: the strategy's trend line);
# the bounds are kept as typed (both are price / MA x100 around 100).
LEGACY_RANGE_KEYS = {"ma50div": "ma60div"}


def migrate_ranges(ranges: dict) -> tuple[dict, bool]:
    """`ranges` with legacy keys renamed (a new key already present wins); and whether anything moved."""
    out, moved = {}, False
    for key, bounds in (ranges or {}).items():
        new = LEGACY_RANGE_KEYS.get(key)
        if new is not None:
            moved = True
            if new in (ranges or {}):
                continue
            key = new
        out[key] = bounds
    return out, moved


# The toolbar's markets in display order: key (stored in rows, settings and
# saved views; never changed) -> display name (row meta, status lines, view
# summaries, tooltips). "NASDAQ 100" shows as "NASDAQ" since 2026-11-18 (user
# direction, docs/ui.md 8.32): the list is not the Nasdaq-100 index but the 150
# largest NASDAQ-listed names (ADRs included) -- data.collectors.yahoo's
# Wikipedia constituents table is gone, so it falls back to the FDR exchange
# listing. The toggle button reads "NASDAQ 150" (UniverseTab._MARKET_BUTTON_TEXT).
MARKETS = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "NASDAQ 100": "NASDAQ", "S&P500": "S&P 500"}


def market_display(market: str) -> str:
    """A stored market key as shown on screen ("NASDAQ 100" -> "NASDAQ"); other values as-is."""
    return MARKETS.get(market, market) if market == "NASDAQ 100" else market


def market_list(market) -> list | str:
    """A state's `market` as "ALL" or a list of known markets in display order."""
    if not market or market == "ALL":
        return "ALL"
    given = {market} if isinstance(market, str) else set(market)
    picked = [m for m in MARKETS if m in given]
    return picked or "ALL"

@dataclass(frozen=True)
class FilterPreset:
    """A quick pick at the top of the Filters panel: a name and the ranges
    ticking it fills in (docs/ui.md 8.9; the former built-in saved views)."""
    name: str         # key, kept Korean (the label is translated by preset_label)
    ranges: dict      # {field_key: [lo | None, hi | None]} in RANGE_FIELDS units


# The spec's own examples (docs/ui.md 2.6: "저PER", "신고가 근접").
FILTER_PRESETS = (
    FilterPreset("저PER", {"tper": [0.0, 12.0]}),
    FilterPreset("52주 신고가 근접", {"range52w": [90.0, None]}),
)
_PRESET_EN = {"저PER": "Low tPER", "52주 신고가 근접": "Near 52W High"}


def preset_label(preset: FilterPreset) -> str:
    en = _PRESET_EN.get(preset.name)
    return tr(preset.name, en) if en else preset.name


def _clean_ranges(ranges: dict) -> dict:
    return normalize({"ranges": ranges})["ranges"]


def preset_is_on(ranges: dict, preset: FilterPreset) -> bool:
    """Ticked exactly while every range of the preset is in force, whether
    it got there by ticking the box or by typing the same bounds."""
    current, own = _clean_ranges(ranges), _clean_ranges(preset.ranges)
    return bool(own) and all(current.get(k) == b for k, b in own.items())


def add_preset(ranges: dict, preset: FilterPreset) -> dict:
    """`ranges` with the preset's ranges added (ticked presets all apply);
    a bound on the same column is replaced, which unticks a preset that set
    it differently."""
    out = _clean_ranges(ranges)
    out.update(_clean_ranges(preset.ranges))
    return out


def remove_preset(ranges: dict, preset: FilterPreset) -> dict:
    """`ranges` without the preset's own ranges; other bounds stay."""
    out = _clean_ranges(ranges)
    for k, b in _clean_ranges(preset.ranges).items():
        if out.get(k) == b:
            del out[k]
    return out


def empty_state() -> dict:
    return {"market": "ALL", "status": "", "search": "", "ranges": {}}


def normalize(state: dict) -> dict:
    """A clean copy: known keys only, ranges with at least one bound, bounds
    as floats. Used before saving and comparing."""
    out = empty_state()
    out["market"] = market_list(state.get("market"))
    out["status"] = state.get("status") or ""
    out["search"] = (state.get("search") or "").strip()
    for key, bounds in migrate_ranges(state.get("ranges") or {})[0].items():
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
    markets = market_list(state.get("market"))
    # Only markets with a toggle are filtered: index rows and single names
    # added from a market without one (NASDAQ / NYSE, 2026-11-15) always show.
    if markets != "ALL" and row.get("market") in MARKETS and row.get("market") not in markets:
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
