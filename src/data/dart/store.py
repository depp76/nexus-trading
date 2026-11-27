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
# 6 (2026-11-26, stage 4): seg_fetch -- which filings' segment revenue was read.
# 7 (2026-11-26, review_codex 2): a Q4 row per Q3 version (fin_std.derived = 2 under the
#   late Q3's receipt) instead of one value with an availability day; Q4 rows re-derived.
# 10 (2026-11-27): segments read again once -- the business section's product /
#   segment tables (segments.biz_candidates).
# 11 (2026-11-27): segment_rev.level -- a period keeps its segment breakdown and,
#   where the business section has one, its product breakdown (read again once).
# 12 (2026-11-27, review_codex 5): segments read again once -- order-book, cost and
#   customer tables are no longer taken for revenue breakdowns, a candidate is checked
#   against its own period only, a product sold by several segments is added up.
# 13 also (2026-11-27, review_agy 2; the branch was not merged, so no 14):
#   segment_rev.axis -- what the items are (segment / product / region / sales_type /
#   unknown), apart from the source -- and segment_rev.op_source, where an operating
#   income came from; the stored rows get their axis from their names at once.
SCHEMA_VERSION = 13

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
  derived INTEGER DEFAULT 0,         -- 1: computed (Q4 = annual - Q3, a quarter from cumulatives);
                                     -- 2: Q4 restated under a Q3 correction received after the annual
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  currency TEXT DEFAULT 'KRW',       -- reporting currency (plan 4-6); amounts are never converted
  avail_dt TEXT,                     -- unused since schema 7 (a late Q3 gives its own Q4 row, derived = 2)
  ytd_derived INTEGER DEFAULT 0,     -- 1: value_ytd = the quarter before + value_q (the reply had no cumulative)
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, metric, rcept_no)
);

CREATE TABLE IF NOT EXISTS segment_rev (
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_q INTEGER NOT NULL,
  level TEXT NOT NULL DEFAULT 'segment',   -- segment / product (the business section's product table)
  segment_raw TEXT NOT NULL,
  segment_code TEXT,
  revenue REAL,
  op_income REAL,
  basis TEXT,
  source TEXT,
  check_status TEXT,
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  axis TEXT,                               -- segment / product / region / sales_type / unknown (Candidate.axis)
  op_source TEXT,                          -- the source op_income was read from (segments.fill_ops)
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, level, segment_raw, rcept_no)
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
  status TEXT NOT NULL,              -- ok / unmapped / document / no_data / awaiting_correction
  src_rcept_no TEXT,                 -- the filing the API's numbers came from
  fs_div TEXT,
  fetched_at TEXT,
  retry_after TEXT                   -- awaiting_correction: asked again from this day (schema 8)
);

