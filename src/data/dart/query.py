"""Read side of dart.db for screens and strategies (docs/dart_db_plan_2026-11-26.md 3, 6).

``get_financials(code, as_of=...)`` returns, per fiscal quarter and metric, the
newest value whose filing was received on or before ``as_of`` (the first day a
strategy could have used it; plan 1 and 4-5), with YoY / QoQ growth and the
profit-turn flag computed over that same as-of set. Without ``as_of`` it is the
newest value overall (what ``v_fin_growth`` shows).

Every function here reads through ``store.open_readonly``: nothing is written
and no write lock is taken, so a query never waits for (or disturbs) a running
collector, and a missing dart.db is simply "no data" (review_agy H1).
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import sqlite3

import paths
from data.dart.store import open_readonly


@contextlib.contextmanager
def _reader(conn: sqlite3.Connection | None, db_path: str | None):
    """`conn` as given, else a read-only connection closed on exit (None: no DB yet)."""
    if conn is not None:
        yield conn
        return
    own = open_readonly(db_path or paths.DART_DB_FILE)
    try:
        yield own
    finally:
        if own is not None:
            own.close()


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
    """Rows {fiscal_year, fiscal_q, metric, fs_div, currency, value_q, value_ytd,
    derived, rcept_no, rcept_dt, period_end, cal_year, cal_q, yoy, qoq,
    turn, fs_div_changed, currency_changed, lookahead} for a stock code (6
    digits) or corp code (8), oldest quarter first. `as_of` is a date or
    "YYYY-MM-DD".

    - period_end / cal_year / cal_q place a fiscal quarter on the calendar for
      non-December year ends (plan 4-7).
    - Growth and the turn flag compare like with like only: with a base
      quarter of another statement kind (OFS -> CFS) or reporting currency,
      yoy / qoq / turn are None and ``fs_div_changed`` / ``currency_changed``
      is True (review_agy M6, review_codex P1).
    - A Q4 is the annual total minus the Q3 cumulative, one stored row per
      version: under the annual receipt (with the Q3 known that day; value_q
      None when there was none yet) and under each Q3 correction received
      later (``lookahead`` True on those). The newest row received by `as_of`
      is returned, so a past as-of answer never changes when later filings are
      loaded (review_codex P1, review_codex 2 P1)."""
    with _reader(conn, db_path) as db:
        if db is None:
            return []
        corp = _resolve_corp(db, code)
        if corp is None:
            return []
        cols = {r[1] for r in db.execute("PRAGMA table_info(fin_std)")}
        currency = "COALESCE(currency, 'KRW') AS currency" if "currency" in cols else "'KRW' AS currency"
        sql = (f"SELECT fiscal_year, fiscal_q, metric, fs_div, {currency}, value_q, value_ytd, derived, rcept_no, "
               "rcept_dt FROM fin_std WHERE corp_code = ?")
        args: list = [corp]
        if as_of is not None:
            day = (as_of.isoformat() if isinstance(as_of, (_dt.date, _dt.datetime)) else str(as_of))[:10]
            sql += " AND rcept_dt <= ?"
            args.append(day)
        if metrics:
            sql += f" AND metric IN ({','.join('?' * len(metrics))})"
            args.extend(metrics)
        latest: dict = {}
        for r in db.execute(sql + " ORDER BY rcept_no", args):
            latest[(r["fiscal_year"], r["fiscal_q"], r["metric"])] = dict(r)
        fm_row = db.execute("SELECT fiscal_month FROM company WHERE corp_code = ?", (corp,)).fetchone()
        fiscal_month = fm_row[0] if fm_row else None

    for row in latest.values():
        row["lookahead"] = row["derived"] == 2      # a Q4 restated by a Q3 received after the annual report

    def base(key, row):
        """(the base quarter's value, statement kind differs, currency differs)."""
        b = latest.get(key)
        if b is None:
            return None, False, False
        fs, cur = b["fs_div"] != row["fs_div"], b["currency"] != row["currency"]
        return (None if (fs or cur) else b["value_q"]), fs, cur

    out = []
    for (fy, fq, metric), row in sorted(latest.items()):
        prev_year, fs_changed, cur_changed = base((fy - 1, fq, metric), row)
        prev_q, _, _ = base((fy, fq - 1, metric) if fq > 1 else (fy - 1, 4, metric), row)
        period_end, cal_year, cal_q = calendar_period(fy, fq, fiscal_month)
        row.update(corp_code=corp, yoy=_growth(row["value_q"], prev_year), qoq=_growth(row["value_q"], prev_q),
                   turn=_turn(metric, row["value_q"], prev_year), fs_div_changed=fs_changed,
                   currency_changed=cur_changed, period_end=period_end, cal_year=cal_year, cal_q=cal_q)
        out.append(row)
    return out


def get_growth(code: str, *, conn: sqlite3.Connection | None = None, db_path: str | None = None) -> list[dict]:
    """``v_fin_growth`` rows of one company (newest values), oldest quarter first."""
    with _reader(conn, db_path) as db:
        if db is None:
            return []
        corp = _resolve_corp(db, code)
        if corp is None:
            return []
        return [dict(r) for r in db.execute(
            "SELECT * FROM v_fin_growth WHERE corp_code = ? ORDER BY fiscal_year, fiscal_q, metric", (corp,))]


# ---- text reports (tools/dart_sync.py --show / --issues) ---------------------------

def growth_table(code: str, as_of=None, *, quarters: int = 12, conn: sqlite3.Connection | None = None,
                 db_path: str | None = None) -> list[dict]:
    """One row per fiscal quarter, newest last (at most `quarters`): revenue,
    operating income, owners' (else total) net income with YoY / QoQ, the
    operating margin, the operating-income turn flag, and whether a value is
    derived (Q4 = annual - Q3), rests on later numbers (lookahead) or follows a
    change of statement kind / reporting currency. Each row carries its own
    currency. Plan 3단계 "증감표"."""
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
            "lookahead": any(x["lookahead"] for x in present),
            "fs_div_changed": any(x["fs_div_changed"] for x in present),
            "currency_changed": any(x["currency_changed"] for x in present),
            "fs_div": present[0]["fs_div"], "currency": present[0]["currency"] or "KRW",
            "rcept_dt": max(x["rcept_dt"] for x in present),
        })
    return out


