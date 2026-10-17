"""
trade_db.py – SQLite-backed trading history database.

Replaces the fragmented JSON approach (custom_history.json + trade_overrides.json)
with a single, ACID-compliant SQLite database (portfolio.db).

Schema
------
trades
  id           INTEGER  PRIMARY KEY AUTOINCREMENT
  orig_key     TEXT     UNIQUE  – "Company_BuyDate_Qty" natural key (backwards-compat)
  company      TEXT     NOT NULL
  market       TEXT
  ticker       TEXT
  buy_date     TEXT     NOT NULL  – YYYY-MM-DD
  buy_price    REAL
  qty          REAL
  buy_amount   REAL
  sell_date    TEXT               – YYYY-MM-DD or ''
  sell_price   REAL
  sell_qty     REAL
  sell_amount  REAL
  is_custom    INTEGER  DEFAULT 1 – 1 = manually entered, 0 = imported from XLS
  created_at   TEXT               – ISO-8601 timestamp
  updated_at   TEXT               – ISO-8601 timestamp

asset_records  (Total Assets tab's weekly snapshots; was trading_record.json,
                imported once by _migrate_asset_records_json)
  date         TEXT     PRIMARY KEY – YYYY-MM-DD (a Friday)
  total        REAL     NOT NULL    – total assets in KRW
  manual       INTEGER  DEFAULT 1   – 1 = typed by the user, 0 = auto-filled from the live total
  created_at / updated_at TEXT

migrations  (one row per completed one-time JSON import)
  name         TEXT     PRIMARY KEY – "trades_legacy_json" / "asset_records_json"
  done_at      TEXT
"""

import sqlite3
import json
import math
import os
import logging
import datetime as _dt

from paths import DB_FILE, LEGACY_CUSTOM_HISTORY_JSON, LEGACY_TRADE_OVERRIDES_JSON, TRADING_RECORD_FILE

logger = logging.getLogger(__name__)

# Module-level aliases kept as plain names so tests (TempDBMixin) can swap them
# for temp files without touching paths.py.
_DB_FILE        = DB_FILE
_CUSTOM_JSON    = LEGACY_CUSTOM_HISTORY_JSON
_OVERRIDES_JSON = LEGACY_TRADE_OVERRIDES_JSON
_ASSET_JSON     = TRADING_RECORD_FILE


# ── Connection helper ──────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    """Return a connection with row_factory and WAL mode enabled."""
    conn = sqlite3.connect(_DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema initialisation ──────────────────────────────────────────────────────

def init_db() -> None:
    """Create the trades table if it doesn't exist, then run all migrations."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                orig_key     TEXT    UNIQUE,
                company      TEXT    NOT NULL,
                market       TEXT    DEFAULT '',
                ticker       TEXT    DEFAULT '',
                buy_date     TEXT    NOT NULL,
                buy_price    REAL    DEFAULT 0.0,
                qty          REAL    DEFAULT 0.0,
                buy_amount   REAL    DEFAULT 0.0,
                sell_date    TEXT    DEFAULT '',
                sell_price   REAL    DEFAULT 0.0,
                sell_qty     REAL    DEFAULT 0.0,
                sell_amount  REAL    DEFAULT 0.0,
                is_custom    INTEGER DEFAULT 1,
                created_at   TEXT    DEFAULT (datetime('now','localtime')),
                updated_at   TEXT    DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_buy_date ON trades(buy_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_sell_date ON trades(sell_date)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asset_records (
                date        TEXT    PRIMARY KEY,
                total       REAL    NOT NULL,
                manual      INTEGER DEFAULT 1,
                created_at  TEXT    DEFAULT (datetime('now','localtime')),
                updated_at  TEXT    DEFAULT (datetime('now','localtime'))
            )
        """)
        # Completed one-time imports. Without it the JSON migrations below
        # could only tell "not migrated yet" from "data in the table", so
        # rows the user deleted came back from the legacy files on the next
        # start (review_codex.md #2).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                name    TEXT PRIMARY KEY,
                done_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()
        # 1. Legacy JSON migration (custom_history.json + trade_overrides.json)
        _migrate_legacy_json(conn)
        # 2. Total Assets snapshots (trading_record.json), once
        _migrate_asset_records_json(conn)
    finally:
        conn.close()


# ── Total Assets snapshots (asset_records) ────────────────────────────────────

