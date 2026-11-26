"""Pure maths behind the MA chart's 재무 tab (docs/dart_db_plan_2026-11-26.md 13;
docs/ui.md 8.34). No Qt: built from ``data.dart.query.company_financials`` and
tested on plain dicts.

- ``quarter_rows``: one row per fiscal quarter (revenue, operating income and
  margin, owners' -- else total -- net income, YoY / QoQ, the operating
  profit-turn flag, derived Q4 †, a correction receipt ⓡ, receipt date).
- ``annual_rows``: one row per fiscal year (the annual report's totals; the
  current year as its cumulative to the latest quarter, compared with the
  same quarters a year before).
- ``kpis``: the four cards (latest quarter revenue, operating income with
  margin, owners' net income, trailing four quarters' revenue).
- ``segment_view``: segment revenue per fiscal year (annual reports, and the
  newest period of the year in progress), shares, YoY, the CAGR over the full
  years, the check status per column and the reorganisation notes.

Amounts stay in the reporting currency's unit (``unit_of``: 억원 for KRW,
millions otherwise; plan 4-6).
"""
from __future__ import annotations

from i18n import tr

TURN_LABELS = {"turn_profit": ("흑자전환", "Turned profitable"), "turn_loss": ("적자전환", "Turned to loss"),
               "loss_continued": ("적자지속", "Loss continued")}
CHECK_LABELS = {"ok": ("ok", "ok"), "sum_mismatch": ("합계 불일치", "Sum mismatch"),
                "review": ("확인 필요", "Review"), "na": ("해당 없음", "N/A")}


def unit_of(currency: str | None) -> tuple[float, str, str]:
    """(divisor, Korean unit, English unit) for a reporting currency."""
    if (currency or "KRW") == "KRW":
        return 1e8, "억원", "KRW 100M"
    return 1e6, f"백만 {currency}", f"{currency} M"


def growth(cur, base):
    if cur is None or base is None or base <= 0:
        return None
    return cur / base - 1


def _key(r):
    return r["fiscal_year"], r["fiscal_q"]


def quarter_rows(data: dict) -> list[dict]:
    """Oldest quarter first; see the module docstring."""
    corrections = data.get("corrections") or set()
    by: dict = {}
    for r in data.get("rows") or []:
        by.setdefault(_key(r), {})[r["metric"]] = r
    out = []
    for (fy, fq), m in sorted(by.items()):
        rev, op = m.get("revenue"), m.get("op_income")
        ni = m.get("net_income_owners") or m.get("net_income")
        present = [x for x in (rev, op, ni) if x]
        if not present:
            continue
        rv = rev["value_q"] if rev else None
        ov = op["value_q"] if op else None
        out.append({
            "fiscal_year": fy, "fiscal_q": fq, "period_end": present[0]["period_end"],
            "revenue": rv, "revenue_yoy": rev["yoy"] if rev else None, "revenue_qoq": rev["qoq"] if rev else None,
            "op_income": ov, "op_yoy": op["yoy"] if op else None, "op_qoq": op["qoq"] if op else None,
            "op_margin": (ov / rv) if (ov is not None and rv) else None,
            "net_income": ni["value_q"] if ni else None, "net_yoy": ni["yoy"] if ni else None,
            "net_qoq": ni["qoq"] if ni else None, "net_is_owners": bool(m.get("net_income_owners")),
            "turn": op["turn"] if op else None,
            "derived": any(x["derived"] for x in present),
            "lookahead": any(x.get("lookahead") for x in present),
            "corrected": any(x["rcept_no"] in corrections for x in present),
            "rcept_dt": max(x["rcept_dt"] for x in present),
            "rcept_no": max(x["rcept_no"] for x in present),
            "fs_div": present[0]["fs_div"], "currency": present[0].get("currency") or "KRW",
            "ytd": {k: (x["value_ytd"] if x else None) for k, x in (("revenue", rev), ("op_income", op),
                                                                     ("net_income", ni))},
        })
    return out


def same_basis(a: dict, b: dict) -> bool:
    """Whether two rows may be added or compared: the same reporting currency
    and the same statement kind (consolidated / separate). The data layer
    applies this to YoY / QoQ; the tab's own sums and annual comparisons must
    too (review_codex 2 P2)."""
    return (a.get("currency") or "KRW") == (b.get("currency") or "KRW") and a.get("fs_div") == b.get("fs_div")


def _quarter_before(key: tuple[int, int], n: int) -> tuple[int, int]:
    idx = key[0] * 4 + key[1] - 1 - n
    return idx // 4, idx % 4 + 1


