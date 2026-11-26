"""dart.db: schema, connection and small write helpers (docs/dart_db_plan_2026-11-26.md 3).

Every table of plan section 3 is created here, plus two bookkeeping tables the
plan's collection rules need (recorded in plan 3, "구현 메모"):

- ``fin_fetch``: one row per filing whose financials were requested, so a run
  that stops anywhere resumes with the filings still missing (plan 3: commit per
  report; 11-3: resume).
- ``fin_unmapped``: (filing, metric) pairs no account rule matched (plan 4-1:
  "매핑되지 않은 회사는 목록으로 남긴다").

The connection is opened in WAL mode so a reader is never blocked by the
collector (plan 3). The schema script (tables, views, migrations) runs only
when the stored ``schema_version`` differs from ``SCHEMA_VERSION``, and the
read side (``open_readonly``) never writes: a query no longer takes the write
lock or recreates the views while a collector is running (review_agy H1).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sqlite3

# 2 (2026-11-26): fin_issue; periods marked no_data are retried (document fallback).
# 3 (2026-11-26): periods with unmapped metrics are re-evaluated (financial revenue
#   not applicable, wider owners / net income names); their replies come from the cache.
# 4 (2026-11-26, review_agy): fin_std.currency (plan 4-6), fetch_log index,
#   v_fin_latest without the self-join, growth only between equal fs_div.
# 5 (2026-11-26, review_codex): fin_std.avail_dt -- the day a derived value became
#   knowable (a Q4 resting on a Q3 received after the annual report); growth only
#   between equal currencies.
SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
  corp_code TEXT PRIMARY KEY,
  stock_code TEXT,
  corp_name TEXT NOT NULL,
  market TEXT,
  fiscal_month INTEGER,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_company_stock ON company(stock_code);

CREATE TABLE IF NOT EXISTS filing (
  rcept_no TEXT PRIMARY KEY,
  corp_code TEXT NOT NULL REFERENCES company,
  reprt_code TEXT NOT NULL,
  bsns_year INTEGER NOT NULL,
  rcept_dt TEXT NOT NULL,
  report_nm TEXT,
  is_correction INTEGER DEFAULT 0,
  superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_filing_period ON filing(corp_code, bsns_year, reprt_code);

CREATE TABLE IF NOT EXISTS fin_raw (
  rcept_no TEXT NOT NULL REFERENCES filing,
  fs_div TEXT NOT NULL,
  sj_div TEXT NOT NULL,
  account_id TEXT, account_nm TEXT, ord INTEGER,
  thstrm_amount REAL, thstrm_add_amount REAL,
  frmtrm_amount REAL, frmtrm_add_amount REAL,
  currency TEXT,
  PRIMARY KEY (rcept_no, fs_div, sj_div, ord)
);

CREATE TABLE IF NOT EXISTS fin_std (
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_q INTEGER NOT NULL,
  fs_div TEXT NOT NULL,
  metric TEXT NOT NULL,
  value_q REAL,
  value_ytd REAL,
  derived INTEGER DEFAULT 0,
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  currency TEXT DEFAULT 'KRW',       -- reporting currency (plan 4-6); amounts are never converted
  avail_dt TEXT,                     -- value_q knowable from this day when later than rcept_dt (NULL: rcept_dt)
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, metric, rcept_no)
);

CREATE TABLE IF NOT EXISTS segment_rev (
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_q INTEGER NOT NULL,
  segment_raw TEXT NOT NULL,
  segment_code TEXT,
  revenue REAL,
  op_income REAL,
  basis TEXT,
  source TEXT,
  check_status TEXT,
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, segment_raw, rcept_no)
);

CREATE TABLE IF NOT EXISTS segment_map (
  corp_code TEXT NOT NULL,
  segment_raw TEXT NOT NULL,
  segment_code TEXT NOT NULL,
  valid_from INTEGER, valid_to INTEGER,
  note TEXT,
  PRIMARY KEY (corp_code, segment_raw)
);

CREATE TABLE IF NOT EXISTS fetch_log (
  endpoint TEXT, params TEXT, status TEXT, fetched_at TEXT, error TEXT,
  run_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_fetch_log_at ON fetch_log(fetched_at);

CREATE TABLE IF NOT EXISTS api_usage (
  kst_date TEXT PRIMARY KEY,
  calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_run (
  run_id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT, finished_at TEXT,
  status TEXT,
  calls INTEGER, filings_added INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS fin_fetch (
  rcept_no TEXT PRIMARY KEY,
  status TEXT NOT NULL,              -- ok / unmapped / document / no_data
  src_rcept_no TEXT,                 -- the filing the API's numbers came from
  fs_div TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS fin_unmapped (
  rcept_no TEXT NOT NULL,
  corp_code TEXT NOT NULL,
  metric TEXT NOT NULL,
  PRIMARY KEY (rcept_no, metric)
);

CREATE TABLE IF NOT EXISTS fin_issue (
  rcept_no TEXT NOT NULL,
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER, fiscal_q INTEGER,
  kind TEXT NOT NULL,                -- revenue_na / owners_from_net_income / fs_div_change / from_document /
                                     -- no_data / q4_uses_later_q3
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (rcept_no, kind, detail)
);

"""

