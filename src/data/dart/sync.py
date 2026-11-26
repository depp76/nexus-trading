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
  -> backfill -> segments of the reports received in the daily window only
  (plan 11-3-4: "새로 들어온 관심 종목 보고서만").
- ``segments``: revenue by business segment of the target companies, all
  pending periods (stage 4, plan 5; the initial load is this explicit run --
  most periods need the report document, a few MB each): the Universe's KOSPI/KOSDAQ stocks and ``segment_targets.json``;
  annual reports of the whole window and the quarterly reports of the last
  SEGMENT_QUARTER_YEARS fiscal years, newest first, once their financials are in.

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
import sqlite3
import uuid

import paths
from data.dart import corp as corp_mod
from data.dart import documents as docs
from data.dart import filings as fil
from data.dart import financials as fin
from data.dart import segments as seg
from data.dart.client import (
    DAILY_LIMIT, DartApiError, DartBudgetExceeded, DartCancelled, DartClient, api_key_from_env, calls_today,
    is_fatal, kst_today,
)
from data.dart.lock import SyncLock
from data.dart.store import connect, get_state, now_iso, open_readonly, set_state

logger = logging.getLogger(__name__)

DART_YEARS = 5                 # 5 business years + the current one (plan header)
DAILY_OVERLAP_DAYS = 3         # plan 11-3-2
MODES = ("init", "daily", "backfill", "auto", "segments")
_KEY_FROM_ENV = object()
_KST = _dt.timezone(_dt.timedelta(hours=9))
EVENING_HOUR = 19              # plan 11-2: one run after 19:00 KST catches the day's filings
INITIAL_PENDING_PERIODS = 1000  # more periods than this still to fetch = the initial load (plan 11-8)
MAX_CONSECUTIVE_ERRORS = 5     # per-period request failures in a row before the run gives up
# A report is listed at once but its statements are loaded into the financial
# API hours later. For this many days after a receipt, an answer without it (a
# 013, or still the previous version's numbers) means "not loaded yet": nothing
# is cached and the receipt stays pending (review_agy M4, review_codex P1).
RECENT_FILING_DAYS = 7
# Past that wait, a correction the API still answers with an older receipt for
# is not closed with the older numbers (review_codex 3 P1): it is kept as
# ``awaiting_correction`` and asked again every CORRECTION_RETRY_DAYS, without
# the reply cache, until CORRECTION_GIVE_UP_DAYS after its receipt.
CORRECTION_RETRY_DAYS = 7
CORRECTION_GIVE_UP_DAYS = 365
ATTACHMENT_ONLY_TAGS = fil.ATTACHMENT_ONLY_TAGS
FETCH_LOG_KEEP_DAYS = 90
# Bumped when store_filings keeps more than before: the market-wide list is then
# walked again (past windows come from the cache). 2: one fiscal year before
# the window is kept for non-December filers (review_agy H4).
LIST_VERSION = "2"
SEGMENT_QUARTER_YEARS = 2      # quarterly segment data for the current and the previous fiscal year
_SEGMENT_TARGETS_FILE = os.path.join(os.path.dirname(__file__), "segment_targets.json")


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


def is_due(last_run: str | None, last_status: str | None, now: _dt.datetime, at_start: bool,
           last_error: str | None = None) -> bool:
    """Plan 11-2. At app start: run unless a run already finished after today's
    19:00 KST. On the 30-minute check: run only after 19:00, and only if no run
    finished after 19:00 today.

    A run that failed, or that stopped because it was cancelled / its process
    ended (the app was closed mid-collection), does not count as tonight's run:
    reopening the app resumes it (plan 11-6; review_agy M2). A run stopped by
    the daily limit or a run budget does count -- there is nothing left to
    spend today."""
    now = now.astimezone(_KST) if now.tzinfo else now.replace(tzinfo=_KST)
    evening = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
    last = _parse_kst(last_run)
    err = (last_error or "").lower()
    interrupted = last_status == "partial" and err.startswith(("cancelled", "interrupted"))
    done_tonight = last is not None and last >= evening and last_status != "failed" and not interrupted
    if done_tonight:
        return False
    return at_start or now >= evening