-- Segment revenue requests (stage 4): one row per filing (the newest receipt
-- of a period) whose segments were read, so a run resumes with the rest.
CREATE TABLE IF NOT EXISTS seg_fetch (
  rcept_no TEXT PRIMARY KEY,
  status TEXT NOT NULL,              -- ok / sum_mismatch / review / na (no revenue to check against)
  source TEXT,                       -- xbrl / tag_segment / tag_region / biz_segment / html_segment / html_product / single (product level: biz_product / tag_product / html_product)
  detail TEXT,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_segment_rev_period ON segment_rev(corp_code, fiscal_year, fiscal_q);

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
-- One statement version per (company, fiscal quarter): the newest receipt that
-- stored the quarter's statement rows (derived <> 2), and only the metrics of
-- that receipt -- a metric a correction no longer has is not taken from the
-- original (review_codex 4 P1). A Q4 restated under a later Q3 correction
-- (derived = 2) is kept on its own and replaces that metric when it is newer.
-- With the calendar position of the quarter (plan 4-7): period_end 'YYYY-MM'
-- and its calendar year / quarter, from the company's fiscal month (12 when unknown).
DROP VIEW IF EXISTS v_fin_growth;
DROP VIEW IF EXISTS v_fin_latest;
DROP VIEW IF EXISTS v_fin_version;
CREATE VIEW v_fin_version AS
SELECT corp_code, fiscal_year, fiscal_q, MAX(rcept_no) AS rcept_no FROM fin_std
WHERE COALESCE(derived, 0) <> 2 GROUP BY corp_code, fiscal_year, fiscal_q;

CREATE VIEW v_fin_latest AS
SELECT s.*,
       printf('%04d-%02d', (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) / 12, (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) % 12 + 1) AS period_end,
       (s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) / 12 AS cal_year,
       ((s.fiscal_year * 12 + COALESCE(c.fiscal_month, 12) - 1 - 3 * (4 - s.fiscal_q)) % 12) / 3 + 1 AS cal_q
FROM fin_std s
JOIN v_fin_version v ON v.corp_code = s.corp_code AND v.fiscal_year = s.fiscal_year AND v.fiscal_q = s.fiscal_q
LEFT JOIN company c ON c.corp_code = s.corp_code
WHERE (COALESCE(s.derived, 0) <> 2 AND s.rcept_no = v.rcept_no
       AND NOT EXISTS (SELECT 1 FROM fin_std t WHERE t.corp_code = s.corp_code AND t.fiscal_year = s.fiscal_year
                       AND t.fiscal_q = s.fiscal_q AND t.metric = s.metric AND t.derived = 2
                       AND t.rcept_no > v.rcept_no))
   OR (s.derived = 2 AND s.rcept_no > v.rcept_no
       AND s.rcept_no = (SELECT MAX(t.rcept_no) FROM fin_std t WHERE t.corp_code = s.corp_code
                         AND t.fiscal_year = s.fiscal_year AND t.fiscal_q = s.fiscal_q
                         AND t.metric = s.metric AND t.derived = 2));

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


def _add_segment_level(conn: sqlite3.Connection) -> None:
    """segment_rev before schema 11 had no level and a key without it: rebuilt
    with every existing row at the segment level."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(segment_rev)")]
    if not cols or "level" in cols:
        return
    conn.execute("ALTER TABLE segment_rev RENAME TO segment_rev_old")
    conn.execute("DROP INDEX IF EXISTS ix_segment_rev_period")
    conn.executescript(_SCHEMA)
    keep = ", ".join(cols)
    conn.execute(f"INSERT INTO segment_rev({keep}, level) SELECT {keep}, "
                 "CASE WHEN source = 'biz_product' THEN 'product' ELSE 'segment' END FROM segment_rev_old")
    conn.execute("DROP TABLE segment_rev_old")


def _add_segment_axis(conn: sqlite3.Connection) -> None:
    """segment_rev.axis / op_source (schema 13, review_agy 2). The rows already
    stored get the axis their names and source give (Candidate.axis), so the
    screen stops joining a year of kinds of sale to a year of segments before
    the periods are read again; op_source stays empty until then."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(segment_rev)")}
    if "op_source" not in cols:
        conn.execute("ALTER TABLE segment_rev ADD COLUMN op_source TEXT")
    if "axis" in cols:
        return
    conn.execute("ALTER TABLE segment_rev ADD COLUMN axis TEXT")
    from data.dart.segments import Candidate
    groups: dict = {}
    for r in conn.execute("SELECT rcept_no, level, source, segment_raw, revenue, segment_code "
                          "FROM segment_rev").fetchall():
        if not str(r[5] or "").startswith("_"):            # adjustment rows take their segments' axis
            groups.setdefault((r[0], r[1], r[2]), []).append((r[3], r[4]))
    conn.executemany("UPDATE segment_rev SET axis = ? WHERE rcept_no = ? AND level = ? AND source IS ?",
                     [(Candidate(source or "", items).axis(), rcept, level, source)
                      for (rcept, level, source), items in groups.items()])