def _unit(currency: str) -> tuple[float, str]:
    """(divisor, label): 억원 for KRW; a foreign reporting currency is shown in
    its own millions, never converted (plan 4-6)."""
    return (1e8, "억원") if (currency or "KRW") == "KRW" else (1e6, f"백만 {currency}")


def _amt(v, div: float) -> str:
    return "-" if v is None else f"{v / div:,.0f}"


def _pct(v) -> str:
    return "" if v is None else f"{v * 100:+.0f}%"


_TURN = {"turn_profit": "흑자전환", "turn_loss": "적자전환", "loss_continued": "적자지속"}


def format_growth_table(code: str, as_of=None, **kw) -> str:
    """growth_table() as text. Amounts are in 억원, or millions of the row's own
    reporting currency: a row in another currency than the newest one says so
    in its note instead of being shown in the wrong unit (review_codex P1)."""
    rows = growth_table(code, as_of, **kw)
    if not rows:
        return f"{code}: no data in dart.db"
    _div, unit = _unit(rows[-1]["currency"])
    title = f"{code} ({unit}, {rows[-1]['fs_div']}" + (f", as of {as_of}" if as_of else "") + ")"
    head = (f"{'분기':<8}{'기간말':<9}{'매출':>11}{'YoY':>7}{'QoQ':>7}{'영업이익':>10}{'YoY':>7}{'이익률':>7}"
            f"{'순이익':>10}{'YoY':>7}  {'접수일':<11}비고")
    lines = [title, head]
    for r in rows:
        div, row_unit = _unit(r["currency"])
        notes = (f"단위 {row_unit}" if row_unit != unit else "", _TURN.get(r["op_turn"] or "", ""),
                 "†계산값" if r["derived"] else "", "‡이후 정정 3분기 사용" if r["lookahead"] else "",
                 "연결/별도 변경" if r["fs_div_changed"] else "", "통화 변경" if r["currency_changed"] else "")
        note = " ".join(x for x in notes if x)
        margin = "" if r["op_margin"] is None else f"{r['op_margin'] * 100:.1f}%"
        quarter = f"{r['fiscal_year']}Q{r['fiscal_q']}"
        lines.append(f"{quarter:<8}{r['period_end']:<9}{_amt(r['revenue'], div):>11}{_pct(r['revenue_yoy']):>7}"
                     f"{_pct(r['revenue_qoq']):>7}{_amt(r['op_income'], div):>10}{_pct(r['op_yoy']):>7}{margin:>7}"
                     f"{_amt(r['net_income'], div):>10}{_pct(r['net_yoy']):>7}  {r['rcept_dt']:<11}{note}")
    return "\n".join(lines)


