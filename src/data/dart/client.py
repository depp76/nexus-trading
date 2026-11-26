"""OpenDART HTTP client (docs/dart_db_plan_2026-11-26.md 2, 6, 11-4).

- The key comes from ``DART_API_KEY`` (environment / ``.env``) and never reaches a
  log line, the cache file names or ``fetch_log``.
- Every network call is first counted in ``api_usage`` for today's Korean date;
  the counter is shared by the app, the CLI and a scheduler run through dart.db,
  and a call that would pass the daily limit (19,000) raises ``DartBudgetExceeded``
  instead of being sent. A per-run ``budget`` stops a run earlier the same way.
- Status ``013`` (no data) is a normal answer; ``020`` (limit reached on DART's
  side) raises ``DartBudgetExceeded``; other non-000 codes raise ``DartApiError``.
- Network errors, 5xx and 429 replies are retried with exponential back-off
  (a 429's Retry-After is honoured up to a minute).
- Calls are spaced at least ``min_interval`` seconds apart (<= 5 per second).
- Every wait (spacing, back-off) watches the run's ``cancel`` and raises
  ``DartCancelled`` at once, so closing the app never sits out a back-off.
- Replies are cached under ``cache/dart/`` so re-parsing never calls the API
  again; callers decide what is cacheable through ``cache_tag``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import time

import requests

from data.cache import SharedTlsAdapter
from data.dart.store import now_iso

logger = logging.getLogger(__name__)

BASE_URL = "https://opendart.fss.or.kr/api/"
DAILY_LIMIT = 19_000          # stop below DART's ~20,000 per key per day (plan 2)
MIN_INTERVAL = 0.2            # seconds between calls: at most 5 per second (plan 11-4)
_KST = _dt.timezone(_dt.timedelta(hours=9))

STATUS_OK = "000"
STATUS_NO_DATA = "013"
STATUS_LIMIT = "020"


class DartApiError(Exception):
    """A non-000/013 status, or a reply that is not what the endpoint returns."""

    def __init__(self, status: str, message: str = ""):
        super().__init__(f"DART status {status}: {message}")
        self.status = status


class DartBudgetExceeded(Exception):
    """The daily limit (api_usage), this run's budget, or DART's own 020."""


class DartCancelled(Exception):
    """The run's cancel was set while the client was waiting."""


# Statuses that make every further call pointless: the run stops. Anything
# else (a network failure after the retries, a broken reply, one report DART
# cannot serve) concerns one request and the run may go on (review_agy H3).
FATAL_STATUSES = frozenset({"010", "011", "012", "800", "900", "901", "http_401", "http_403"})


# DART's own "nothing there" answers to a file request (013 no data, 014 no
# file). Only these may be remembered as "none" (a negative cache, a done mark);
# any other failure -- network, a service status, a broken zip -- says nothing
# about the file and must leave the receipt pending (review_codex 2 P1).
NO_FILE_STATUSES = frozenset({"013", "014"})


def is_no_file(error: DartApiError) -> bool:
    return str(error.status) in NO_FILE_STATUSES


def is_fatal(error: DartApiError) -> bool:
    return str(error.status) in FATAL_STATUSES


def kst_today() -> str:
    return _dt.datetime.now(_KST).strftime("%Y-%m-%d")