def collection_status(db_path: str | None = None, today: _dt.date | None = None, years: int = DART_YEARS) -> dict:
    """What a status display needs, read from dart.db: last run (time, status,
    calls, new filings), whether the list backfill is done, the periods in the
    window and how many still lack financials, and today's calls / limit left.
    Read-only (no write lock; review_agy H1); without a dart.db, the empty state."""
    today = today or kst_now().date()
    empty = {"last_run": None, "last_status": None, "last_calls": None, "last_added": None, "last_error": None,
             "last_trigger": None, "last_run_status": None, "list_done": False, "periods_total": 0,
             "periods_pending": 0, "calls_today": 0, "calls_left": DAILY_LIMIT}
    conn = open_readonly(db_path or paths.DART_DB_FILE)
    if conn is None:
        return empty
    try:
        first = first_fiscal_year(today, years)
        run = conn.execute("SELECT status, calls, filings_added, error, finished_at, trigger FROM sync_run "
                           "WHERE status NOT IN ('running', 'skipped') ORDER BY started_at DESC LIMIT 1").fetchone()
        total, pending = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(p.missing > 0), 0) FROM ("
            " SELECT f.corp_code, f.bsns_year, f.reprt_code,"
            " SUM(x.rcept_no IS NULL OR x.status = 'awaiting_correction') AS missing"
            " FROM filing f LEFT JOIN fin_fetch x ON x.rcept_no = f.rcept_no WHERE f.bsns_year >= ?"
            " GROUP BY f.corp_code, f.bsns_year, f.reprt_code) p", (first,)).fetchone()
        used = calls_today(conn, today.isoformat())
        return {
            "last_run": get_state(conn, "last_run"),
            "last_status": get_state(conn, "last_run_status"),
            "last_calls": run["calls"] if run else None,
            "last_added": run["filings_added"] if run else None,
            "last_error": run["error"] if run else None,
            "last_trigger": run["trigger"] if run else None,
            "last_run_status": run["status"] if run else None,
            "list_done": get_state(conn, "list_backfill_done") == "1",
            "periods_total": int(total or 0),
            "periods_pending": int(pending or 0),
            "calls_today": used,
            "calls_left": max(0, DAILY_LIMIT - used),
        }
    except sqlite3.Error:
        logger.warning("DART status read failed", exc_info=True)
        return empty
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
    segments_done: int = 0
    periods_failed: int = 0           # financial periods whose request failed (left pending)
    segments_failed: int = 0          # segment periods whose request failed
    unknown_tickers: tuple = ()       # tickers with no corp code even after the company list


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
                if getattr(client, "_cancel", None) is None and hasattr(client, "_cancel"):
                    client._cancel = cancel          # waits inside the client stop on cancel too
            else:
                client = DartClient(conn, key, cache_dir, run_id=run_id, budget=budget, cancel=cancel)
            try:
                job = _Job(conn, client, today, years, cancel, on_progress, tickers, priority)
                status, error = "done", None
                try:
                    job.run(mode)
                except (DartBudgetExceeded, DartCancelled, _Cancelled) as e:
                    status, error = "partial", str(e) or "cancelled"
                    conn.rollback()
                except Exception as e:
                    # boundary: a run records any failure in sync_run instead of raising into the caller's thread.
                    logger.exception("DART sync failed (run %s)", run_id)
                    status, error = "failed", f"{type(e).__name__}: {e}"
                    conn.rollback()
                if status == "done" and (job.periods_failed or job.segments_failed):
                    # Requests that failed after their retries were skipped and left
                    # pending: the run is not "done" (review_codex 3 P2). A report
                    # whose statements DART has not loaded yet is a wait, not a failure.
                    status = "partial"
                    error = (f"request failures: {job.periods_failed} financial period(s), "
                             f"{job.segments_failed} segment period(s); left pending")
                calls = getattr(client, "calls", 0)
                pending = job.pending_count()
                set_state(conn, "last_run", kst_now().isoformat(timespec="seconds"))
                set_state(conn, "last_run_status", status)
                _prune_fetch_log(conn)
                _record_run(conn, run_id, trigger, mode, status, calls, job.filings_added, error, finished=True)
                return SyncResult(status, run_id, calls, job.filings_added, job.periods_fetched, pending, error,
                                  job.segments_done, job.periods_failed, job.segments_failed,
                                  tuple(job.unknown_tickers))
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        finally:
            lock.release()
    finally:
        conn.close()