def issues(conn: sqlite3.Connection | None = None, db_path: str | None = None) -> dict:
    """What collection could not do cleanly (plan 4-1 / 4-4): counts per kind,
    the unmapped metrics, and the fs_div changes / no-data periods / Q4 values
    resting on a later Q3."""
    with _reader(conn, db_path) as db:
        if db is None:
            return {"counts": [], "unmapped": [], "listed": []}

        def q(sql, *a):
            return [dict(r) for r in db.execute(sql, a)]
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


# ---- segments (stage 4, plan 5) -----------------------------------------------------

def get_segments(code: str, as_of=None, *, include_adjustments: bool = False,
                 conn: sqlite3.Connection | None = None, db_path: str | None = None) -> list[dict]:
    """Revenue by segment, oldest quarter first: {fiscal_year, fiscal_q,
    segment_raw, segment_code, revenue, op_income, basis, source,
    check_status, rcept_no, rcept_dt}. ``basis`` says whether revenue is the
    quarter's ('q') or the cumulative year-to-date amount ('ytd'). Adjustment
    rows (eliminations, the difference to the reported total) only with
    `include_adjustments`.

    For each fiscal quarter the rows of the newest receipt *processed* by
    `as_of` -- the newest in seg_fetch, not the newest that happens to have
    amounts: when a correction was read and no segment table was found in it,
    the period has no rows (its status says "review") instead of showing the
    original report's amounts as if they were current (review_codex 2 P2).
    ``segment_periods`` gives that receipt and status per period."""
    with _reader(conn, db_path) as db:
        if db is None:
            return []
        corp = _resolve_corp(db, code)
        if corp is None:
            return []
        newest = segment_periods(corp, as_of, conn=db)
        rows = [dict(r) for r in db.execute("SELECT * FROM segment_rev WHERE corp_code = ?", (corp,))]
    out = [r for r in rows
           if newest.get((r["fiscal_year"], r["fiscal_q"]), {}).get("rcept_no") == r["rcept_no"]
           and (include_adjustments or not str(r["segment_code"] or "").startswith("_"))]
    out.sort(key=lambda r: (r["fiscal_year"], r["fiscal_q"], -(r["revenue"] or 0)))
    return out