def api_key_from_env() -> str | None:
    """DART_API_KEY from the environment, loading the repo's .env first if needed."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        try:
            from dotenv import load_dotenv
            import paths
            load_dotenv(paths.ENV_FILE)
        except ImportError:
            return None
        key = os.environ.get("DART_API_KEY")
    key = (key or "").strip()
    return key or None


def calls_today(conn: sqlite3.Connection, day: str | None = None) -> int:
    row = conn.execute("SELECT calls FROM api_usage WHERE kst_date = ?", (day or kst_today(),)).fetchone()
    return int(row[0]) if row else 0


class DartClient:
    """One run's view of OpenDART. `conn` is the run's dart.db connection (usage
    counter and fetch_log); `session` can be a fake in tests."""

    def __init__(self, conn: sqlite3.Connection, api_key: str, cache_dir: str, *,
                 run_id: str = "", daily_limit: int = DAILY_LIMIT, budget: int | None = None,
                 min_interval: float = MIN_INTERVAL, session=None, retries: int = 3,
                 backoff: float = 1.0, today=kst_today, cancel=None):
        self.conn = conn
        self._key = api_key
        self.cache_dir = cache_dir
        self.run_id = run_id
        self.daily_limit = daily_limit
        self.budget = budget
        self.min_interval = min_interval
        self.retries = retries
        self.backoff = backoff
        self._today = today
        self.calls = 0                 # network calls made by this client
        self.cache_hits = 0
        self._last_call = 0.0
        self._cancel = cancel            # threading.Event, a callable, or None
        self._owns_session = session is None
        if session is None:
            session = requests.Session()
            session.mount("https://", SharedTlsAdapter())
        self.session = session

    def close(self) -> None:
        """Close the HTTP session this client created (a given one is the caller's)."""
        if self._owns_session:
            try:
                self.session.close()
            except Exception:
                # boundary: closing sockets at the end of a run must never raise into it.
                logger.debug("DART session close failed", exc_info=True)

    def _cancelled(self) -> bool:
        c = self._cancel
        if c is None:
            return False
        return c.is_set() if hasattr(c, "is_set") else bool(c())

    def _wait(self, seconds: float) -> None:
        """Sleep, waking at once (DartCancelled) when the run is cancelled."""
        if self._cancelled():
            raise DartCancelled("cancelled")
        if seconds <= 0:
            return
        c = self._cancel
        if c is not None and hasattr(c, "wait"):
            if c.wait(seconds):
                raise DartCancelled("cancelled")
            return
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))
            if self._cancelled():
                raise DartCancelled("cancelled")

    # ---- public ------------------------------------------------------------------

    def get_json(self, endpoint: str, params: dict, cache_tag: str | None = None) -> dict:
        """The decoded reply of `endpoint` ("list.json", ...). With a `cache_tag`
        the reply (000 or 013) is cached under it and served from there next
        time; without one nothing is read from or written to the cache."""
        path = self._cache_path(endpoint, params, cache_tag, ".json") if cache_tag else None
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.cache_hits += 1
                    return json.load(f)
            except (OSError, ValueError):
                logger.warning("Unreadable DART cache file %s; fetching again", path, exc_info=True)
        resp = self._request(endpoint, params)
        try:
            data = resp.json()
        except ValueError as e:
            self._log(endpoint, params, "bad_reply", str(e))
            raise DartApiError("bad_reply", f"{endpoint} did not return JSON") from e
        status = str(data.get("status", ""))
        self._log(endpoint, params, status, None if status in (STATUS_OK, STATUS_NO_DATA) else data.get("message"))
        self._raise_for_status(status, data.get("message", ""))
        if path:
            self._write_cache(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return data

    def get_bytes(self, endpoint: str, params: dict, cache_tag: str | None = None, suffix: str = ".zip") -> bytes:
        """A binary reply (corpCode.xml's zip). DART answers errors on these
        endpoints with a small XML/JSON body instead of a zip."""
        path = self._cache_path(endpoint, params, cache_tag, suffix) if cache_tag else None
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    self.cache_hits += 1
                    return f.read()
            except OSError:
                logger.warning("Unreadable DART cache file %s; fetching again", path, exc_info=True)
        resp = self._request(endpoint, params)
        body = resp.content
        if not body.startswith(b"PK"):
            status, message = _error_from_body(body)
            self._log(endpoint, params, status, message)
            self._raise_for_status(status, message)
            raise DartApiError(status or "bad_reply", message or f"{endpoint} did not return a zip")
        self._log(endpoint, params, STATUS_OK, None)
        if path:
            self._write_cache(path, body)
        return body

    # ---- internals ---------------------------------------------------------------

    def _reserve_call(self) -> None:
        """Count one call in api_usage, or raise when the day's limit or this
        run's budget is used up. The conditional UPDATE makes the check and the
        increment one step, so two processes cannot both take the last call."""
        if self.budget is not None and self.calls >= self.budget:
            raise DartBudgetExceeded(f"run budget of {self.budget} calls used")
        day = self._today()
        self.conn.execute("INSERT OR IGNORE INTO api_usage(kst_date, calls) VALUES (?, 0)", (day,))
        cur = self.conn.execute(
            "UPDATE api_usage SET calls = calls + 1 WHERE kst_date = ? AND calls < ?", (day, self.daily_limit))
        self.conn.commit()
        if cur.rowcount == 0:
            raise DartBudgetExceeded(f"daily limit of {self.daily_limit} calls reached ({day} KST)")
        self.calls += 1

    def _request(self, endpoint: str, params: dict):
        last_exc = None
        for attempt in range(self.retries):
            self._reserve_call()
            self._wait(self.min_interval - (time.monotonic() - self._last_call))
            self._last_call = time.monotonic()
            delay = self.backoff * (2 ** attempt)
            try:
                resp = self.session.get(BASE_URL + endpoint, params={**params, "crtfc_key": self._key},
                                        timeout=(10, 30))
                if resp.status_code == 429:
                    delay = max(delay, _retry_after(resp))
                    raise requests.HTTPError("HTTP 429")
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    self._log(endpoint, params, f"http_{resp.status_code}", None)
                    raise DartApiError(f"http_{resp.status_code}", endpoint)
                return resp
            except requests.RequestException as e:
                last_exc = e
                self._log(endpoint, params, "network_error", _scrub(str(e), self._key))
                if attempt + 1 < self.retries:
                    self._wait(delay)
        raise DartApiError("network_error", _scrub(str(last_exc), self._key))

    def _raise_for_status(self, status: str, message: str) -> None:
        if status in (STATUS_OK, STATUS_NO_DATA):
            return
        if status == STATUS_LIMIT:
            raise DartBudgetExceeded(f"DART refused: {message} (020)")
        raise DartApiError(status, message)

    def _log(self, endpoint: str, params: dict, status: str, error) -> None:
        self.conn.execute(
            "INSERT INTO fetch_log(endpoint, params, status, fetched_at, error, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (endpoint, json.dumps(params, ensure_ascii=False, sort_keys=True), status, now_iso(),
             error, self.run_id))
        self.conn.commit()

    def _cache_path(self, endpoint: str, params: dict, tag: str, suffix: str) -> str:
        name = endpoint.split(".")[0]
        digest = hashlib.sha1(json.dumps([params, tag], sort_keys=True).encode("utf-8")).hexdigest()[:16]
        sub = str(params.get("corp_code") or "_all")
        return os.path.join(self.cache_dir, name, sub, f"{tag}_{digest}{suffix}".replace("/", "_"))

    @staticmethod
    def _write_cache(path: str, body: bytes) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError:
            logger.warning("Could not write DART cache file %s", path, exc_info=True)


def _retry_after(resp) -> float:
    """Seconds a 429 asks to wait (0 when absent or unreadable), at most 60."""
    try:
        return min(60.0, max(0.0, float(resp.headers.get("Retry-After", 0))))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _scrub(text: str, key: str) -> str:
    """`text` with the API key removed (request URLs end up in exception text)."""
    return text.replace(key, "***") if key else text


def _error_from_body(body: bytes) -> tuple[str, str]:
    """(status, message) of an error reply to a zip endpoint (XML or JSON)."""
    text = body[:2000].decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        return str(data.get("status", "")), str(data.get("message", ""))
    except ValueError:
        pass
    st = re.search(r"<status>(.*?)</status>", text)
    msg = re.search(r"<message>(.*?)</message>", text)
    return (st.group(1) if st else ""), (msg.group(1) if msg else "")
