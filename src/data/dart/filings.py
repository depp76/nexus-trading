"""list.json -> filing (docs/dart_db_plan_2026-11-26.md 2, 3, 4-5, 4-7).

Periodic reports (``pblntf_ty=A``) of KOSPI (``corp_cls=Y``) and KOSDAQ (``K``)
companies. Without a corp code DART limits a search to three months, so the
market-wide backfill walks calendar quarters; a per-company search (``--tickers``)
has no such limit.

Fiscal year / quarter (plan 4-7): a report name carries its period end, e.g.
"분기보고서 (2025.09)". With the company's fiscal (year-end) month m, a period
ending in month p is quarter ((p - m) mod 12) / 3 (0 -> 4) of the fiscal year
that ends in the calendar year of p (p <= m) or the next one (p > m). The fiscal
month is learnt from the company's annual reports ("사업보고서 (2025.03)" -> 3);
12 until one is seen, and a company's filings are re-classified when it changes.

Corrections (plan 4-5): every report keeps its own row; within one
(company, fiscal year, report code) all but the newest receipt number get
``superseded_by`` = the newest.
"""
from __future__ import annotations

import datetime as _dt
import re

from data.dart.store import now_iso

REPRT_ANNUAL, REPRT_HALF, REPRT_Q1, REPRT_Q3 = "11011", "11012", "11013", "11014"
REPRT_BY_Q = {1: REPRT_Q1, 2: REPRT_HALF, 3: REPRT_Q3, 4: REPRT_ANNUAL}
Q_BY_REPRT = {v: k for k, v in REPRT_BY_Q.items()}
MARKET_BY_CLS = {"Y": "KOSPI", "K": "KOSDAQ"}

_NAME_RE = re.compile(r"^\s*((?:\[[^\]]*\]\s*)*)(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")
_KIND_ANNUAL = "사업보고서"


def parse_report_name(name: str):
    """(kind, period year, period month, correction tags) of a periodic report
    name, or None for any other report (결산서류 etc.)."""
    m = _NAME_RE.match(name or "")
    if not m:
        return None
    tags = re.findall(r"\[([^\]]*)\]", m.group(1))
    return m.group(2), int(m.group(3)), int(m.group(4)), tags


def classify(kind: str, period_year: int, period_month: int, fiscal_month: int):
    """(fiscal_year, fiscal_q, reprt_code) of a report; see the module docstring."""
    fm = fiscal_month or 12
    if kind == _KIND_ANNUAL:
        q = 4
    else:
        q = ((period_month - fm) % 12) // 3 or 4
        if q == 4:          # a quarterly report ending on the year-end month: treat as Q3 of a short year
            q = 3
        if kind == "반기보고서":
            q = 2
    fiscal_year = period_year if period_month <= fm else period_year + 1
    return fiscal_year, q, REPRT_BY_Q[q]


def quarter_windows(start: _dt.date, end: _dt.date) -> list[tuple[_dt.date, _dt.date]]:
    """Calendar-quarter windows [(first day, last day)] covering start..end."""
    out = []
    y, q0 = start.year, (start.month - 1) // 3
    while True:
        first = _dt.date(y, q0 * 3 + 1, 1)
        if first > end:
            break
        nxt = _dt.date(y + (q0 == 3), (q0 + 1) % 4 * 3 + 1, 1)
        out.append((max(first, start), min(nxt - _dt.timedelta(days=1), end)))
        y, q0 = nxt.year, (nxt.month - 1) // 3
    return out


