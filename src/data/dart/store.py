"""dart.db: schema, connection and small write helpers (docs/dart_db_plan_2026-11-26.md 3).

Every table of plan section 3 is created here, plus two bookkeeping tables the
plan's collection rules need (recorded in plan 3, "구현 메모"):

- ``fin_fetch``: one row per filing whose financials were requested, so a run
  that stops anywhere resumes with the filings still missing (plan 3: commit per
  report; 11-3: resume).
- ``fin_unmapped``: (filing, metric) pairs no account rule matched (plan 4-1:
  "매핑되지 않은 회사는 목록으로 남긴다").

The connection is opened in WAL mode so a reader is never blocked by the
collector (plan 3).
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

# 2 (2026-11-26): fin_issue; periods marked no_data are retried (document fallback).
# 3 (2026-11-26): periods with unmapped metrics are re-evaluated (financial revenue
#   not applicable, wider owners / net income names); their replies come from the cache.
SCHEMA_VERSION = 3

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
  status TEXT NOT NULL,              -- ok / no_data / unmapped / error
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
  kind TEXT NOT NULL,                -- revenue_na / owners_from_net_income / fs_div_change / from_document / no_data
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (rcept_no, kind, detail)
);

-- Newest receipt per (company, fiscal quarter, metric), with the calendar
-- position of the quarter (plan 4-7): period_end 'YYYY-MM' and its calendar
-- year / quarter, from the company's fiscal month (12 when unknown).
DROP VIEW IF EXISTS v_fin_latest;
CREATE VIEW v_fin_latest AS
SELECT s.*,
       printf('%04d-%02d', p.m_idx / 12, p.m_idx % 12 + 1) AS period_end,
       p.m_idx / 12 AS cal_year,
       (p.m_idx % 12) / 3 + 1 AS cal_q
FROM fin_std s
JOIN (SELECT s2.rowid AS rid,
             s2.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s2.fiscal_q) AS m_idx
      FROM fin_std s2 LEFT JOIN company c ON c.corp_code = s2.corp_code) p ON p.rid = s.rowid
WHERE s.rcept_no = (
  SELECT MAX(t.rcept_no) FROM fin_std t
  WHERE t.corp_code = s.corp_code AND t.fiscal_year = s.fiscal_year
    AND t.fiscal_q = s.fiscal_q AND t.metric = s.metric
);

DROP VIEW IF EXISTS v_fin_growth;
CREATE VIEW v_fin_growth AS
SELECT c.corp_code, c.fiscal_year, c.fiscal_q, c.metric, c.fs_div,
       c.value_q, c.value_ytd, c.derived, c.rcept_no, c.rcept_dt,
       c.period_end, c.cal_year, c.cal_q,
       y.value_q AS value_q_prev_year,
       p.value_q AS value_q_prev_q,
       CASE WHEN y.value_q > 0 THEN c.value_q / y.value_q - 1 END AS yoy,
       CASE WHEN p.value_q > 0 THEN c.value_q / p.value_q - 1 END AS qoq,
       CASE
         WHEN c.metric = 'revenue' OR c.value_q IS NULL OR y.value_q IS NULL THEN NULL
         WHEN y.value_q <= 0 AND c.value_q > 0 THEN 'turn_profit'
         WHEN y.value_q > 0 AND c.value_q <= 0 THEN 'turn_loss'
         WHEN y.value_q <= 0 AND c.value_q <= 0 THEN 'loss_continued'
       END AS turn
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


def connect(path: str) -> sqlite3.Connection:
    """An open dart.db with the schema in place (WAL, row access by name)."""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    old = get_state(conn, "schema_version")
    if old is not None and int(old) < 2:
        conn.execute("DELETE FROM fin_fetch WHERE status = 'no_data'")
    if old is not None and int(old) < 3:
        conn.execute("DELETE FROM fin_fetch WHERE status = 'unmapped'")
        conn.execute("DELETE FROM fin_unmapped")
    set_state(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
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