def _migration_done(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM migrations WHERE name = ?", (name,)).fetchone() is not None


def _mark_migration_done(conn: sqlite3.Connection, name: str) -> None:
    """Record `name` as done; the caller commits, so the mark lands in the
    same transaction as the rows it imported."""
    conn.execute("INSERT OR IGNORE INTO migrations (name) VALUES (?)", (name,))


_ASSET_MIGRATION = "asset_records_json"
_TRADES_MIGRATION = "trades_legacy_json"


def _migrate_asset_records_json(conn: sqlite3.Connection) -> None:
    """One-time import of trading_record.json into asset_records. Recorded in
    `migrations`, so a JSON left on disk is never re-applied, not even after
    the user deletes every snapshot. A table that already has rows from
    before the `migrations` table existed counts as migrated."""
    if _migration_done(conn, _ASSET_MIGRATION):
        return
    if conn.execute("SELECT COUNT(*) FROM asset_records").fetchone()[0]:
        _mark_migration_done(conn, _ASSET_MIGRATION)
        conn.commit()
        return
    if not os.path.exists(_ASSET_JSON):
        return
    try:
        with open(_ASSET_JSON, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        logger.warning("[trade_db] Could not read %s: %s", _ASSET_JSON, e)
        return
    if not isinstance(records, list):
        return
    rows = [(r["date"], float(r["total"]), 1 if r.get("manual", True) else 0)
            for r in records if isinstance(r, dict) and r.get("date") is not None and r.get("total") is not None]
    conn.executemany("INSERT OR IGNORE INTO asset_records (date, total, manual) VALUES (?, ?, ?)", rows)
    _mark_migration_done(conn, _ASSET_MIGRATION)
    conn.commit()
    logger.info("[trade_db] Imported %d asset snapshot(s) from %s", len(rows), os.path.basename(_ASSET_JSON))


def load_asset_records() -> list[dict]:
    """All weekly snapshots, oldest first: [{"date", "total", "manual"}, ...]."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT date, total, manual FROM asset_records ORDER BY date").fetchall()
        return [{"date": r["date"], "total": float(r["total"]), "manual": bool(r["manual"])} for r in rows]
    finally:
        conn.close()


def save_asset_records(records: list[dict]) -> None:
    """Replace the whole asset_records table with `records` in one transaction.
    The Total Assets tab edits an in-memory list (a few hundred rows at most)
    and saves it as a unit, the same way it saved the JSON file. A non-finite
    total raises ValueError before anything is deleted (review_codex.md #6)."""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(r["date"], float(r["total"]), 1 if r.get("manual", True) else 0, now, now) for r in records]
    bad = [row[0] for row in rows if not math.isfinite(row[1])]
    if bad:
        raise ValueError(f"asset total must be a finite number: {bad}")
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM asset_records")
            conn.executemany(
                "INSERT INTO asset_records (date, total, manual, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        conn.close()


# ── Migration from JSON ────────────────────────────────────────────────────────

def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    """One-time migration: import custom_history.json + trade_overrides.json into DB.
    Skips records that already exist, and once it has run (recorded in
    `migrations`) never runs again, so trades deleted afterwards stay deleted."""
    if _migration_done(conn, _TRADES_MIGRATION):
        return
    if not (os.path.exists(_OVERRIDES_JSON) or os.path.exists(_CUSTOM_JSON)):
        return

    # ── Load overrides to patch buy-side records from JSON ──────────────────
    overrides: dict = {}
    if os.path.exists(_OVERRIDES_JSON):
        try:
            with open(_OVERRIDES_JSON, "r", encoding="utf-8") as f:
                overrides = json.load(f)
        except Exception as e:
            logger.warning("[trade_db] Could not read trade_overrides.json: %s", e)

    # ── Load custom_history.json ─────────────────────────────────────────────
    custom_records: list = []
    if os.path.exists(_CUSTOM_JSON):
        try:
            with open(_CUSTOM_JSON, "r", encoding="utf-8") as f:
                custom_records = json.load(f)
        except Exception as e:
            logger.warning("[trade_db] Could not read custom_history.json: %s", e)

    # ── First: import override entries (these are the canonical closed trades) ─
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    imported = 0
    for key, ov in overrides.items():
        co = ov.get("company", "")
        if not co:
            continue
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                    (orig_key, company, market, ticker,
                     buy_date, buy_price, qty, buy_amount,
                     sell_date, sell_price, sell_qty, sell_amount,
                     is_custom, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
            """, (
                key, co,
                ov.get("market", ""), ov.get("ticker", ""),
                ov.get("buy_date", ""), float(ov.get("buy_price", 0)),
                float(ov.get("qty", 0)), float(ov.get("buy_amount", 0)),
                ov.get("sell_date", ""), float(ov.get("sell_price", 0)),
                float(ov.get("sell_qty", 0)), float(ov.get("sell_amount", 0)),
                now, now,
            ))
            imported += conn.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            logger.warning("[trade_db] Migration error for override '%s': %s", key, e)

    # ── Second: import custom_history.json (open positions not yet in DB) ─────
    for rec in custom_records:
        co = rec.get("company", "")
        if not co:
            continue
        buy_date = rec.get("buy_date", "")
        qty = float(rec.get("qty", 0))
        key = rec.get("orig_key") or f"{co}_{buy_date}_{qty}"

        # If this record already has an override entry, skip (already imported above)
        if key in overrides:
            continue

        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                    (orig_key, company, market, ticker,
                     buy_date, buy_price, qty, buy_amount,
                     sell_date, sell_price, sell_qty, sell_amount,
                     is_custom, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
            """, (
                key, co,
                rec.get("market", ""), rec.get("ticker", ""),
                buy_date, float(rec.get("buy_price", 0)),
                qty, float(rec.get("buy_amount", 0)),
                rec.get("sell_date", ""), float(rec.get("sell_price", 0)),
                float(rec.get("sell_qty", 0)), float(rec.get("sell_amount", 0)),
                now, now,
            ))
            imported += conn.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            logger.warning("[trade_db] Migration error for custom trade '%s': %s", key, e)

    _mark_migration_done(conn, _TRADES_MIGRATION)
    conn.commit()
    if imported:
        logger.info("[trade_db] Migrated %d legacy record(s) into portfolio.db", imported)


# ── CRUD helpers ───────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict matching the in-memory record format."""
    d = dict(row)
    # Derived / runtime fields (computed elsewhere, not stored)
    d.setdefault("pl", 0.0)
    d.setdefault("pl_pct", 0.0)
    d.setdefault("days_held", 0)
    d.setdefault("curr_days", 0)
    d.setdefault("curr_price", 0.0)
    d.setdefault("curr_pl", 0.0)
    d.setdefault("curr_pl_pct", 0.0)
    d.setdefault("curr_pct_pl", 0.0)
    d.setdefault("position_w", 0.0)
    d.setdefault("wk1", 0.0)
    d.setdefault("wk2", 0.0)
    d.setdefault("mth1", 0.0)
    return d


def load_all_trades() -> list[dict]:
    """Return all trades sorted by buy_date ascending."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY buy_date ASC, id ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


_UPSERT_SQL = """
    INSERT INTO trades
        (orig_key, company, market, ticker,
         buy_date, buy_price, qty, buy_amount,
         sell_date, sell_price, sell_qty, sell_amount,
         is_custom, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
    ON CONFLICT(orig_key) DO UPDATE SET
        company     = excluded.company,
        market      = excluded.market,
        ticker      = excluded.ticker,
        buy_date    = excluded.buy_date,
        buy_price   = excluded.buy_price,
        qty         = excluded.qty,
        buy_amount  = excluded.buy_amount,
        sell_date   = excluded.sell_date,
        sell_price  = excluded.sell_price,
        sell_qty    = excluded.sell_qty,
        sell_amount = excluded.sell_amount,
        updated_at  = excluded.updated_at
"""

# Plain INSERT (no ON CONFLICT) used only for the auto-generated-key path: letting the
# UNIQUE constraint itself reject a collision — instead of pre-checking with a separate
# SELECT — closes the TOCTOU window where two concurrent callers could both see the same
# base_key as free and one silently overwrite the other's row via ON CONFLICT DO UPDATE.
_INSERT_ONLY_SQL = """
    INSERT INTO trades
        (orig_key, company, market, ticker,
         buy_date, buy_price, qty, buy_amount,
         sell_date, sell_price, sell_qty, sell_amount,
         is_custom, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
"""


_MAX_KEY_SUFFIX = 1000


def _insert_with_generated_key(conn, base_key: str, values_tail: tuple) -> str:
    """Atomically claims a free orig_key derived from base_key by retrying the INSERT
    on a UNIQUE-constraint collision, rather than pre-checking with a SELECT.

    Only an orig_key collision is retried: any other IntegrityError (NOT NULL,
    a future CHECK) fails the same way whatever the key, and used to loop here
    forever (review_codex.md #7)."""
    candidate = base_key
    for suffix in range(2, _MAX_KEY_SUFFIX + 2):
        try:
            conn.execute(_INSERT_ONLY_SQL, (candidate, *values_tail))
            return candidate
        except sqlite3.IntegrityError as e:
            if "trades.orig_key" not in str(e):
                raise
            candidate = f"{base_key}_{suffix}"
    raise sqlite3.IntegrityError(f"no free orig_key for {base_key!r} after {_MAX_KEY_SUFFIX} tries")


def _values_tail(record: dict, now: str) -> tuple:
    """The trade columns after orig_key, validated: company and buy_date are
    required (NOT NULL) and every number must be finite -- inf/NaN would
    poison every P/L sum, and SQLite stores NaN as NULL (review_codex.md #6/#7).
    Raises ValueError."""
    co = record.get("company", "")
    buy_date = record.get("buy_date", "")
    if co is None or buy_date is None:
        raise ValueError(f"trade without company/buy_date: {record.get('orig_key')!r}")

    def num(field):
        v = float(record.get(field, 0) or 0)
        if not math.isfinite(v):
            raise ValueError(f"{field} must be a finite number, got {v!r}")
        return v

    return (
        co, record.get("market", ""), record.get("ticker", ""),
        buy_date, num("buy_price"),
        num("qty"), num("buy_amount"),
        record.get("sell_date", "") or "", num("sell_price"),
        num("sell_qty"), num("sell_amount"),
        now, now,
    )


def upsert_trade(record: dict) -> str:
    """Insert or update a trade. Returns the orig_key of the saved record."""
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values_tail = _values_tail(record, now)
    co, buy_date, qty = values_tail[0], values_tail[3], values_tail[5]

    conn = _connect()
    try:
        key = record.get("orig_key")
        if key:
            # Caller already knows which record it's updating — upsert is correct here,
            # there's no key to generate and thus no collision to race against.
            conn.execute(_UPSERT_SQL, (key, *values_tail))
        else:
            key = _insert_with_generated_key(conn, f"{co}_{buy_date}_{qty}", values_tail)
            record["orig_key"] = key
        conn.commit()
        return key
    finally:
        conn.close()


def upsert_trades(records: list[dict]) -> list[str]:
    """Insert or update many trades in a single connection/transaction.
    Returns the list of orig_keys that were saved, in input order.

    Rows with an explicit orig_key are batched through one executemany upsert (no
    collision possible — the caller already identified the row). Rows needing a
    generated key are inserted individually via _insert_with_generated_key so a
    collision retries against the DB itself instead of an in-memory snapshot that
    could go stale against a concurrent writer (see upsert_trade's TOCTOU note).
    """
    if not records:
        return []

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keys: list[str] = [None] * len(records)
    keyed_rows = []
    unkeyed_indices = []

    for i, record in enumerate(records):
        values_tail = _values_tail(record, now)
        co, buy_date, qty = values_tail[0], values_tail[3], values_tail[5]
        key = record.get("orig_key")
        if key:
            keys[i] = key
            keyed_rows.append((key, *values_tail))
        else:
            unkeyed_indices.append((i, record, co, buy_date, qty, values_tail))

    conn = _connect()
    try:
        # One transaction for the whole batch (the partial-sell split saves its
        # closed sub-lot and the remainder together, review_codex.md #1); the
        # generated keys reach the callers' records only after the commit.
        with conn:
            if keyed_rows:
                conn.executemany(_UPSERT_SQL, keyed_rows)
            for i, record, co, buy_date, qty, values_tail in unkeyed_indices:
                keys[i] = _insert_with_generated_key(conn, f"{co}_{buy_date}_{qty}", values_tail)
        for i, record, *_ in unkeyed_indices:
            record["orig_key"] = keys[i]
        return keys
    finally:
        conn.close()


def backup_to(dest_path: str) -> None:
    """Write a consistent copy of portfolio.db to dest_path with SQLite's
    online backup API. Unlike a file copy after checkpoint_wal(), this
    includes every committed WAL frame and takes the locks itself, so it is
    safe while the app is running (AutoBackupThread uses it at startup).

    Copies in 100-page chunks with a short sleep between them (rather than
    the whole DB in one shot) so a concurrent write from the main/UI thread
    gets a chance to run between chunks instead of hitting "database is
    locked" (review_agy.md #6)."""
    src = _connect()
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst, pages=100, sleep=0.01)
        finally:
            dst.close()
    finally:
        src.close()


def delete_trade(orig_key: str) -> bool:
    """Delete a trade by its orig_key. Returns True if a row was deleted."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM trades WHERE orig_key = ?", (orig_key,))
        conn.commit()
        deleted = conn.execute("SELECT changes()").fetchone()[0]
        return deleted > 0
    finally:
        conn.close()


def get_trade(orig_key: str) -> dict | None:
    """Fetch a single trade by orig_key."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE orig_key = ?", (orig_key,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_open_trades() -> list[dict]:
    """Return trades with no sell_date (open positions)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE (sell_date IS NULL OR sell_date = '') ORDER BY buy_date ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_closed_trades() -> list[dict]:
    """Return trades with a sell_date (closed positions)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE sell_date IS NOT NULL AND sell_date != '' ORDER BY sell_date ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def db_path() -> str:
    """Return the absolute path to the database file."""
    return _DB_FILE
