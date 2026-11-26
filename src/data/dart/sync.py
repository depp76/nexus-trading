"""The one DART collection entry point (docs/dart_db_plan_2026-11-26.md 6, 11, 12).

``run_sync(mode, trigger, ...)`` is called by the CLI (``tools/dart_sync.py``),
later by the app's thread and a scheduler; they differ only in how they start
it. No Qt here: progress goes to ``on_progress(done, total, msg)`` and a stop
request comes from ``cancel`` (a callable or a ``threading.Event``), checked at
every report / list-window boundary.

Modes (plan 11-3 order inside ``auto``):

- ``init``: company list, then the market-wide periodic-report list of the
  window (calendar quarters from Jan 1 of ``today.year - years``), resumable
  through ``sync_state.list_backfill_cursor``.
- ``daily``: company list when older than 7 days; the report list from
  ``daily_until - 3 days`` to today; financials of the new receipts only.
- ``backfill``: financials of every filing not fetched yet (``fin_fetch``),
  Universe stocks first, then the rest by market cap.
- ``auto``: company list (if stale) -> list backfill (if unfinished) -> daily
  -> backfill.

With ``tickers`` the list comes from per-company searches and only those
companies' financials are fetched (verification runs).

Every run is a ``sync_run`` row: ``done``, ``partial`` (daily limit, run budget
or cancel), ``failed``, ``skipped`` (another collector holds the lock) or
``no_key`` (no DART_API_KEY; nothing is called).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import uuid

import paths
from data.dart import corp as corp_mod
from data.dart import filings as fil
from data.dart import financials as fin
from data.dart.client import DAILY_LIMIT, DartBudgetExceeded, DartClient, api_key_from_env, calls_today, kst_today
from data.dart.lock import SyncLock
from data.dart.store import connect, get_state, now_iso, set_state

logger = logging.getLogger(__name__)

DART_YEARS = 5                 # 5 business years + the current one (plan header)
DAILY_OVERLAP_DAYS = 3         # plan 11-3-2
MODES = ("init", "daily", "backfill", "auto")
_KEY_FROM_ENV = object()
_KST = _dt.timezone(_dt.timedelta(hours=9))
EVENING_HOUR = 19              # plan 11-2: one run after 19:00 KST catches the day's filings
INITIAL_PENDING_PERIODS = 1000  # more periods than this still to fetch = the initial load (plan 11-8)


def kst_now() -> _dt.datetime:
    return _dt.datetime.now(_KST)


def _parse_kst(text: str | None) -> _dt.datetime | None:
    if not text:
        return None
    try:
        t = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return t.replace(tzinfo=_KST) if t.tzinfo is None else t.astimezone(_KST)


def is_due(last_run: str | None, last_status: str | None, now: _dt.datetime, at_start: bool) -> bool:
    """Plan 11-2. At app start: run unless a run (not a failed one) already
    finished after today's 19:00 KST. On the 30-minute check: run only after
    19:00, and only if no run finished after 19:00 today."""
    now = now.astimezone(_KST) if now.tzinfo else now.replace(tzinfo=_KST)
    evening = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
    last = _parse_kst(last_run)
    done_tonight = last is not None and last >= evening and last_status != "failed"
    if done_tonight:
        return False
    return at_start or now >= evening


def collection_status(db_path: str | None = None, today: _dt.date | None = None, years: int = DART_YEARS) -> dict:
    """What a status display needs, read from dart.db: last run (time, status,
    calls, new filings), whether the list backfill is done, the periods in the
    window and how many still lack financials, and today's calls / limit left."""
    conn = connect(db_path or paths.DART_DB_FILE)
    try:
        today = today or kst_now().date()
        first = first_fiscal_year(today, years)
        run = conn.execute("SELECT status, calls, filings_added, error, finished_at FROM sync_run "
                           "WHERE status NOT IN ('running', 'skipped') ORDER BY started_at DESC LIMIT 1").fetchone()
        total, pending = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(p.missing > 0), 0) FROM ("
            " SELECT f.corp_code, f.bsns_year, f.reprt_code, SUM(x.rcept_no IS NULL) AS missing"
            " FROM filing f LEFT JOIN fin_fetch x ON x.rcept_no = f.rcept_no WHERE f.bsns_year >= ?"
            " GROUP BY f.corp_code, f.bsns_year, f.reprt_code) p", (first,)).fetchone()
        used = calls_today(conn, today.isoformat())
        return {
            "last_run": get_state(conn, "last_run"),
            "last_status": get_state(conn, "last_run_status"),
            "last_calls": run["calls"] if run else None,
            "last_added": run["filings_added"] if run else None,
            "last_error": run["error"] if run else None,
            "list_done": get_state(conn, "list_backfill_done") == "1",
            "periods_total": int(total or 0),
            "periods_pending": int(pending or 0),
            "calls_today": used,
            "calls_left": max(0, DAILY_LIMIT - used),
        }
    finally:
        conn.close()