def _prune_fetch_log(conn) -> None:
    """Drop fetch_log rows older than FETCH_LOG_KEEP_DAYS (one row per API call
    otherwise grows without end; review_agy L3). Not committed."""
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=FETCH_LOG_KEEP_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM fetch_log WHERE fetched_at < ?", (cutoff,))


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
        self.periods_failed = 0
        self.segments_done = 0
        self.segments_failed = 0
        self.unknown_tickers: list[str] = []
        self.account_map = fin.load_account_map()
        self._order: list[str] | None = None      # backfill order, built once per run
        self._order_has_caps = False

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
        if mode in ("init", "daily", "auto") or (mode in ("backfill", "segments") and self.tickers):
            self.sync_corp_list(force=(mode == "init"))
        if self.tickers:
            # A verification run: these companies' lists, then everything of theirs
            # still pending -- whatever the mode but "init" (lists only). `--daily
            # --tickers` used to stop after the lists (review_codex P2).
            self.list_for_tickers()
            if mode != "init":
                self.fetch_financials()
            if mode in ("segments", "auto"):
                self.fetch_segments()
            return
        if mode == "segments":
            self.fetch_segments()
            return
        if mode in ("init", "auto", "backfill") or get_state(self.conn, "list_backfill_done") != "1":
            self.list_backfill()
        if mode in ("daily", "auto"):
            since = self.daily_list()
            # Receipts left pending as "not loaded yet" stay in reach until they are
            # past that wait: the financial window starts RECENT_FILING_DAYS before
            # the list window, or a daily-only user would never get back to a
            # report answered 013 for a few days (review_codex P2).
            since = (_dt.date.fromisoformat(since) - _dt.timedelta(days=RECENT_FILING_DAYS + 1)).isoformat()
            self._daily_since = since
            self.fetch_financials(since=since)
            # Only now is the window "done": a run stopped before this line keeps the
            # old daily_until, so the next daily run covers the same days again
            # instead of leaving their financials to a later backfill (review_agy L4).
            set_state(self.conn, "daily_until", self.today.isoformat())
            self.conn.commit()
        if mode in ("backfill", "auto"):
            self.fetch_financials()
        if mode == "auto":
            self.fetch_segments(since=getattr(self, "_daily_since", None) or self.today.isoformat())

    # ---- company list ------------------------------------------------------------

    def sync_corp_list(self, force=False):
        last = get_state(self.conn, "corp_list_at")
        if last and not force:
            age = self.today - _dt.date.fromisoformat(last[:10])
            if age.days < corp_mod.CORP_LIST_MAX_AGE_DAYS:
                return
        # Also on a ticker run: a stock listed since the last list has no corp
        # code in an old one (review_codex 3 P2).
        self._progress(0, 1, "corp list")
        corp_mod.sync_corp_codes(self.client, self.conn)
        set_state(self.conn, "corp_list_at", self.today.isoformat())
        self.conn.commit()

    # ---- filing lists ------------------------------------------------------------

    def list_backfill(self):
        if get_state(self.conn, "list_backfill_done") == "1":
            if get_state(self.conn, "list_version", "1") == LIST_VERSION:
                return
            # The list rules changed: walk the windows again (cached pages, no calls
            # for past quarters) so filings the old rules dropped are stored.
            set_state(self.conn, "list_backfill_done", "0")
            set_state(self.conn, "list_backfill_cursor", "")
            self.conn.commit()
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
        set_state(self.conn, "list_version", LIST_VERSION)
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
                if t not in self.unknown_tickers:
                    self.unknown_tickers.append(t)
        return out

    # ---- financials --------------------------------------------------------------

    def _pending_sql(self, since=None):
        """Filings without financials, plus corrections awaiting their numbers
        whose retry day has come (review_codex 3 P1)."""
        give_up = (self.today - _dt.timedelta(days=CORRECTION_GIVE_UP_DAYS)).isoformat()
        sql = ("SELECT f.rcept_no, f.corp_code, f.reprt_code, f.bsns_year, f.rcept_dt, f.report_nm, c.stock_code, "
               "c.fiscal_month, x.status AS fetch_status "
               "FROM filing f JOIN company c ON c.corp_code = f.corp_code "
               "LEFT JOIN fin_fetch x ON x.rcept_no = f.rcept_no "
               "WHERE (x.rcept_no IS NULL OR (x.status = 'awaiting_correction' "
               "AND COALESCE(x.retry_after, '') <= ? AND f.rcept_dt >= ?)) AND f.bsns_year >= ?")
        args: list = [self.today.isoformat(), give_up, self.first_year]
        if since:
            sql += " AND f.rcept_dt >= ?"
            args.append(since)
        if self.tickers:
            sql += f" AND c.stock_code IN ({','.join('?' * len(self.tickers))})"
            args.extend(self.tickers)
        return sql, args

    def pending_count(self) -> int:
        """Periods (company, fiscal year, report) still without financials --
        the unit collection_status counts too (review_agy M9)."""
        try:
            sql, args = self._pending_sql()
            return self.conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM (" + sql + ") GROUP BY corp_code, bsns_year, reprt_code)",
                args).fetchone()[0]
        except Exception:
            # boundary: only a status figure; never fail the run's bookkeeping over it.
            logger.warning("DART pending count failed", exc_info=True)
            return -1

    def fetch_financials(self, since=None):
        """Financials of the pending filings (`since`: only those received from
        that day -- the daily phase, reported as "new financials"; the backfill
        phase as "financials").

        One period failing (a network error after the retries, a broken reply)
        is logged and left pending for the next run; the run stops only on a
        fatal status (key / service problems) or MAX_CONSECUTIVE_ERRORS failures
        in a row (review_agy H3)."""
        sql, args = self._pending_sql(since)
        pending = [dict(r) for r in self.conn.execute(sql, args)]
        if not pending:
            return                      # nothing to order: no Universe / market-cap lookup (review_agy M1)
        groups: dict = {}
        for r in pending:
            groups.setdefault((r["corp_code"], r["bsns_year"], r["reprt_code"]), []).append(r)
        rank = self._rank({g[0]["stock_code"] for g in groups.values()})
        q_of = fil.Q_BY_REPRT
        order = sorted(groups, key=lambda k: (rank(groups[k][0]["stock_code"]), k[0], k[1], q_of[k[2]]))
        total = len(order)
        label = "new financials" if since else "financials"
        errors_in_a_row = 0
        for i, key in enumerate(order):
            self._check()
            corp, fy, reprt = key
            members = groups[key]
            self._progress(i, total, f"{label} {members[0]['stock_code']} {fy} {reprt}")
            try:
                self._fetch_group(corp, fy, reprt, members)
            except DartApiError as e:
                self.conn.rollback()
                if is_fatal(e):
                    raise
                errors_in_a_row += 1
                self.periods_failed += 1
                logger.warning("DART %s %s %s skipped (left pending): %s", members[0]["stock_code"], fy, reprt, e)
                if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                    raise
                continue
            errors_in_a_row = 0
            self.conn.commit()
            self.periods_fetched += 1
        self._progress(total, total, f"{label} done")

    def _fetch_group(self, corp, fy, reprt, members):
        latest = max(m["rcept_no"] for m in members)
        fm = members[0]["fiscal_month"] or 12
        years = api_years(members, fy, fm)
        recent = any(self._is_recent(m) for m in members)
        # No reply cache for a recent receipt, a correction that changes the
        # statements (its first answers may still be the older numbers) or a
        # retry of one awaiting them: a stale reply must not be served again
        # (review_codex 3 P1).
        fresh = recent or any(m.get("fetch_status") == "awaiting_correction"
                              or (fil.parse_report_name(m.get("report_nm") or "") or (0, 0, 0, ()))[3]
                              and not _attachment_only(m.get("report_nm")) for m in members)
        fs_div, rows = None, []
        for api_year in years:
            fs_div, rows = fin.fetch_period(self.client, corp, api_year, reprt, cache_tag=None if fresh else latest)
            if rows and (fm == 12 or self._period_matches(rows, corp, fy, reprt)):
                break
            fs_div, rows = None, []
        if not rows and recent:
            # DART lists a report at once but loads its statements hours later:
            # leave it pending (nothing cached or marked) and ask again next run.
            return
        now = now_iso()
        kw = {}
        source = "api"
        if not rows:
            # 013 from the API (financial-sector filers before 2023 H2): read the
            # report's own income statement, newest receipt first (a correction
            # of attachments only has no statements; the next one is tried).
            for m in sorted(members, key=lambda m: m["rcept_no"], reverse=True):
                self._check()
                got = docs.fetch_document_income(self.client, m["rcept_no"], reprt, self._cache_dir())
                if got:
                    fs_div, rows, financial, nci = got
                    kw = {"raw": False, "financial": financial, "nci": nci}
                    source = "document"
                    break
        q = fil.Q_BY_REPRT[reprt]
        self.conn.execute(
            f"DELETE FROM fin_issue WHERE kind = 'no_data' AND rcept_no IN ({','.join('?' * len(members))})",
            [m["rcept_no"] for m in members])
        if not rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at) "
                "VALUES (?, 'no_data', NULL, NULL, ?)", [(m["rcept_no"], now) for m in members])
            fin.record_issue(self.conn, latest, corp, fy, q, "no_data",
                             "no API data and no income statement found in the document")
            return
        src, missing = fin.store_period(self.conn, corp, fy, reprt, fs_div, rows, self.account_map,
                                        fallback_rcept=latest, **kw)
        if source == "document":
            fin.record_issue(self.conn, src, corp, fy, q, "from_document",
                             "API 013; read from the report's income statement table")
        status = "unmapped" if missing else ("document" if source == "document" else "ok")
        # A correction newer than the receipt the API answered with is not in
        # these numbers yet. Within RECENT_FILING_DAYS it simply stays pending
        # (asked again next run); after that it is kept as awaiting_correction
        # and asked again every CORRECTION_RETRY_DAYS -- never closed with the
        # older numbers (review_codex P1, review_codex 3 P1). An attachment-only
        # correction changes no statement and is done with the rest.
        stale = [m for m in members
                 if source == "api" and m["rcept_no"] > src and not _attachment_only(m.get("report_nm"))]
        waiting = {m["rcept_no"] for m in stale if self._is_recent(m)}
        awaiting = {m["rcept_no"] for m in stale} - waiting
        done = ({m["rcept_no"] for m in members} - waiting - awaiting) | {src}
        self.conn.executemany(
            "INSERT OR REPLACE INTO fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at) VALUES (?, ?, ?, ?, ?)",
            [(r, status, src, fs_div, now) for r in sorted(done)])
        retry = (self.today + _dt.timedelta(days=CORRECTION_RETRY_DAYS)).isoformat()
        for r in sorted(awaiting):
            self.conn.execute(
                "INSERT OR REPLACE INTO fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at, retry_after) "
                "VALUES (?, 'awaiting_correction', ?, ?, ?, ?)", (r, src, fs_div, now, retry))
            fin.record_issue(self.conn, r, corp, fy, q, "correction_not_served",
                             f"the API still answers with {src}; asked again from {retry}")

    # ---- segments (stage 4) -------------------------------------------------------

    def segment_targets(self) -> list[str]:
        """Stock codes whose segments are collected: the given tickers, else the
        Universe's KOSPI/KOSDAQ stocks and segment_targets.json (plan 5-5)."""
        if self.tickers:
            return list(self.tickers)
        if self.priority is not None:
            return list(self.priority)          # an explicit order (tests, callers) is the target list too
        codes = universe_priority()
        try:
            with open(_SEGMENT_TARGETS_FILE, encoding="utf-8") as f:
                extra = [str(c).zfill(6) for c in json.load(f).get("stocks", [])]
        except (OSError, ValueError, AttributeError):
            extra = []
        seen = set(codes)
        return codes + [c for c in extra if c not in seen]

    def _segment_pending(self, targets: list[str], since: str | None = None) -> list[dict]:
        if not targets:
            return []
        first_q_year = self.today.year - SEGMENT_QUARTER_YEARS + 1
        self.conn.execute("DROP TABLE IF EXISTS temp.seg_targets")
        self.conn.execute("CREATE TEMP TABLE seg_targets(stock_code TEXT PRIMARY KEY, pos INTEGER)")
        self.conn.executemany("INSERT OR IGNORE INTO temp.seg_targets VALUES (?, ?)",
                              [(c, i) for i, c in enumerate(targets)])
        rows = self.conn.execute(
            "SELECT f.corp_code, f.bsns_year, f.reprt_code, MAX(f.rcept_no) AS rcept_no, t.pos, c.stock_code "
            "FROM filing f JOIN company c ON c.corp_code = f.corp_code "
            "JOIN temp.seg_targets t ON t.stock_code = c.stock_code "
            "WHERE f.bsns_year >= ? AND (f.reprt_code = '11011' OR f.bsns_year >= ?) "
            "GROUP BY f.corp_code, f.bsns_year, f.reprt_code "
            "HAVING MAX(f.rcept_dt) >= ?", (self.first_year, first_q_year, since or "0000")).fetchall()
        done = {r[0] for r in self.conn.execute("SELECT rcept_no FROM seg_fetch")}
        q_of = fil.Q_BY_REPRT
        out = [dict(r) for r in rows if r["rcept_no"] not in done]
        out.sort(key=lambda r: (r["pos"], -r["bsns_year"], -q_of[r["reprt_code"]]))
        return out

    def fetch_segments(self, since: str | None = None):
        """Segment revenue of the target companies' pending periods (plan 5);
        with `since` only periods with a report received from that day.
        A period waits until its financials are in (the check needs revenue);
        one period failing is skipped like a financial period (H3)."""
        pending = self._segment_pending(self.segment_targets(), since)
        total = len(pending)
        errors_in_a_row = 0
        for i, p in enumerate(pending):
            self._check()
            self._progress(i, total, f"segments {p['stock_code']} {p['bsns_year']} {p['reprt_code']}")
            try:
                if self._segment_period(p):
                    self.segments_done += 1
            except DartApiError as e:
                self.conn.rollback()
                if is_fatal(e):
                    raise
                errors_in_a_row += 1
                self.segments_failed += 1
                logger.warning("DART segments %s %s %s skipped: %s", p["stock_code"], p["bsns_year"],
                               p["reprt_code"], e)
                if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                    raise
                continue
            errors_in_a_row = 0
            self.conn.commit()
        if total:
            self._progress(total, total, "segments done")

    def _segment_period(self, p: dict) -> bool:
        """Read, check and store one period's segments; False when it has to
        wait for its financials."""
        corp, fy, reprt, rcept = p["corp_code"], p["bsns_year"], p["reprt_code"], p["rcept_no"]
        q = fil.Q_BY_REPRT[reprt]
        # The receipt's own financials must be in: a correction whose statements
        # DART has not loaded yet is still pending in fin_fetch, and checking its
        # segment table against the *previous* version's revenue would settle a
        # false mismatch for good (review_codex 2 P1). The revenue is the one
        # stored for this receipt (fin_fetch.src_rcept_no; an attachment-only
        # correction points at the original).
        ff = self.conn.execute("SELECT status, src_rcept_no FROM fin_fetch WHERE rcept_no = ?", (rcept,)).fetchone()
        if ff is None:
            return False
        rev = None
        if ff["src_rcept_no"]:
            rev = self.conn.execute(
                "SELECT value_q, value_ytd, fs_div FROM fin_std WHERE corp_code = ? AND fiscal_year = ? "
                "AND fiscal_q = ? AND metric = 'revenue' AND rcept_no = ?", (corp, fy, q, ff["src_rcept_no"])).fetchone()
        rcept_dt = self.conn.execute("SELECT rcept_dt FROM filing WHERE rcept_no = ?", (rcept,)).fetchone()[0]
        if rev is None:
            na = self.conn.execute(
                "SELECT 1 FROM fin_issue WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = ? "
                "AND kind = 'revenue_na'", (corp, fy, q)).fetchone()
            if na:
                seg.record_fetch(self.conn, rcept, "na", None, "financial filer: no revenue to check against")
                return True
            return False
        rq, ry, fs_div = rev["value_q"], rev["value_ytd"], rev["fs_div"]
        cache = self._cache_dir()
        xc = seg.fetch_xbrl(self.client, rcept, reprt, fs_div, cache)
        chosen, basis, residual, status = seg.choose(xc, rq, ry)
        if status != "ok":
            self._check()
            dc, single = seg.fetch_document(self.client, rcept, fs_div, cache)
            d_chosen, d_basis, d_residual, d_status = seg.choose(dc, rq, ry)
            if d_status == "ok":
                chosen, basis, residual, status = d_chosen, d_basis, d_residual, d_status
            elif single:
                chosen, basis = seg.single_segment(rq, ry, reprt)
                residual, status = 0.0, "ok"
            elif status != "sum_mismatch" and d_status == "sum_mismatch":
                chosen, basis, residual, status = d_chosen, d_basis, d_residual, d_status
        source = chosen.source if chosen is not None else None
        seg.store_segments(self.conn, corp, fy, q, rcept, rcept_dt, chosen, basis, residual, status, source or "")
        seg.record_fetch(self.conn, rcept, status, source, chosen.label if chosen is not None else "")
        return True

    def _is_recent(self, member) -> bool:
        return (self.today - _dt.date.fromisoformat(member["rcept_dt"][:10])).days <= RECENT_FILING_DAYS

    def _cache_dir(self) -> str:
        return getattr(self.client, "cache_dir", None) or paths.DART_CACHE_DIR

    def _period_matches(self, rows, corp, fy, reprt) -> bool:
        """For a non-December filer: whether the reply's receipt belongs to the
        wanted period (an unknown or missing receipt is accepted)."""
        src = fin.source_receipt(rows)
        if src is None:
            return True
        row = self.conn.execute("SELECT bsns_year, reprt_code FROM filing WHERE rcept_no = ?", (src,)).fetchone()
        return row is None or (row[0] == fy and row[1] == reprt)

    def _rank(self, stocks: set):
        """Position of a stock in the backfill order. Built once per run; the
        KRX market-cap file is asked for only when a pending stock is outside
        the Universe list (review_agy M1)."""
        if self.priority is not None:
            order = list(self.priority)
        else:
            if self._order is None:
                self._order = universe_priority()
            if not self._order_has_caps and not stocks <= set(self._order):
                self._order = self._order + market_cap_priority(set(self._order))
                self._order_has_caps = True
            order = self._order
        pos = {code: i for i, code in enumerate(order)}
        n = len(pos)
        return lambda stock: pos.get(stock, n)