def fetch_filing_list(client, bgn: _dt.date, end: _dt.date, *, corp_cls: str | None = None,
                      corp_code: str | None = None, cacheable: bool = False, check=None) -> list[dict]:
    """Every periodic-report row DART lists for the window (all pages). Past
    windows (`cacheable`) are served from the cache on a rerun. `check()` runs
    before every page (run_sync's cancel check: a market-wide quarter is ~15
    pages, too long to wait for when the app closes)."""
    rows, page = [], 1
    while True:
        if check:
            check()
        params = {"bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                  "pblntf_ty": "A", "last_reprt_at": "N", "page_no": page, "page_count": 100}
        if corp_cls:
            params["corp_cls"] = corp_cls
        if corp_code:
            params["corp_code"] = corp_code
        data = client.get_json("list.json", params, cache_tag="list" if cacheable else None)
        if data.get("status") != "000":
            break
        rows.extend(data.get("list") or [])
        if page >= int(data.get("total_page") or 1):
            break
        page += 1
    return rows


# Filings are kept from one fiscal year before the window: a non-December
# filer's first reports arrive before its first annual report tells the fiscal
# month, so they are classified with month 12 at first and can look a year too
# early ("분기보고서 (2020.12)" of a March year end is FY2021 Q3). Dropping them
# then would lose them for good; the financial fetch still starts at the
# window's first year (review_agy H4).
KEEP_YEARS_BEFORE = 1


def store_filings(conn, rows: list[dict], first_year: int) -> int:
    """Store the periodic reports among `rows` (list.json items) whose fiscal
    year is `first_year - KEEP_YEARS_BEFORE` or later, with their companies;
    returns new filings. Rows are taken oldest first (list.json answers newest
    first), so a company's name and market end as its newest report has them
    (review_agy M8)."""
    periodic = []
    for r in rows:
        parsed = parse_report_name(r.get("report_nm", ""))
        if parsed and r.get("corp_code") and r.get("rcept_no"):
            periodic.append((r, parsed))
    if not periodic:
        return 0
    periodic.sort(key=lambda x: str(x[0]["rcept_no"]))

    now = now_iso()
    touched_fm = set()
    for r, (kind, _py, pm, _tags) in periodic:
        market = MARKET_BY_CLS.get(r.get("corp_cls", ""))
        stock = (r.get("stock_code") or "").strip() or None
        conn.execute(
            "INSERT INTO company(corp_code, stock_code, corp_name, market, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(corp_code) DO UPDATE SET stock_code = COALESCE(excluded.stock_code, company.stock_code), "
            "corp_name = excluded.corp_name, market = COALESCE(excluded.market, company.market), "
            "updated_at = excluded.updated_at",
            (r["corp_code"], stock, r.get("corp_name") or r["corp_code"], market, now))
        if kind == _KIND_ANNUAL:
            cur = conn.execute("SELECT fiscal_month FROM company WHERE corp_code = ?", (r["corp_code"],)).fetchone()
            if cur[0] != pm:
                conn.execute("UPDATE company SET fiscal_month = ? WHERE corp_code = ?", (pm, r["corp_code"]))
                touched_fm.add(r["corp_code"])

    added = 0
    corps = set()
    for r, (kind, py, pm, tags) in periodic:
        fm = _fiscal_month(conn, r["corp_code"])
        fy, _q, reprt = classify(kind, py, pm, fm)
        if fy < first_year - KEEP_YEARS_BEFORE:
            continue
        rcept_dt = _fmt_date(r.get("rcept_dt") or r["rcept_no"][:8])
        cur = conn.execute(
            "INSERT OR IGNORE INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt, report_nm, is_correction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["rcept_no"], r["corp_code"], reprt, fy, rcept_dt, r.get("report_nm"), int(bool(tags))))
        added += cur.rowcount
        corps.add(r["corp_code"])
    for corp in touched_fm:
        reclassify_company(conn, corp)
    for corp in corps | touched_fm:
        link_corrections(conn, corp)
    return added


def reclassify_company(conn, corp_code: str) -> None:
    """Recompute bsns_year / reprt_code of a company's filings after its fiscal
    month changed. A filing whose period moved loses what was standardised
    under the old period (fin_std, issues, segment rows, the fetch marks), so
    the next run stores it again under the right fiscal year / quarter
    (review_agy H4)."""
    fm = _fiscal_month(conn, corp_code)
    for row in conn.execute("SELECT rcept_no, report_nm, bsns_year, reprt_code FROM filing WHERE corp_code = ?",
                            (corp_code,)).fetchall():
        parsed = parse_report_name(row["report_nm"] or "")
        if not parsed:
            continue
        fy, _q, reprt = classify(parsed[0], parsed[1], parsed[2], fm)
        if (fy, reprt) == (row["bsns_year"], row["reprt_code"]):
            continue
        r = row["rcept_no"]
        conn.execute("UPDATE filing SET bsns_year = ?, reprt_code = ? WHERE rcept_no = ?", (fy, reprt, r))
        conn.execute("DELETE FROM fin_std WHERE rcept_no = ?", (r,))
        conn.execute("DELETE FROM fin_issue WHERE rcept_no = ?", (r,))
        conn.execute("DELETE FROM fin_unmapped WHERE rcept_no = ?", (r,))
        conn.execute("DELETE FROM fin_fetch WHERE rcept_no = ? OR src_rcept_no = ?", (r, r))
        # Segment amounts sit under the old fiscal quarter and their fetch mark
        # would keep the right quarter from ever being read (review_codex 2 P2).
        conn.execute("DELETE FROM segment_rev WHERE rcept_no = ?", (r,))
        conn.execute("DELETE FROM seg_fetch WHERE rcept_no = ?", (r,))


def link_corrections(conn, corp_code: str) -> None:
    """superseded_by = the newest receipt of the same period, NULL on the newest itself."""
    conn.execute(
        "UPDATE filing SET superseded_by = ("
        "  SELECT MAX(f2.rcept_no) FROM filing f2 WHERE f2.corp_code = filing.corp_code "
        "  AND f2.bsns_year = filing.bsns_year AND f2.reprt_code = filing.reprt_code) "
        "WHERE corp_code = ?", (corp_code,))
    conn.execute("UPDATE filing SET superseded_by = NULL WHERE corp_code = ? AND superseded_by = rcept_no",
                 (corp_code,))


def ensure_filing(conn, rcept_no: str, corp_code: str, fiscal_year: int, reprt_code: str) -> bool:
    """Add a stub filing for a receipt number the financial API answered with but
    the filing list did not contain (its date is the receipt number's first eight
    digits); True when a row was added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt, report_nm, is_correction) "
        "VALUES (?, ?, ?, ?, ?, NULL, 0)",
        (rcept_no, corp_code, reprt_code, fiscal_year, _fmt_date(rcept_no[:8])))
    if cur.rowcount:
        link_corrections(conn, corp_code)
    return bool(cur.rowcount)


def _fiscal_month(conn, corp_code: str) -> int:
    row = conn.execute("SELECT fiscal_month FROM company WHERE corp_code = ?", (corp_code,)).fetchone()
    return int(row[0]) if row and row[0] else 12


def _fmt_date(yyyymmdd: str) -> str:
    s = str(yyyymmdd).replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
