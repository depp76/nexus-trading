"""Read side of dart.db for screens and strategies (docs/dart_db_plan_2026-11-26.md 3, 6).

``get_financials(code, as_of=...)`` returns, per fiscal quarter and metric, the
newest value whose filing was received on or before ``as_of`` (the first day a
strategy could have used it; plan 1 and 4-5), with YoY / QoQ growth and the
profit-turn flag computed over that same as-of set. Without ``as_of`` it is the
newest value overall (what ``v_fin_growth`` shows).
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import paths
from data.dart.store import connect


def _resolve_corp(conn, code: str) -> str | None:
    code = str(code).strip()
    if len(code) == 8 and code.isdigit():
        return code
    row = conn.execute("SELECT corp_code FROM company WHERE stock_code = ? ORDER BY updated_at DESC LIMIT 1",
                       (code.zfill(6),)).fetchone()
    return row[0] if row else None


def calendar_period(fiscal_year: int, fiscal_q: int, fiscal_month: int | None) -> tuple[str, int, int]:
    """(period end 'YYYY-MM', calendar year, calendar quarter) of a fiscal
    quarter (plan 4-7). Quarter 4 ends in the fiscal month of `fiscal_year`;
    each earlier quarter three months before. A quarter ending in a month that
    is not a calendar quarter end (a February year end) takes the calendar
    quarter its last month falls in."""
    idx = fiscal_year * 12 + (fiscal_month or 12) - 1 - 3 * (4 - fiscal_q)
    year, month = idx // 12, idx % 12 + 1
    return f"{year:04d}-{month:02d}", year, (month - 1) // 3 + 1


def _growth(cur, base):
    if cur is None or base is None or base <= 0:
        return None
    return cur / base - 1


def _turn(metric, cur, prev_year):
    if metric == "revenue" or cur is None or prev_year is None:
        return None
    if prev_year <= 0 < cur:
        return "turn_profit"
    if prev_year > 0 >= cur:
        return "turn_loss"
    if prev_year <= 0 and cur <= 0:
        return "loss_continued"
    return None


def get_financials(code: str, as_of=None, *, metrics=None, conn: sqlite3.Connection | None = None,
                   db_path: str | None = None) -> list[dict]:
    """Rows {fiscal_year, fiscal_q, metric, fs_div, value_q, value_ytd, derived,
    rcept_no, rcept_dt, period_end, cal_year, cal_q, yoy, qoq, turn} for a stock
    code (6 digits) or corp code (8), oldest quarter first. `as_of` is a date
    or "YYYY-MM-DD". period_end / cal_year / cal_q place a fiscal quarter on the
    calendar for non-December year ends (plan 4-7)."""
    own = conn is None
    if own:
        conn = connect(db_path or paths.DART_DB_FILE)
    try:
        corp = _resolve_corp(conn, code)
        if corp is None:
            return []
        sql = ("SELECT fiscal_year, fiscal_q, metric, fs_div, value_q, value_ytd, derived, rcept_no, rcept_dt "
               "FROM fin_std WHERE corp_code = ?")
        args: list = [corp]
        if as_of is not None:
            day = as_of.isoformat() if isinstance(as_of, (_dt.date, _dt.datetime)) else str(as_of)
            sql += " AND rcept_dt <= ?"
            args.append(day[:10])
        if metrics:
            sql += f" AND metric IN ({','.join('?' * len(metrics))})"
            args.extend(metrics)
        latest: dict = {}
        for r in conn.execute(sql + " ORDER BY rcept_no", args):
            latest[(r["fiscal_year"], r["fiscal_q"], r["metric"])] = dict(r)
        fm_row = conn.execute("SELECT fiscal_month FROM company WHERE corp_code = ?", (corp,)).fetchone()
        fiscal_month = fm_row[0] if fm_row else None
    finally:
        if own:
            conn.close()

    out = []
    for (fy, fq, metric), row in sorted(latest.items()):
        prev_year = latest.get((fy - 1, fq, metric), {}).get("value_q")
        pk = (fy, fq - 1, metric) if fq > 1 else (fy - 1, 4, metric)
        prev_q = latest.get(pk, {}).get("value_q")
        period_end, cal_year, cal_q = calendar_period(fy, fq, fiscal_month)
        row.update(corp_code=corp, yoy=_growth(row["value_q"], prev_year), qoq=_growth(row["value_q"], prev_q),
                   turn=_turn(metric, row["value_q"], prev_year),
                   period_end=period_end, cal_year=cal_year, cal_q=cal_q)
        out.append(row)
    return out


def get_growth(code: str, *, conn: sqlite3.Connection | None = None, db_path: str | None = None) -> list[dict]:
    """``v_fin_growth`` rows of one company (newest values), oldest quarter first."""
    own = conn is None
    if own:
        conn = connect(db_path or paths.DART_DB_FILE)
    try:
        corp = _resolve_corp(conn, code)
        if corp is None:
            return []
        return [dict(r) for r in conn.execute(
            "SELECT * FROM v_fin_growth WHERE corp_code = ? ORDER BY fiscal_year, fiscal_q, metric", (corp,))]
    finally:
        if own:
            conn.close()


# ---- text reports (tools/dart_sync.py --show / --issues) ---------------------------

def growth_table(code: str, as_of=None, *, quarters: int = 12, conn: sqlite3.Connection | None = None,
                 db_path: str | None = None) -> list[dict]:
    """One row per fiscal quarter, newest last (at most `quarters`): revenue,
    operating income, owners' (else total) net income with YoY / QoQ, the
    operating margin, the operating-income turn flag, and whether a value is
    derived (Q4 = annual - Q3). Plan 3단계 "증감표"."""
    rows = get_financials(code, as_of, conn=conn, db_path=db_path)
    by_q: dict = {}
    for r in rows:
        by_q.setdefault((r["fiscal_year"], r["fiscal_q"]), {})[r["metric"]] = r
    out = []
    for (fy, fq), m in sorted(by_q.items())[-quarters:]:
        rev, op = m.get("revenue"), m.get("op_income")
        ni = m.get("net_income_owners") or m.get("net_income")
        present = [x for x in (rev, op, ni) if x]
        if not present:
            continue
        margin = None
        if op and rev and op["value_q"] is not None and rev["value_q"]:
            margin = op["value_q"] / rev["value_q"]
        out.append({
            "fiscal_year": fy, "fiscal_q": fq, "period_end": present[0]["period_end"],
            "revenue": rev["value_q"] if rev else None, "revenue_yoy": rev["yoy"] if rev else None,
            "revenue_qoq": rev["qoq"] if rev else None,
            "op_income": op["value_q"] if op else None, "op_yoy": op["yoy"] if op else None,
            "op_turn": op["turn"] if op else None, "op_margin": margin,
            "net_income": ni["value_q"] if ni else None, "net_yoy": ni["yoy"] if ni else None,
            "derived": any(x["derived"] for x in present),
            "fs_div": present[0]["fs_div"], "rcept_dt": max(x["rcept_dt"] for x in present),
        })
    return out


def _eok(v) -> str:
    return "-" if v is None else f"{v / 1e8:,.0f}"


def _pct(v) -> str:
    return "" if v is None else f"{v * 100:+.0f}%"


_TURN = {"turn_profit": "흑자전환", "turn_loss": "적자전환", "loss_continued": "적자지속"}


def format_growth_table(code: str, as_of=None, **kw) -> str:
    """growth_table() as text, amounts in 억원."""
    rows = growth_table(code, as_of, **kw)
    if not rows:
        return f"{code}: no data in dart.db"
    title = f"{code} (억원, {rows[-1]['fs_div']}" + (f", as of {as_of}" if as_of else "") + ")"
    head = (f"{'분기':<8}{'기간말':<9}{'매출':>11}{'YoY':>7}{'QoQ':>7}{'영업이익':>10}{'YoY':>7}{'이익률':>7}"
            f"{'순이익':>10}{'YoY':>7}  {'접수일':<11}비고")
    lines = [title, head]
    for r in rows:
        note = " ".join(x for x in (_TURN.get(r["op_turn"] or "", ""), "†계산값" if r["derived"] else "") if x)
        margin = "" if r["op_margin"] is None else f"{r['op_margin'] * 100:.1f}%"
        quarter = f"{r['fiscal_year']}Q{r['fiscal_q']}"
        lines.append(f"{quarter:<8}{r['period_end']:<9}{_eok(r['revenue']):>11}{_pct(r['revenue_yoy']):>7}"
                     f"{_pct(r['revenue_qoq']):>7}{_eok(r['op_income']):>10}{_pct(r['op_yoy']):>7}{margin:>7}"
                     f"{_eok(r['net_income']):>10}{_pct(r['net_yoy']):>7}  {r['rcept_dt']:<11}{note}")
    return "\n".join(lines)


def issues(conn: sqlite3.Connection | None = None, db_path: str | None = None) -> dict:
    """What collection could not do cleanly (plan 4-1 / 4-4): counts per kind,
    the unmapped metrics, and the fs_div changes / no-data periods / Q4 values
    resting on a later Q3."""
    own = conn is None
    if own:
        conn = connect(db_path or paths.DART_DB_FILE)
    try:
        def q(sql, *a):
            return [dict(r) for r in conn.execute(sql, a)]
        return {
            "counts": q("SELECT kind, COUNT(*) AS n, COUNT(DISTINCT corp_code) AS companies FROM fin_issue "
                        "GROUP BY kind ORDER BY kind")
                      + q("SELECT 'unmapped' AS kind, COUNT(*) AS n, COUNT(DISTINCT corp_code) AS companies "
                          "FROM fin_unmapped"),
            "unmapped": q("SELECT c.stock_code, c.corp_name, f.bsns_year AS fiscal_year, f.reprt_code, u.metric, "
                          "u.rcept_no FROM fin_unmapped u JOIN company c ON c.corp_code = u.corp_code "
                          "JOIN filing f ON f.rcept_no = u.rcept_no ORDER BY c.corp_name, u.rcept_no, u.metric"),
            "listed": q("SELECT i.kind, c.stock_code, c.corp_name, i.fiscal_year, i.fiscal_q, i.detail, i.rcept_no "
                        "FROM fin_issue i JOIN company c ON c.corp_code = i.corp_code "
                        "WHERE i.kind IN ('fs_div_change', 'no_data', 'q4_uses_later_q3') "
                        "ORDER BY i.kind, c.corp_name, i.fiscal_year, i.fiscal_q"),
        }
    finally:
        if own:
            conn.close()


ISSUE_LABELS = {
    "unmapped": "계정 미매핑 (account_map.json으로 보완)",
    "fs_div_change": "연결/별도 구분 변경",
    "no_data": "데이터 없음 (API 013, 원문 표도 없음)",
    "from_document": "원문 표에서 읽음 (API 013)",
    "revenue_na": "금융업: 매출 해당 없음",
    "owners_from_net_income": "비지배지분 없음: 지배주주순이익 = 순이익",
    "q4_uses_later_q3": "4분기 계산에 사업보고서 이후 정정된 3분기 사용",
}


def format_issues(limit: int = 50, **kw) -> str:
    """issues() as text."""
    rep = issues(**kw)
    lines = ["== 요약"]
    for c in rep["counts"]:
        lines.append(f"  {ISSUE_LABELS.get(c['kind'], c['kind'])}: {c['n']:,}건 ({c['companies']:,}개사)")
    if rep["unmapped"]:
        lines.append(f"== {ISSUE_LABELS['unmapped']} (최대 {limit}건)")
        for r in rep["unmapped"][:limit]:
            lines.append(f"  {r['stock_code']} {r['corp_name']} {r['fiscal_year']} {r['reprt_code']} "
                         f"{r['metric']} ({r['rcept_no']})")
    for kind in ("fs_div_change", "no_data", "q4_uses_later_q3"):
        items = [r for r in rep["listed"] if r["kind"] == kind]
        if items:
            lines.append(f"== {ISSUE_LABELS[kind]} (최대 {limit}건)")
            for r in items[:limit]:
                lines.append(f"  {r['stock_code']} {r['corp_name']} {r['fiscal_year']}Q{r['fiscal_q']} "
                             f"{r['detail']} ({r['rcept_no']})")
    return "\n".join(lines)