_attachment_only = fil.is_attachment_only


def api_years(members, fiscal_year: int, fiscal_month: int) -> list[int]:
    """The ``bsns_year`` values to ask the financial API for, in order. DART's
    is the calendar year in which the reported period ends (checked 2026-11-26
    on March / June year-end filers: "분기보고서 (2026.06)" of the year ending
    2027-03 is bsns_year 2026), so the report name gives it directly; without
    a name (a stub filing), the fiscal year and the one before."""
    for m in sorted(members, key=lambda m: m["rcept_no"], reverse=True):
        parsed = fil.parse_report_name(m.get("report_nm") or "")
        if parsed:
            return [parsed[1]]
    return [fiscal_year] if fiscal_month == 12 else [fiscal_year, fiscal_year - 1]


def universe_priority() -> list[str]:
    """The Universe cache's KOSPI/KOSDAQ stock codes by market cap (plan 11-3-3)."""
    try:
        with open(paths.UNIVERSE_CACHE_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        kr = [r for r in rows if isinstance(r, dict) and r.get("market") in ("KOSPI", "KOSDAQ") and r.get("ticker")]
        kr.sort(key=lambda r: -(r.get("market_cap") or 0))
        return [str(r["ticker"]).zfill(6) for r in kr]
    except (OSError, ValueError, TypeError):
        logger.info("DART priority: no Universe cache", exc_info=True)
        return []


def market_cap_priority(seen: set) -> list[str]:
    """Every other KRX stock by its latest market cap (one download of the KRX file)."""
    try:
        from data.listing import _fetch_recent_marcaps
        _day, caps = _fetch_recent_marcaps()
        return [c for c, _ in sorted(caps.items(), key=lambda kv: -(kv[1] or 0)) if c not in seen]
    except Exception:
        # kept broad: the KRX market-cap file is optional ordering input; any failure keeps the Universe order.
        logger.warning("DART priority: market caps unavailable", exc_info=True)
        return []


def default_priority() -> list[str]:
    """Stock codes in backfill order (plan 11-3-3): the Universe's KOSPI/KOSDAQ
    rows by market cap, then every other KRX stock by its latest market cap."""
    codes = universe_priority()
    return codes + market_cap_priority(set(codes))
