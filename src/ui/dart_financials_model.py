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
- ``segment_view``: segment (or product) revenue per fiscal year (annual
  reports, and the newest period of the year in progress) or per quarter
  (cumulatives differenced), shares of the company's revenue, YoY, the CAGR
  over the full years, the check status and source per column and the
  reorganisation notes.

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
    """A segment name without note marks: "차량(*1)" -> "차량"; a long list of
    products ("설화수,헤라,아이오페,...") shows its first two and 등."""
    import re
    name = re.sub(r"\s*\(\*\d*\)$", "", raw or "").strip()
    parts = [p for p in re.split(r"\s*,\s*", name) if p]
    if len(name) > 16 and len(parts) > 2:
        name = ",".join(parts[:2]) + " 등"
    return name


def segment_levels(data: dict) -> list[str]:
    """The breakdowns a company has: "product" (the business section's product
    table) first, then "segment"."""
    have = {r.get("level") or "segment" for r in data.get("segments") or []
            if not str(r.get("segment_code") or "").startswith("_")}
    return [lv for lv in ("product", "segment") if lv in have]


def segment_view(data: dict, max_years: int = 6, mode: str = "y", max_quarters: int = 8,
                 level: str | None = None, keys: list | None = None) -> dict:
    """Segment revenue per period (see the module docstring):
    {"columns": [{"fiscal_year", "fiscal_q", "label", "basis", "gross", "partial", "status", "source",
                  "verified"}],
     "segments": [{"code", "name", "values": [..], "yoy": [..], "shares": [..], "cagr"}],
     "notes": [...], "currency", "mode", "level", "levels"}.

    ``keys``: the (fiscal_year, fiscal_q) of every column, given by the tab
    so the segment chart and table have the results' periods slot for slot
    (user direction 2026-11-27: one 실적 card); a period without segment data
    is an empty column. Without it the columns are the periods that have data.

    ``level``: "product" or "segment" -- one breakdown for every column, so a
    line's trend is never broken by a year read from another table; by default
    the product breakdown where there is one (user direction 2026-11-27).

    ``mode`` "y": the annual reports, plus the newest period of a year that has
    no annual report yet (``partial``). "q": one column per fiscal quarter, the
    quarter's own amount -- a cumulative figure minus the one before it in the
    same year, on the same breakdown and basis (user direction 2026-11-27: the
    business section lists cumulative amounts) -- with YoY against the same
    quarter a year earlier. ``shares`` are of the company's revenue for the
    period (the income statement's), else of the segments' sum.

    Only a period whose rows passed the check against revenue (``check_status``
    "ok") on a known basis ("q" / "ytd") is ``verified`` and enters a
    calculation -- the quarter split, a cumulative sum, YoY, CAGR. An
    unverified amount is shown as disclosed in its own column (the annual
    view, and a first quarter in the quarterly view) and nothing is derived
    from it: with Q1 unverified and the half year verified, the half-year
    cumulative shows and Q2 alone stays empty (review_codex 5 P1)."""
    levels = segment_levels(data)
    if level not in levels:
        level = levels[0] if levels else "segment"
    all_rows = [r for r in data.get("segments") or [] if (r.get("level") or "segment") == level]
    rows = [r for r in all_rows if not str(r.get("segment_code") or "").startswith("_")]
    adjust: dict = {}
    for r in all_rows:
        code = str(r.get("segment_code") or "")
        # A product table's residual is the lines it leaves out, not an elimination.
        if code.startswith("_") and r.get("revenue") is not None and not (level == "product" and code == "_residual"):
            adjust[_key(r)] = adjust.get(_key(r), 0.0) + r["revenue"]
    status = data.get("segment_status") or {}
    by_period: dict = {}
    for r in rows:
        by_period.setdefault(_key(r), []).append(r)
    revenue = {_key(r): r for r in data.get("rows") or [] if r.get("metric") == "revenue"}

    meta: dict = {}
    for (fy, fq), recs in by_period.items():
        st = status.get((fy, fq), (recs[0]["check_status"], recs[0]["source"]))
        seg_sum = sum(r["revenue"] for r in recs if r["revenue"] and r["revenue"] > 0)
        # Segments reported gross (before intersegment eliminations, which come as
        # adjustment rows) are not comparable with external-revenue segments: a
        # company whose source changes (Hyundai: the note's net table, then
        # XBRL's gross one) would show growth that is only the change of basis.
        gross = abs(adjust.get((fy, fq), 0.0)) > 0.01 * seg_sum if seg_sum else False
        if level == "product":                  # the period's status is the segment reading's
            st = ("ok", recs[0]["source"])
        verified = recs[0].get("check_status") == "ok" and recs[0]["basis"] in ("q", "ytd")
        meta[(fy, fq)] = {"basis": recs[0]["basis"], "gross": gross, "status": st[0],
                          "source": st[1] or recs[0]["source"], "level": level, "verified": verified}

    def raw(key, code):
        v = [r["revenue"] for r in by_period.get(key, []) if r["segment_code"] == code and r["revenue"] is not None]
        return sum(v) if v else None

    def same(a, b) -> bool:
        ma, mb = meta.get(a), meta.get(b)
        return bool(ma and mb) and ma["gross"] == mb["gross"] and ma["level"] == mb["level"]

    def verified(key) -> bool:
        return key in meta and meta[key]["verified"]

    def ytd_checked(key, code):
        """The cumulative amount to `key` from verified periods only (a 3-month
        figure added to the cumulative before it); None when one is not."""
        if not verified(key):
            return None
        v = raw(key, code)
        if v is None or meta[key]["basis"] == "ytd" or key[1] == 1:
            return v
        prev = (key[0], key[1] - 1)
        p = ytd_checked(prev, code) if same(prev, key) else None
        return None if p is None else p + v

    def quarter_checked(key, code):
        """The quarter's own amount from verified periods only (a cumulative
        minus the cumulative before it)."""
        if not verified(key):
            return None
        v = raw(key, code)
        if v is None or meta[key]["basis"] == "q" or key[1] == 1:
            return v
        prev = (key[0], key[1] - 1)
        p = ytd_checked(prev, code) if same(prev, key) else None
        return None if p is None else v - p

    def ytd(key, code):
        """The annual view's cell: an unverified period as disclosed."""
        return ytd_checked(key, code) if verified(key) else raw(key, code)

    def quarter(key, code):
        """The quarterly view's cell: an unverified amount only for a first
        quarter (whatever its basis, it is that quarter's); later in the year
        its basis is unknown, so it is no quarter amount."""
        if key not in meta:
            return None
        if not verified(key):
            return raw(key, code) if key[1] == 1 else None
        return quarter_checked(key, code)

    def blank():
        return {"basis": None, "gross": False, "status": None, "source": None, "level": level, "verified": False}

    if keys is not None:
        cols = []
        for fy, fq in keys:
            m = meta.get((fy, fq)) or blank()
            if mode == "q":
                label = tr(f"{fy} {fq}Q", f"{fy} Q{fq}")
            elif fq == 4:
                label = str(fy)
            else:
                label = tr(f"{fy} {fq}Q누적", f"{fy} Q{fq} YTD")
            cols.append({"fiscal_year": fy, "fiscal_q": fq, "label": label,
                         "partial": mode != "q" and fq != 4, **m})
    elif mode == "q":
        # Quarters with an amount: an annual report whose Q3 was not collected
        # (quarterly segments: the last two fiscal years) cannot be split.
        keys = [k for k in sorted(by_period)
                if any(quarter(k, r["segment_code"]) is not None for r in by_period[k])][-max_quarters:]
        cols = [{"fiscal_year": fy, "fiscal_q": fq, "label": tr(f"{fy} {fq}Q", f"{fy} Q{fq}"), "partial": False,
                 **meta[(fy, fq)]} for fy, fq in keys]
    else:
        years: dict = {}
        for (fy, fq) in by_period:             # the annual report, else the newest quarter of the year
            years[fy] = max(years.get(fy, 0), fq)
        cols = []
        for fy in sorted(years)[-max_years:]:
            fq = years[fy]
            m = meta[(fy, fq)]
            label = str(fy) if fq == 4 else tr(f"{fy} {fq}Q누적", f"{fy} Q{fq} YTD")
            cols.append({"fiscal_year": fy, "fiscal_q": fq, "label": label, "partial": fq != 4, **m})

    def value(c, code):
        key = (c["fiscal_year"], c["fiscal_q"])
        return quarter(key, code) if mode == "q" else ytd(key, code)      # annual: the cumulative

    # Codes ordered by the newest column's revenue; the newest raw name names a code.
    names: dict = {}
    for c in cols:
        for r in by_period.get((c["fiscal_year"], c["fiscal_q"]), []):
            names[r["segment_code"]] = _display_name(r["segment_raw"])
    with_data = [c for c in cols if any(value(c, code) is not None for code in names)]
    last = with_data[-1] if with_data else None
    last_vals = {code: (value(last, code) or 0) for code in names} if last else {}
    codes = sorted(names, key=lambda k: (-last_vals.get(k, 0), names[k]))
    segs = []
    for code in codes:
        values = [value(c, code) for c in cols]
        yoy = []
        for i, c in enumerate(cols):
            key = (c["fiscal_year"], c["fiscal_q"])
            if mode == "q":
                ago = (key[0] - 1, key[1])
                yoy.append(growth(quarter_checked(key, code), quarter_checked(ago, code)) if same(ago, key) else None)
                continue
            prev = cols[i - 1] if i else None
            # Exactly the year before: with a year missing in between, the change
            # over two years is not a year-on-year figure (review_codex 2 P2).
            comparable = (prev is not None and prev["verified"] and c["verified"]
                          and prev["fiscal_q"] == 4 and c["fiscal_q"] == 4
                          and prev["basis"] == c["basis"] and prev["gross"] == c["gross"]
                          and prev["level"] == c["level"] and prev["fiscal_year"] == c["fiscal_year"] - 1)
            yoy.append(growth(values[i], values[i - 1]) if comparable else None)
        cagr = None
        if mode != "q":
            # CAGR over the newest run of full years on one basis (gross or external).
            full = []
            for c, v in reversed(list(zip(cols, values))):
                if c["fiscal_q"] != 4:
                    continue
                if full and (c["gross"] != full[-1][2] or c["level"] != full[-1][3]
                             or c["fiscal_year"] != full[-1][0] - 1):
                    break                      # another basis, or a year missing: the run ends
                if v and c["verified"]:        # an unverified year is a missing one: the run ends there
                    full.append((c["fiscal_year"], v, c["gross"], c["level"]))
            full.reverse()
            if len(full) >= 2 and full[0][1] > 0 and full[-1][1] > 0 and full[-1][0] > full[0][0]:
                cagr = (full[-1][1] / full[0][1]) ** (1 / (full[-1][0] - full[0][0])) - 1
        segs.append({"code": code, "name": names[code], "values": values, "yoy": yoy, "cagr": cagr})
    for i, c in enumerate(cols):
        rev = revenue.get((c["fiscal_year"], c["fiscal_q"]))
        whole = None
        if rev is not None:
            whole = rev.get("value_q") if mode == "q" else rev.get("value_ytd")
        if not whole or whole <= 0:
            whole = sum(s["values"][i] for s in segs if s["values"][i] and s["values"][i] > 0)
        for s in segs:
            v = s["values"][i]
            s.setdefault("shares", []).append((v / whole) if (v and v > 0 and whole) else None)
    notes = []
    for u in data.get("user_maps") or []:
        notes.append(("map", u))
    for u in data.get("unmatched") or []:
        if (u.get("level") or "segment") == level:
            notes.append(("unmatched", u))
    currency = (data.get("rows") or [{}])[-1].get("currency") if data.get("rows") else "KRW"
    return {"columns": cols, "segments": segs, "notes": notes, "currency": currency or "KRW", "mode": mode,
            "level": level, "levels": levels, "has_values": bool(with_data)}


def basis_report(quarters: list[dict]) -> dict:
    """The header chips: the newest report (year, quarter, receipt date,
    consolidated or separate) and the span of quarters shown."""
    if not quarters:
        return {}
    last, first = quarters[-1], quarters[0]
    return {"fiscal_year": last["fiscal_year"], "fiscal_q": last["fiscal_q"], "rcept_dt": last["rcept_dt"],
            "fs_div": last["fs_div"], "first": (first["fiscal_year"], first["fiscal_q"]),
            "currency": last["currency"]}