def kpis(quarters: list[dict]) -> dict:
    """The four cards' numbers from quarter_rows. The trailing-four-quarter
    revenue needs the four *consecutive* fiscal quarters ending at the latest
    one, all with a revenue on the same basis -- with a quarter missing (an
    initial load under way, one filing failed) there is no sum, rather than a
    sum of whatever four rows come last; the comparison likewise needs the
    four quarters before them (review_codex 2 P2)."""
    if not quarters:
        return {}
    last = quarters[-1]
    by = {(q["fiscal_year"], q["fiscal_q"]): q for q in quarters}
    key = (last["fiscal_year"], last["fiscal_q"])
    ago = by.get((last["fiscal_year"] - 1, last["fiscal_q"]))
    margin_change = None
    if last["op_margin"] is not None and ago and ago["op_margin"] is not None and same_basis(ago, last):
        margin_change = last["op_margin"] - ago["op_margin"]

    def window(start: int):
        rows = [by.get(_quarter_before(key, start + i)) for i in range(4)]
        if all(r is not None and r["revenue"] is not None and same_basis(r, last) for r in rows):
            return sum(r["revenue"] for r in rows)
        return None
    ttm, ttm_prev = window(0), window(4)
    return {"quarter": key, "last": last, "margin_change": margin_change, "ttm": ttm,
            "ttm_growth": growth(ttm, ttm_prev) if ttm is not None else None}


def annual_rows(quarters: list[dict]) -> list[dict]:
    """One row per fiscal year, oldest first. A year with its Q4 uses the
    annual cumulative; the year in progress its cumulative to the latest
    quarter (``partial_q``), compared with the same cumulative a year before."""
    by_year: dict = {}
    for q in quarters:
        by_year.setdefault(q["fiscal_year"], []).append(q)
    at = {(q["fiscal_year"], q["fiscal_q"]): q for q in quarters}
    out = []
    for fy in sorted(by_year):
        last = max(by_year[fy], key=lambda q: q["fiscal_q"])
        vals = {k: last["ytd"].get(k) for k in ("revenue", "op_income", "net_income")}
        # The same cumulative a year before, only on the same currency and statement kind.
        before = at.get((fy - 1, last["fiscal_q"]))
        changed = before is not None and not same_basis(before, last)
        prev = before["ytd"] if (before is not None and not changed) else {}
        out.append({
            "fiscal_year": fy, "partial_q": None if last["fiscal_q"] == 4 else last["fiscal_q"],
            "revenue": vals["revenue"], "op_income": vals["op_income"], "net_income": vals["net_income"],
            "revenue_yoy": growth(vals["revenue"], prev.get("revenue")),
            "op_yoy": growth(vals["op_income"], prev.get("op_income")),
            "net_yoy": growth(vals["net_income"], prev.get("net_income")),
            "op_margin": (vals["op_income"] / vals["revenue"]) if (vals["op_income"] is not None
                                                                  and vals["revenue"]) else None,
            "turn": _turn(vals["op_income"], prev.get("op_income")),
            "rcept_dt": last["rcept_dt"], "derived": False, "corrected": last["corrected"],
            "currency": last["currency"], "fs_div": last["fs_div"], "basis_changed": changed,
        })
    return out


def _turn(cur, prev):
    if cur is None or prev is None:
        return None
    if prev <= 0 < cur:
        return "turn_profit"
    if prev > 0 >= cur:
        return "turn_loss"
    if prev <= 0 and cur <= 0:
        return "loss_continued"
    return None


def _display_name(raw: str) -> str:
    """A segment name without note marks: "차량(*1)" -> "차량"."""
    import re
    return re.sub(r"\s*\(\*\d*\)$", "", raw or "").strip()