def _migrate(conn: sqlite3.Connection, old: int | None) -> None:
    """Bring the DB to SCHEMA_VERSION: tables, column additions, data fixes, views."""
    _add_segment_level(conn)
    conn.executescript(_SCHEMA)
    _add_segment_axis(conn)
    if "currency" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_std)")}:
        conn.execute("ALTER TABLE fin_std ADD COLUMN currency TEXT DEFAULT 'KRW'")
        conn.execute(
            "UPDATE fin_std SET currency = COALESCE((SELECT r.currency FROM fin_raw r "
            "WHERE r.rcept_no = fin_std.rcept_no AND r.currency IS NOT NULL AND r.currency <> '' LIMIT 1), 'KRW')")
    if "avail_dt" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_std)")}:
        conn.execute("ALTER TABLE fin_std ADD COLUMN avail_dt TEXT")
    if "ytd_derived" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_std)")}:
        conn.execute("ALTER TABLE fin_std ADD COLUMN ytd_derived INTEGER DEFAULT 0")
    if "retry_after" not in {r[1] for r in conn.execute("PRAGMA table_info(fin_fetch)")}:
        conn.execute("ALTER TABLE fin_fetch ADD COLUMN retry_after TEXT")
    if old is not None and old < 12:
        # Every period's segments are read again (the stored rows stay shown until
        # then); the XBRL replies are cached, the documents are asked for again.
        # 11: the level column; 12: the review_codex 5 reading rules -- seg_fetch
        # skips a receipt already read, so a new parser version alone would leave
        # the misread rows (order books, cost tables, customers) in place.
        conn.execute("DELETE FROM seg_fetch")
    elif old is not None and old < 13:
        # 13 (2026-11-27, 기아): segment note tables whose labels run over several
        # row columns, a region's own eliminations inside its total, and the
        # document's XBRL-tagged cells as a source ahead of the table readers --
        # every period not settled by the XBRL package is read again (a tagged
        # product breakdown may fill the product level of those too: all of them).
        # Also the review_agy readings of the same day ("all other segments",
        # account names and instant columns left out, "&cr;", the net reading
        # before the gross one, operating income filled in) and of review_agy 2
        # (kinds of sale and regions are not taken for operating segments, a
        # period no table settles keeps basis NULL). This reread asks
        # for each document once more and saves its slice (segments.slice_document);
        # a later change of the readers is read from the slices.
        conn.execute("DELETE FROM seg_fetch")
    if old is not None and old < 8:
        # A correction marked done although the numbers stored for it came from an
        # older receipt (closed after the 7-day wait before schema 8) is asked
        # again once; attachment-only corrections change no statement and stay
        # done (review_codex 3 P1).
        from data.dart.filings import is_attachment_only
        stale = [r[0] for r in conn.execute(
            "SELECT x.rcept_no, f.report_nm FROM fin_fetch x JOIN filing f ON f.rcept_no = x.rcept_no "
            "WHERE x.status IN ('ok', 'unmapped') AND x.src_rcept_no IS NOT NULL AND x.src_rcept_no < x.rcept_no "
            "AND f.is_correction = 1").fetchall() if not is_attachment_only(r[1])]
        conn.executemany("UPDATE fin_fetch SET status = 'awaiting_correction', retry_after = '' WHERE rcept_no = ?",
                         [(r,) for r in stale])
    if old is not None and old < 7:
        # Periods closed as "no data" / "review" may have been closed by a failed
        # request (review_codex 2 P1): look at them once more. A confirmed "no
        # file" answer is in the negative cache, so this costs (almost) no calls.
        conn.execute("DELETE FROM fin_fetch WHERE status = 'no_data'")
        conn.execute("DELETE FROM seg_fetch WHERE status = 'review'")
        # Q4 rows are rebuilt (currency-aware bases; one row per later Q3 version).
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