def segment_periods(code: str, as_of=None, *, conn: sqlite3.Connection | None = None,
                    db_path: str | None = None) -> dict:
    """{(fiscal_year, fiscal_q): {rcept_no, rcept_dt, status, source}}: per
    period the newest receipt whose segments were processed and received by
    `as_of` (a receipt with rows but no seg_fetch mark counts too)."""
    with _reader(conn, db_path) as db:
        if db is None:
            return {}
        corp = _resolve_corp(db, code)
        if corp is None:
            return {}
        day = None
        if as_of is not None:
            day = (as_of.isoformat() if isinstance(as_of, (_dt.date, _dt.datetime)) else str(as_of))[:10]
        q_of = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
        out: dict = {}

        def take(key, rcept_no, rcept_dt, status, source):
            if day is not None and rcept_dt > day:
                return
            if rcept_no > out.get(key, {}).get("rcept_no", ""):
                out[key] = {"rcept_no": rcept_no, "rcept_dt": rcept_dt, "status": status, "source": source}

        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "seg_fetch" in tables:
            for r in db.execute("SELECT f.bsns_year, f.reprt_code, s.rcept_no, f.rcept_dt, s.status, s.source "
                                "FROM seg_fetch s JOIN filing f ON f.rcept_no = s.rcept_no WHERE f.corp_code = ?",
                                (corp,)):
                take((r[0], q_of.get(r[1], 0)), r[2], r[3], r[4], r[5])
        for r in db.execute("SELECT DISTINCT fiscal_year, fiscal_q, rcept_no, rcept_dt, check_status, source "
                            "FROM segment_rev WHERE corp_code = ?", (corp,)):
            take((r[0], r[1]), r[2], r[3], r[4], r[5])
        return out


def segment_report(tickers=None, conn: sqlite3.Connection | None = None, db_path: str | None = None) -> dict:
    """Per company: periods read and their check status / source counts, the
    pass rate (plan 7, stage 4 completion), and the segment names that do not
    run through all years (to map by hand in segment_map, plan 5-4)."""
    from data.dart.segments import unmatched_codes
    with _reader(conn, db_path) as db:
        if db is None:
            return {"companies": [], "unmatched": {}}
        sql = ("SELECT c.corp_code, c.stock_code, c.corp_name, s.status, s.source, COUNT(*) AS n FROM seg_fetch s "
               "JOIN filing f ON f.rcept_no = s.rcept_no JOIN company c ON c.corp_code = f.corp_code")
        args: list = []
        if tickers:
            sql += f" WHERE c.stock_code IN ({','.join('?' * len(tickers))})"
            args = [str(t).zfill(6) for t in tickers]
        by: dict = {}
        for r in db.execute(sql + " GROUP BY 1, 2, 3, 4, 5", args):
            d = by.setdefault(r["corp_code"], {"stock_code": r["stock_code"], "corp_name": r["corp_name"],
                                               "status": {}, "source": {}})
            d["status"][r["status"]] = d["status"].get(r["status"], 0) + r["n"]
            if r["source"]:
                d["source"][r["source"]] = d["source"].get(r["source"], 0) + r["n"]
        unmatched = {corp: unmatched_codes(db, corp) for corp in by}
    companies = []
    for corp, d in by.items():
        st = d["status"]
        checked = sum(n for k, n in st.items() if k != "na")
        d["periods"] = sum(st.values())
        d["pass_rate"] = (st.get("ok", 0) / checked) if checked else None
        d["corp_code"] = corp
        companies.append(d)
    companies.sort(key=lambda d: d["stock_code"] or "")
    return {"companies": companies, "unmatched": {k: v for k, v in unmatched.items() if v}}


_SOURCE_LABEL = {"xbrl": "XBRL", "html_segment": "원문 부문표", "html_product": "원문 제품표", "single": "단일부문"}


