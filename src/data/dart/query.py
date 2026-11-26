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
    rcept_no, rcept_dt, yoy, qoq, turn} for a stock code (6 digits) or corp code
    (8), oldest quarter first. `as_of` is a date or "YYYY-MM-DD"."""
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
    finally:
        if own:
            conn.close()

    out = []
    for (fy, fq, metric), row in sorted(latest.items()):
        prev_year = latest.get((fy - 1, fq, metric), {}).get("value_q")
        pk = (fy, fq - 1, metric) if fq > 1 else (fy - 1, 4, metric)
        prev_q = latest.get(pk, {}).get("value_q")
        row.update(corp_code=corp, yoy=_growth(row["value_q"], prev_year), qoq=_growth(row["value_q"], prev_q),
                   turn=_turn(metric, row["value_q"], prev_year))
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