def is_initial_load(status: dict) -> bool:
    """Whether the first full collection is still under way (plan 11-8 label)."""
    return not status.get("list_done") or status.get("periods_pending", 0) > INITIAL_PENDING_PERIODS


@dataclasses.dataclass
class SyncResult:
    status: str                       # done / partial / failed / skipped / no_key
    run_id: str
    calls: int = 0
    filings_added: int = 0
    periods_fetched: int = 0
    pending_left: int = 0
    error: str | None = None


class _Cancelled(Exception):
    pass


def _is_cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return cancel.is_set()
    return bool(cancel())


def first_fiscal_year(today: _dt.date, years: int = DART_YEARS) -> int:
    return today.year - years


def run_sync(mode: str = "auto", trigger: str = "cli", *, budget: int | None = None, cancel=None,
             on_progress=None, tickers=None, api_key=_KEY_FROM_ENV, db_path: str | None = None,
             cache_dir: str | None = None, lock_path: str | None = None, client_factory=None,
             today: _dt.date | None = None, years: int = DART_YEARS, priority=None) -> SyncResult:
    """Collect DART data; see the module docstring. `client_factory(conn, key,
    run_id)` replaces the HTTP client (tests); `priority` is the stock-code order
    for the financial backfill (default: Universe cache, then market cap)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    run_id = f"{_dt.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    db_path = db_path or paths.DART_DB_FILE
    cache_dir = cache_dir or paths.DART_CACHE_DIR
    lock_path = lock_path or os.path.join(cache_dir, "sync.lock")
    today = today or _dt.date.fromisoformat(kst_today())
    key = api_key_from_env() if api_key is _KEY_FROM_ENV else api_key

    conn = connect(db_path)
    try:
        if not key:
            _record_run(conn, run_id, trigger, mode, "no_key", 0, 0, "DART_API_KEY is not set", finished=True)
            return SyncResult("no_key", run_id, error="DART_API_KEY is not set")
        lock = SyncLock(lock_path)
        if not lock.acquire():
            _record_run(conn, run_id, trigger, mode, "skipped", 0, 0, "another DART collector holds the lock",
                        finished=True)
            return SyncResult("skipped", run_id)
        try:
            # Holding the lock means no other run is alive: a 'running' row left by a
            # process that ended mid-run (the app closing past its 2 s wait) is closed here.
            conn.execute("UPDATE sync_run SET status = 'partial', finished_at = ?, "
                         "error = 'interrupted: the process ended during the run' WHERE status = 'running'",
                         (now_iso(),))
            _record_run(conn, run_id, trigger, mode, "running", 0, 0, None)
            if client_factory is not None:
                client = client_factory(conn, key, run_id)
            else:
                client = DartClient(conn, key, cache_dir, run_id=run_id, budget=budget)
            job = _Job(conn, client, today, years, cancel, on_progress, tickers, priority)
            status, error = "done", None
            try:
                job.run(mode)
            except (DartBudgetExceeded, _Cancelled) as e:
                status, error = "partial", str(e) or "cancelled"
                conn.rollback()
            except Exception as e:
                # boundary: a run records any failure in sync_run instead of raising into the caller's thread.
                logger.exception("DART sync failed (run %s)", run_id)
                status, error = "failed", f"{type(e).__name__}: {e}"
                conn.rollback()
            calls = getattr(client, "calls", 0)
            pending = job.pending_count()
            set_state(conn, "last_run", kst_now().isoformat(timespec="seconds"))
            set_state(conn, "last_run_status", status)
            _record_run(conn, run_id, trigger, mode, status, calls, job.filings_added, error, finished=True)
            return SyncResult(status, run_id, calls, job.filings_added, job.periods_fetched, pending, error)
        finally:
            lock.release()
    finally:
        conn.close()


def _record_run(conn, run_id, trigger, mode, status, calls, added, error, finished=False) -> None:
    conn.execute(
        "INSERT INTO sync_run(run_id, trigger, mode, started_at, status, calls, filings_added, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, "
        "calls = excluded.calls, filings_added = excluded.filings_added, error = excluded.error",
        (run_id, trigger, mode, now_iso(), status, calls, added, error))
    if finished:
        conn.execute("UPDATE sync_run SET finished_at = ? WHERE run_id = ?", (now_iso(), run_id))
    conn.commit()


class _Job:
    def __init__(self, conn, client, today, years, cancel, on_progress, tickers, priority):
        self.conn = conn
        self.client = client
        self.today = today
        self.first_year = first_fiscal_year(today, years)
        self.cancel = cancel
        self.on_progress = on_progress
        self.tickers = [str(t).zfill(6) for t in tickers] if tickers else None
        self.priority = priority
        self.filings_added = 0
        self.periods_fetched = 0
        self.account_map = fin.load_account_map()

    # ---- plumbing ----------------------------------------------------------------

    def _check(self):
        if _is_cancelled(self.cancel):
            raise _Cancelled("cancelled")

    def _progress(self, done, total, msg):
        if self.on_progress:
            try:
                self.on_progress(done, total, msg)
            except Exception:
                # boundary: a progress callback must never stop the collection.
                logger.warning("DART progress callback failed", exc_info=True)

    def run(self, mode):
        if mode in ("init", "daily", "auto") or (mode == "backfill" and self.tickers):
            self.sync_corp_list(force=(mode == "init"))
        if self.tickers:
            self.list_for_tickers()
        elif mode in ("init", "auto", "backfill") or get_state(self.conn, "list_backfill_done") != "1":
            self.list_backfill()
        if mode in ("daily", "auto") and not self.tickers:
            since = self.daily_list()
            self.fetch_financials(since=since)
        if mode in ("backfill", "auto"):
            self.fetch_financials()

    # ---- company list ------------------------------------------------------------

    def sync_corp_list(self, force=False):
        last = get_state(self.conn, "corp_list_at")
        if last and not force:
            age = self.today - _dt.date.fromisoformat(last[:10])
            if age.days < corp_mod.CORP_LIST_MAX_AGE_DAYS:
                return
        if self.tickers is None or not last:
            self._progress(0, 1, "corp list")
            corp_mod.sync_corp_codes(self.client, self.conn)
            set_state(self.conn, "corp_list_at", self.today.isoformat())
            self.conn.commit()

    # ---- filing lists ------------------------------------------------------------

    def list_backfill(self):
        if get_state(self.conn, "list_backfill_done") == "1":
            return
        start = _dt.date(self.first_year, 1, 1)
        cursor = get_state(self.conn, "list_backfill_cursor")
        if cursor:
            start = max(start, _dt.date.fromisoformat(cursor))
        windows = fil.quarter_windows(start, self.today)
        for i, (bgn, end) in enumerate(windows):
            self._check()
            self._progress(i, len(windows), f"filing list {bgn}..{end}")
            for cls in ("Y", "K"):
                rows = fil.fetch_filing_list(self.client, bgn, end, corp_cls=cls, cacheable=end < self.today,
                                             check=self._check)
                self.filings_added += fil.store_filings(self.conn, rows, self.first_year)
            set_state(self.conn, "list_backfill_cursor", (end + _dt.timedelta(days=1)).isoformat())
            self.conn.commit()
        set_state(self.conn, "list_backfill_done", "1")
        if not get_state(self.conn, "daily_until"):
            set_state(self.conn, "daily_until", self.today.isoformat())
        self.conn.commit()

    def daily_list(self) -> str:
        """Report list since daily_until - 3 days; returns the window start (ISO)."""
        last = get_state(self.conn, "daily_until") or self.today.isoformat()
        start = _dt.date.fromisoformat(last) - _dt.timedelta(days=DAILY_OVERLAP_DAYS)
        for bgn, end in fil.quarter_windows(start, self.today):
            self._check()
            for cls in ("Y", "K"):
                rows = fil.fetch_filing_list(self.client, bgn, end, corp_cls=cls, check=self._check)
                self.filings_added += fil.store_filings(self.conn, rows, self.first_year)
            self.conn.commit()
        set_state(self.conn, "daily_until", self.today.isoformat())
        self.conn.commit()
        return start.isoformat()

    def list_for_tickers(self):
        corps = self._corps_for_tickers()
        start = _dt.date(self.first_year, 1, 1)
        for i, corp in enumerate(corps):
            self._check()
            self._progress(i, len(corps), f"filing list {corp}")
            rows = fil.fetch_filing_list(self.client, start, self.today, corp_code=corp, check=self._check)
            self.filings_added += fil.store_filings(self.conn, rows, self.first_year)
            self.conn.commit()

    def _corps_for_tickers(self) -> list[str]:
        out = []
        for t in self.tickers or []:
            row = self.conn.execute("SELECT corp_code FROM company WHERE stock_code = ?", (t,)).fetchone()
            if row:
                out.append(row[0])
            else:
                logger.warning("DART: no corp code for stock %s", t)
        return out

    # ---- financials --------------------------------------------------------------

    def _pending_sql(self, since=None):
        sql = ("SELECT f.rcept_no, f.corp_code, f.reprt_code, f.bsns_year, f.rcept_dt, c.stock_code, c.fiscal_month "
               "FROM filing f JOIN company c ON c.corp_code = f.corp_code "
               "LEFT JOIN fin_fetch x ON x.rcept_no = f.rcept_no "
               "WHERE x.rcept_no IS NULL AND f.bsns_year >= ?")
        args: list = [self.first_year]
        if since:
            sql += " AND f.rcept_dt >= ?"
            args.append(since)
        if self.tickers:
            sql += f" AND c.stock_code IN ({','.join('?' * len(self.tickers))})"
            args.extend(self.tickers)
        return sql, args

    def pending_count(self) -> int:
        try:
            sql, args = self._pending_sql()
            return len(self.conn.execute(sql, args).fetchall())
        except Exception:
            # boundary: only a status figure; never fail the run's bookkeeping over it.
            logger.warning("DART pending count failed", exc_info=True)
            return -1

    def fetch_financials(self, since=None):
        sql, args = self._pending_sql(since)
        pending = [dict(r) for r in self.conn.execute(sql, args)]
        groups: dict = {}
        for r in pending:
            groups.setdefault((r["corp_code"], r["bsns_year"], r["reprt_code"]), []).append(r)
        rank = self._rank()
        q_of = fil.Q_BY_REPRT
        order = sorted(groups, key=lambda k: (rank(groups[k][0]["stock_code"]), k[0], k[1], q_of[k[2]]))
        total = len(order)
        for i, key in enumerate(order):
            self._check()
            corp, fy, reprt = key
            members = groups[key]
            self._progress(i, total, f"financials {members[0]['stock_code']} {fy} {reprt}")
            self._fetch_group(corp, fy, reprt, members)
            self.conn.commit()
            self.periods_fetched += 1
        if total:
            self._progress(total, total, "financials done")

    def _fetch_group(self, corp, fy, reprt, members):
        latest = max(m["rcept_no"] for m in members)
        fm = members[0]["fiscal_month"] or 12
        years = [fy] if fm == 12 else [fy, fy - 1]
        fs_div, rows = None, []
        for api_year in years:
            fs_div, rows = fin.fetch_period(self.client, corp, api_year, reprt, cache_tag=latest)
            if rows and (fm == 12 or self._period_matches(rows, corp, fy, reprt)):
                break
            fs_div, rows = None, []
        now = now_iso()
        if not rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at) "
                "VALUES (?, 'no_data', NULL, NULL, ?)", [(m["rcept_no"], now) for m in members])
            return
        src, missing = fin.store_period(self.conn, corp, fy, reprt, fs_div, rows, self.account_map)
        status = "unmapped" if missing else "ok"
        done = {m["rcept_no"] for m in members} | {src}
        self.conn.executemany(
            "INSERT OR REPLACE INTO fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(r, status, src, fs_div, now) for r in sorted(done)])

    def _period_matches(self, rows, corp, fy, reprt) -> bool:
        """For a non-December filer: whether the reply's receipt belongs to the
        wanted period (an unknown receipt is accepted)."""
        src = max(str(r.get("rcept_no")) for r in rows if r.get("rcept_no"))
        row = self.conn.execute("SELECT bsns_year, reprt_code FROM filing WHERE rcept_no = ?", (src,)).fetchone()
        return row is None or (row[0] == fy and row[1] == reprt)

    def _rank(self):
        order = self.priority if self.priority is not None else default_priority()
        pos = {code: i for i, code in enumerate(order)}
        n = len(pos)
        return lambda stock: pos.get(stock, n)


def default_priority() -> list[str]:
    """Stock codes in backfill order (plan 11-3-3): the Universe's KOSPI/KOSDAQ
    rows by market cap, then every other KRX stock by its latest market cap."""
    codes: list[str] = []
    try:
        with open(paths.UNIVERSE_CACHE_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        kr = [r for r in rows if isinstance(r, dict) and r.get("market") in ("KOSPI", "KOSDAQ")]
        kr.sort(key=lambda r: -(r.get("market_cap") or 0))
        codes = [str(r.get("ticker")).zfill(6) for r in kr]
    except (OSError, ValueError, TypeError):
        logger.info("DART priority: no Universe cache", exc_info=True)
    try:
        from data.listing import _fetch_recent_marcaps
        _day, caps = _fetch_recent_marcaps()
        seen = set(codes)
        codes += [c for c, _ in sorted(caps.items(), key=lambda kv: -(kv[1] or 0)) if c not in seen]
    except Exception:
        # kept broad: the KRX market-cap file is optional ordering input; any failure keeps the Universe order.
        logger.warning("DART priority: market caps unavailable", exc_info=True)
    return codes