def format_segment_report(tickers=None, **kw) -> str:
    """segment_report() as text: the check table per company, the totals and
    the names to map."""
    rep = segment_report(tickers, **kw)
    if not rep["companies"]:
        return "no segment data in dart.db (run tools/dart_sync.py --segments)"
    lines = [f"{'종목':<8}{'회사':<14}{'기간':>4}{'ok':>5}{'불일치':>7}{'review':>8}{'해당없음':>8}{'통과율':>8}  출처",
             "-" * 92]
    tot: dict = {}
    for d in rep["companies"]:
        st = d["status"]
        for k, n in st.items():
            tot[k] = tot.get(k, 0) + n
        rate = "-" if d["pass_rate"] is None else f"{d['pass_rate'] * 100:.0f}%"
        src = ", ".join(f"{_SOURCE_LABEL.get(k, k)} {n}" for k, n in sorted(d["source"].items(), key=lambda x: -x[1]))
        lines.append(f"{d['stock_code']:<8}{d['corp_name'][:12]:<14}{d['periods']:>4}{st.get('ok', 0):>5}"
                     f"{st.get('sum_mismatch', 0):>7}{st.get('review', 0):>8}{st.get('na', 0):>8}{rate:>8}  {src}")
    checked = sum(n for k, n in tot.items() if k != "na")
    lines.append("-" * 92)
    lines.append(f"합계 {len(rep['companies'])}개사 · 기간 {sum(tot.values())} · ok {tot.get('ok', 0)} · 불일치 "
                 f"{tot.get('sum_mismatch', 0)} · review {tot.get('review', 0)} · 해당없음 {tot.get('na', 0)} · 통과율 "
                 + (f"{tot.get('ok', 0) / checked * 100:.0f}%" if checked else "-"))
    names = {d["corp_code"]: (d["stock_code"], d["corp_name"]) for d in rep["companies"]}
    lines.append("")
    lines.append("== 매핑 안 된 부문명 (모든 연도에 이어지지 않는 이름: segment_map에서 묶을 후보)")
    if not rep["unmatched"]:
        lines.append("  없음")
    for corp, items in rep["unmatched"].items():
        code, name = names[corp]
        for it in items:
            lines.append(f"  {code} {name}: '{it['segment_raw']}' (코드 {it['segment_code']}) "
                         f"있는 해 {','.join(map(str, it['years']))} / 없는 해 {','.join(map(str, it['missing']))}")
    return "\n".join(lines)


# ---- the MA chart's 재무 tab (stage 5, plan 13) --------------------------------------

def company_financials(code: str, conn: sqlite3.Connection | None = None,
                       db_path: str | None = None) -> dict | None:
    """Everything the 재무 tab shows for one stock, read in one go (read-only):
    the company, its quarterly rows (get_financials, all metrics), its segment
    rows, the segment check status per period, segment names that do not run
    through the years and the user's own segment_map rows, the receipts that
    are corrections, and the last collection run. None when the stock is not
    in dart.db (or there is no dart.db yet)."""
    from data.dart.segments import unmatched_codes
    with _reader(conn, db_path) as db:
        if db is None:
            return None
        corp = _resolve_corp(db, code)
        if corp is None:
            return None
        comp = db.execute("SELECT corp_code, stock_code, corp_name, market, fiscal_month FROM company "
                          "WHERE corp_code = ?", (corp,)).fetchone()
        # Amounts and status of one and the same receipt per period (review_codex 2 P2).
        seg_status = {k: (v["status"], v["source"]) for k, v in segment_periods(corp, conn=db).items()}
        state = dict(db.execute("SELECT key, value FROM sync_state WHERE key IN ('last_run', 'last_run_status')"))
        out = {
            "company": dict(comp) if comp else {"corp_code": corp},
            "rows": get_financials(corp, conn=db),
            "segments": get_segments(corp, include_adjustments=True, conn=db),
            "segment_status": seg_status,
            "unmatched": unmatched_codes(db, corp),
            "user_maps": [dict(r) for r in db.execute(
                "SELECT segment_raw, segment_code, valid_from, valid_to, note FROM segment_map "
                "WHERE corp_code = ? AND note <> 'auto' ORDER BY segment_code, valid_from", (corp,))],
            "corrections": {r[0] for r in db.execute(
                "SELECT rcept_no FROM filing WHERE corp_code = ? AND is_correction = 1", (corp,))},
            "last_run": state.get("last_run"),
            "last_run_status": state.get("last_run_status"),
        }
    return out