def segment_view(data: dict, max_years: int = 6) -> dict:
    """Segment revenue by fiscal year (see the module docstring):
    {"columns": [{"fiscal_year", "fiscal_q", "label", "basis", "status", "source"}],
     "segments": [{"code", "name", "values": [..], "yoy": [..], "shares": [..], "cagr"}],
     "notes": [...], "currency"}. Columns are the annual reports, plus the newest
    period of a year that has no annual report yet."""
    all_rows = data.get("segments") or []
    rows = [r for r in all_rows if not str(r.get("segment_code") or "").startswith("_")]
    adjust: dict = {}
    for r in all_rows:
        if str(r.get("segment_code") or "").startswith("_") and r.get("revenue") is not None:
            adjust[_key(r)] = adjust.get(_key(r), 0.0) + r["revenue"]
    status = data.get("segment_status") or {}
    by_period: dict = {}
    for r in rows:
        by_period.setdefault(_key(r), []).append(r)
    years: dict = {}
    for (fy, fq) in by_period:                 # the annual report, else the newest quarter of the year
        years[fy] = max(years.get(fy, 0), fq)
    cols = []
    for fy in sorted(years)[-max_years:]:
        fq = years[fy]
        recs = by_period[(fy, fq)]
        basis = recs[0]["basis"]
        st = status.get((fy, fq), (recs[0]["check_status"], recs[0]["source"]))
        if fq == 4:
            label = str(fy)
        elif basis == "ytd":
            label = tr(f"{fy} {fq}Q누적", f"{fy} Q{fq} YTD")
        else:
            label = tr(f"{fy} {fq}Q", f"{fy} Q{fq}")
        seg_sum = sum(r["revenue"] for r in recs if r["revenue"] and r["revenue"] > 0)
        # Segments reported gross (before intersegment eliminations, which come as
        # adjustment rows) are not comparable with external-revenue segments: a
        # company whose source changes (Hyundai: the note's net table, then
        # XBRL's gross one) would show growth that is only the change of basis.
        gross = abs(adjust.get((fy, fq), 0.0)) > 0.01 * seg_sum if seg_sum else False
        cols.append({"fiscal_year": fy, "fiscal_q": fq, "label": label, "basis": basis, "gross": gross,
                     "status": st[0], "source": st[1], "rows": recs})
    # Codes ordered by the newest column's revenue; the newest raw name names a code.
    names: dict = {}
    for c in cols:
        for r in c["rows"]:
            names[r["segment_code"]] = _display_name(r["segment_raw"])
    last_vals = {r["segment_code"]: (r["revenue"] or 0) for r in (cols[-1]["rows"] if cols else [])}
    codes = sorted(names, key=lambda k: (-last_vals.get(k, 0), names[k]))
    segs = []
    for code in codes:
        values = []
        for c in cols:
            v = [r["revenue"] for r in c["rows"] if r["segment_code"] == code and r["revenue"] is not None]
            values.append(sum(v) if v else None)
        yoy = [None]
        for i in range(1, len(cols)):
            prev, cur = cols[i - 1], cols[i]
            # Exactly the year before: with a year missing in between, the change
            # over two years is not a year-on-year figure (review_codex 2 P2).
            comparable = (prev["fiscal_q"] == 4 and cur["fiscal_q"] == 4 and prev["basis"] == cur["basis"]
                          and prev["gross"] == cur["gross"] and prev["fiscal_year"] == cur["fiscal_year"] - 1)
            yoy.append(growth(values[i], values[i - 1]) if comparable else None)
        # CAGR over the newest run of full years on one basis (gross or external).
        full = []
        for c, v in reversed(list(zip(cols, values))):
            if c["fiscal_q"] != 4:
                continue
            if full and (c["gross"] != full[-1][2] or c["fiscal_year"] != full[-1][0] - 1):
                break                          # another basis, or a year missing: the run ends
            if v:
                full.append((c["fiscal_year"], v, c["gross"]))
        full.reverse()
        cagr = None
        if len(full) >= 2 and full[0][1] > 0 and full[-1][1] > 0 and full[-1][0] > full[0][0]:
            cagr = (full[-1][1] / full[0][1]) ** (1 / (full[-1][0] - full[0][0])) - 1
        segs.append({"code": code, "name": names[code], "values": values, "yoy": yoy, "cagr": cagr})
    for i, _c in enumerate(cols):
        total = sum(s["values"][i] for s in segs if s["values"][i] and s["values"][i] > 0)
        for s in segs:
            v = s["values"][i]
            s.setdefault("shares", []).append((v / total) if (v and v > 0 and total) else None)
    notes = []
    for u in data.get("user_maps") or []:
        notes.append(("map", u))
    for u in data.get("unmatched") or []:
        notes.append(("unmatched", u))
    currency = (data.get("rows") or [{}])[-1].get("currency") if data.get("rows") else "KRW"
    return {"columns": [{k: v for k, v in c.items() if k != "rows"} for c in cols], "segments": segs,
            "notes": notes, "currency": currency or "KRW"}


def basis_report(quarters: list[dict]) -> dict:
    """The header chips: the newest report (year, quarter, receipt date,
    consolidated or separate) and the span of quarters shown."""
    if not quarters:
        return {}
    last, first = quarters[-1], quarters[0]
    return {"fiscal_year": last["fiscal_year"], "fiscal_q": last["fiscal_q"], "rcept_dt": last["rcept_dt"],
            "fs_div": last["fs_div"], "first": (first["fiscal_year"], first["fiscal_q"]),
            "currency": last["currency"]}