# Views are kept apart from the tables: they are dropped and recreated on a
# schema change only (after the column migrations).
_VIEWS = """
-- Newest receipt per (company, fiscal quarter, metric), with the calendar
-- position of the quarter (plan 4-7): period_end 'YYYY-MM' and its calendar
-- year / quarter, from the company's fiscal month (12 when unknown).
DROP VIEW IF EXISTS v_fin_growth;
DROP VIEW IF EXISTS v_fin_latest;
CREATE VIEW v_fin_latest AS
SELECT s.*,
       printf('%04d-%02d', (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) / 12, (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) % 12 + 1) AS period_end,
       (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) / 12 AS cal_year,
       ((s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) % 12) / 3 + 1 AS cal_q
FROM fin_std s
LEFT JOIN company c ON c.corp_code = s.corp_code
WHERE s.rcept_no = (
  SELECT MAX(t.rcept_no) FROM fin_std t
  WHERE t.corp_code = s.corp_code AND t.fiscal_year = s.fiscal_year
    AND t.fiscal_q = s.fiscal_q AND t.metric = s.metric
);

-- Growth is computed only against a base of the same statement kind and the
-- same reporting currency (a company moving OFS -> CFS, or KRW -> USD, would
-- otherwise show a jump that is not growth); fs_div_changed / currency_changed
-- mark the rows whose year-ago base was left out for that.
CREATE VIEW v_fin_growth AS
SELECT c.corp_code, c.fiscal_year, c.fiscal_q, c.metric, c.fs_div, c.currency,
       c.value_q, c.value_ytd, c.derived, c.rcept_no, c.rcept_dt,
       c.period_end, c.cal_year, c.cal_q,
       y.value_q AS value_q_prev_year,
       p.value_q AS value_q_prev_q,
       CASE WHEN y.value_q > 0 AND y.fs_div = c.fs_div AND COALESCE(y.currency, 'KRW') = COALESCE(c.currency, 'KRW')
            THEN c.value_q / y.value_q - 1 END AS yoy,
       CASE WHEN p.value_q > 0 AND p.fs_div = c.fs_div AND COALESCE(p.currency, 'KRW') = COALESCE(c.currency, 'KRW')
            THEN c.value_q / p.value_q - 1 END AS qoq,
       CASE
         WHEN c.metric = 'revenue' OR c.value_q IS NULL OR y.value_q IS NULL OR y.fs_div <> c.fs_div
              OR COALESCE(y.currency, 'KRW') <> COALESCE(c.currency, 'KRW') THEN NULL
         WHEN y.value_q <= 0 AND c.value_q > 0 THEN 'turn_profit'
         WHEN y.value_q > 0 AND c.value_q <= 0 THEN 'turn_loss'
         WHEN y.value_q <= 0 AND c.value_q <= 0 THEN 'loss_continued'
       END AS turn,
       CASE WHEN y.fs_div IS NOT NULL AND y.fs_div <> c.fs_div THEN 1 ELSE 0 END AS fs_div_changed,
       CASE WHEN y.rcept_no IS NOT NULL AND COALESCE(y.currency, 'KRW') <> COALESCE(c.currency, 'KRW')
            THEN 1 ELSE 0 END AS currency_changed
FROM v_fin_latest c
LEFT JOIN v_fin_latest y
  ON y.corp_code = c.corp_code AND y.metric = c.metric
 AND y.fiscal_year = c.fiscal_year - 1 AND y.fiscal_q = c.fiscal_q
LEFT JOIN v_fin_latest p
  ON p.corp_code = c.corp_code AND p.metric = c.metric
 AND ((c.fiscal_q > 1 AND p.fiscal_year = c.fiscal_year AND p.fiscal_q = c.fiscal_q - 1)
   OR (c.fiscal_q = 1 AND p.fiscal_year = c.fiscal_year - 1 AND p.fiscal_q = 4));
"""


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _stored_version(conn: sqlite3.Connection) -> int | None:
    """schema_version of an open DB; None for a new (or pre-version) file."""
    try:
        row = conn.execute("SELECT value FROM sync_state WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    try:
        return int(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def _migrate(conn: sqlite3.Connection, old: int | None) -> None:
    """Bring the DB to SCHEMA_VERSION: tables, column additions, data fixes, views."""
    conn.executescript(_SCHEMA)
    if "currency" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_std)")}:
        conn.execute("ALTER TABLE fin_std ADD COLUMN currency TEXT DEFAULT 'KRW'")
        conn.execute(
            "UPDATE fin_std SET currency = COALESCE((SELECT r.currency FROM fin_raw r "
            "WHERE r.rcept_no = fin_std.rcept_no AND r.currency IS NOT NULL AND r.currency <> '' LIMIT 1), 'KRW')")
    if "avail_dt" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_std)")}:
        conn.execute("ALTER TABLE fin_std ADD COLUMN avail_dt TEXT")
    if old is not None and old < 5:
        # Q4 values get their availability day (and currency-aware bases).
        from data.dart.financials import derive_q4
        for corp, year in conn.execute(
                "SELECT DISTINCT corp_code, fiscal_year FROM fin_std WHERE fiscal_q = 4").fetchall():
            derive_q4(conn, corp, year)
    if old is not None and old < 2:
        conn.execute("DELETE FROM fin_fetch WHERE status = 'no_data'")
    if old is not None and old < 3:
        conn.execute("DELETE FROM fin_fetch WHERE status = 'unmapped'")
        conn.execute("DELETE FROM fin_unmapped")
    conn.executescript(_VIEWS)
    set_state(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    """A read-write dart.db (WAL, rows by name) for the collector. The schema
    script and migrations run only when the stored version is not current."""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    old = _stored_version(conn)
    if old != SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate(conn, old)
    return conn


def schema_is_current(conn: sqlite3.Connection) -> bool:
    return _stored_version(conn) == SCHEMA_VERSION


def open_readonly(path: str, migrate: bool = True) -> sqlite3.Connection | None:
    """A read-only connection for queries and status displays, or None when
    there is no dart.db yet. Nothing is written: no schema script, no state
    update, no write lock. A DB left at an older schema is migrated first
    through a short read-write connection (skipped when that is not possible,
    e.g. a read-only folder; the caller then reads what is there).
    `migrate=False` never touches the file whatever its version -- for a caller
    that only inspects someone's data (the verification test; check
    ``schema_is_current`` and say so instead)."""
    if not os.path.exists(path):
        return None
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    if migrate and _stored_version(conn) != SCHEMA_VERSION:
        conn.close()
        try:
            connect(path).close()
        except sqlite3.Error:
            pass
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value) -> None:
    """Store `value` (str, or anything JSON-serialisable) under `key`; not committed."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, text, now_iso()))
