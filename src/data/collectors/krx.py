"""data/collectors/krx.py — KRX Open API (VKOSPI, derivative indices) and JP10Y bond data."""
import io
import os
import json
import threading
import time
import requests
from datetime import datetime, timedelta, date as _date
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import polars as pl
import logging

from paths import JP10Y_CACHE_FILE, VKOSPI_CACHE_FILE
from data.cache import (
    PAYLOAD_ERRORS,
    start_date,
    _JP10Y_CACHE,
    _JP10Y_LOCK,
    _HIST_CACHE_STALE_TTL,
    _MOF_SESSION,
    _pdf_is_stale,
    safe_float,
)

logger = logging.getLogger(__name__)

VKOSPI_INDEX_NAME = "코스피 200 변동성지수"
_KRX_DERIV_IDX_URL = "https://openapi.krx.co.kr/OPN/DER/01/0101/der_0101_tab1.jsp"
_KRX_KEY_CACHE: dict = {}
_KRX_VKOSPI_CACHE_PATH = VKOSPI_CACHE_FILE
_VKOSPI_CACHE_LOCK = threading.Lock()
_VKOSPI_FAIL_COOLDOWN_SEC = 1800
_VKOSPI_LAST_FAIL_TS: dict = {"ts": 0.0}
# openapi.krx.co.kr's derivative index endpoint has been returning a persistent 403 WAF
# block since 2026-08-28 regardless of date/headers/retries (confirmed by manual testing
# on 2026-09-02) — an IP/key-level block on KRX's side that the existing cooldown can't
# resolve. Disabled here to stop burning requests against a dead endpoint; flip back to
# False once KRX support confirms the block on this key/IP is lifted.
_VKOSPI_API_DISABLED = True


def _get_krx_auth_key() -> str:
    """Reads the KRX Open API auth key from the KRX_AUTH_KEY env var (.env)."""
    if "key" in _KRX_KEY_CACHE:
        return _KRX_KEY_CACHE["key"]
    key = os.getenv("KRX_AUTH_KEY")
    if not key:
        raise ValueError(
            "KRX_AUTH_KEY is not set. Please add "
            "KRX_AUTH_KEY=your_issued_key to the .env file in the project root. "
            "(Sign up at data.krx.co.kr → apply for 'Derivative Index Market Price Info' API at openapi.krx.co.kr)"
        )
    _KRX_KEY_CACHE["key"] = key
    return key


def fetch_krx_derivative_index_day(bas_dd: str, index_name: str = VKOSPI_INDEX_NAME) -> dict | None:
    """Fetches one trading day's row for a KRX derivative index (default: VKOSPI).

    bas_dd: 'YYYYMMDD'.
    """
    if _VKOSPI_API_DISABLED:
        return None
    res = requests.get(
        _KRX_DERIV_IDX_URL,
        headers={"AUTH_KEY": _get_krx_auth_key()},
        params={"basDd": bas_dd},
        timeout=10,
    )
    if res.status_code != 200:
        try:
            err = res.json()
            msg = err.get("respMsg", res.text)
            code = err.get("respCode", res.status_code)
        except (ValueError, AttributeError):
            # AttributeError: an error body that decodes to a non-dict.
            logger.debug("Failed to parse KRX API error response as JSON, using raw text", exc_info=True)
            msg, code = res.text, res.status_code
        raise ValueError(
            f"KRX Open API Error (Code: {code}): {msg}. "
            f"Please check the API approval status for 'Derivative Index Market Price Info' on the openapi.krx.co.kr my page."
        )
    for item in res.json().get("OutBlock_1", []) or []:
        if item.get("IDX_NM") == index_name:
            return item
    return None


def fetch_vkospi(date_str: str = None) -> float:
    """Returns the VKOSPI closing value for a given date (default: today)."""
    bas_dd = (date_str or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    item = fetch_krx_derivative_index_day(bas_dd)
    return safe_float(item.get("CLSPRC_IDX")) if item else 0.0


def _load_vkospi_cache() -> dict:
    if os.path.exists(_KRX_VKOSPI_CACHE_PATH):
        try:
            with open(_KRX_VKOSPI_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            logger.debug("Failed to load VKOSPI cache file, starting fresh", exc_info=True)
    return {}


def _save_vkospi_cache(cache: dict):
    try:
        temp_path = _KRX_VKOSPI_CACHE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(temp_path, _KRX_VKOSPI_CACHE_PATH)
    except OSError:
        logger.warning("VKOSPI cache save error", exc_info=True)


def fetch_vkospi_history(start: str, end: str = None, max_workers: int = 3, request_delay: float = 0.3) -> pl.DataFrame:
    """Builds daily VKOSPI OHLC history for [start, end] (default end: today).

    max_workers/request_delay default to a modest, throttled pace: a cold-start backfill
    can span a year of business days, and hammering the KRX Open API with a large burst of
    concurrent requests has triggered a WAF-level block (403 on every request, including
    ones with a valid, approved auth key) that persists for a while. While such a block is
    active, every missing date fails, and since _pdf_is_stale() re-triggers this function on
    every call until the missing dates are backfilled, callers (chart renders, the 60s
    auto-refresh timer) would otherwise retry against the still-blocked endpoint indefinitely.
    _VKOSPI_LAST_FAIL_TS enforces a cooldown after an all-failed attempt so retries back off
    instead of continuously feeding the block.
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    bas_dds = [d.strftime("%Y%m%d") for d in pd.bdate_range(start, end)]

    def _throttled_fetch(bd):
        time.sleep(request_delay)
        return fetch_krx_derivative_index_day(bd)

    with _VKOSPI_CACHE_LOCK:
        cache = _load_vkospi_cache()
        today_bd = datetime.now().strftime("%Y%m%d")
        missing = [bd for bd in bas_dds if bd not in cache or (cache[bd] is None and bd == today_bd)]

        if _VKOSPI_API_DISABLED:
            missing = []

        now_ts = time.time()
        if missing and (now_ts - _VKOSPI_LAST_FAIL_TS["ts"]) < _VKOSPI_FAIL_COOLDOWN_SEC:
            missing = []

        if missing:
            any_success = False
            with ThreadPoolExecutor(max_workers=max_workers) as exe:
                futures = {exe.submit(_throttled_fetch, bd): bd for bd in missing}
                for fut in as_completed(futures):
                    bd = futures[fut]
                    try:
                        item = fut.result()
                    except PAYLOAD_ERRORS:
                        logger.warning("KRX VKOSPI fetch error for date=%s", bd, exc_info=True)
                        continue
                    any_success = True
                    if item:
                        cache[bd] = item
                    elif bd != today_bd:
                        cache[bd] = None
            _VKOSPI_LAST_FAIL_TS["ts"] = 0.0 if any_success else now_ts
            _save_vkospi_cache(cache)

    rows = []
    for bd in bas_dds:
        item = cache.get(bd)
        if item:
            rows.append({
                "Date": _date(int(bd[:4]), int(bd[4:6]), int(bd[6:8])),
                "Open": safe_float(item.get("OPNPRC_IDX")),
                "High": safe_float(item.get("HGPRC_IDX")),
                "Low": safe_float(item.get("LWPRC_IDX")),
                "Close": safe_float(item.get("CLSPRC_IDX")),
                "Volume": 0,
            })
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort("Date")


_VKOSPI_PDF_CACHE: dict = {"df": None, "checked_at": None}
_VKOSPI_PDF_LOCK = threading.Lock()


def _get_vkospi_pdf() -> pd.DataFrame:
    """pandas/DatetimeIndex wrapper matching the _get_kr3y_df / _get_jp10y_df convention.
    A frame whose last bar predates today is checked again at most once per
    _HIST_CACHE_STALE_TTL: while the KRX API is disabled the series never gets
    today's bar, and every weekday call used to re-read vkospi_cache.json (L2)."""
    with _VKOSPI_PDF_LOCK:
        cached = _VKOSPI_PDF_CACHE["df"]
        checked_at = _VKOSPI_PDF_CACHE["checked_at"]
        recently_checked = checked_at is not None and datetime.now() - checked_at < _HIST_CACHE_STALE_TTL
        if recently_checked or (cached is not None and not _pdf_is_stale(cached)):
            return cached
        df = fetch_vkospi_history(start_date())
        _VKOSPI_PDF_CACHE["checked_at"] = datetime.now()
        if df.is_empty():
            return cached
        pdf = df.to_pandas()
        pdf["Date"] = pd.to_datetime(pdf["Date"])
        pdf = pdf.set_index("Date")
        _VKOSPI_PDF_CACHE["df"] = pdf
        return pdf


_JP10Y_BASE_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
_JP10Y_CSV_PATHS = ("historical/jgbcme_all.csv", "jgbcme.csv")
# (connect, read) seconds. The connect timeout is per attempt: _MOF_SESSION retries a
# failed connect three times, so a dropped connection costs 3 s instead of the ~21 s OS
# default and the worst case is ~12 s per CSV. The historical CSV is ~1.2 MB.
_JP10Y_TIMEOUT = (3, 20)
# A downloaded series is not downloaded again within this window (MOF publishes once
# a business day; same window as data.cache._HIST_CACHE_STALE_TTL).
_JP10Y_REFETCH_TTL = timedelta(minutes=30)


def _read_mof_csv(path: str) -> pd.DataFrame:
    res = _MOF_SESSION.get(_JP10Y_BASE_URL + path, timeout=_JP10Y_TIMEOUT)
    res.raise_for_status()
    return pd.read_csv(io.BytesIO(res.content), skiprows=1, encoding='shift_jis')


def _load_jp10y_disk_cache():
    """Last downloaded series from JP10Y_CACHE_FILE ({YYYY-MM-DD: yield %}), or None."""
    try:
        with open(JP10Y_CACHE_FILE, encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("JP10Y disk cache unreadable, ignoring", exc_info=True)
        return None
    if not rows:
        return None
    df = pd.DataFrame({"Close": [float(v) for v in rows.values()]},
                      index=pd.to_datetime(list(rows.keys())))
    df.index.name = "Date"
    return df.sort_index()


def _save_jp10y_disk_cache(df: pd.DataFrame) -> None:
    rows = {d.strftime("%Y-%m-%d"): float(v) for d, v in df["Close"].items()}
    try:
        os.makedirs(os.path.dirname(JP10Y_CACHE_FILE), exist_ok=True)
        tmp = JP10Y_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, JP10Y_CACHE_FILE)
    except OSError:
        logger.warning("JP10Y disk cache save failed", exc_info=True)


def _parse_mof_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One MOF CSV as a date-indexed Close (10Y yield) frame, oldest first."""
    df = df.rename(columns={'10Y': 'Close'})
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
    return (df[['Date', 'Close']]
            .dropna(subset=['Date', 'Close'])
            .sort_values('Date')
            .set_index('Date'))


def _download_jp10y(cached):
    """The whole series from the network. With a cached series that already
    reaches into the month of the current-month CSV, only that small CSV is
    downloaded and laid over the cache; the 1.2 MB historical CSV (everything
    up to the end of last month) is needed only on the first run and once
    after each month turns."""
    hist_path, recent_path = _JP10Y_CSV_PATHS
    if cached is None or cached.empty:
        frames = [_read_mof_csv(hist_path), _read_mof_csv(recent_path)]
        return _parse_mof_frame(pd.concat(frames, ignore_index=True))
    recent = _parse_mof_frame(_read_mof_csv(recent_path))
    if recent.empty:
        return cached
    if cached.index[-1] >= recent.index[0]:
        merged = pd.concat([cached[cached.index < recent.index[0]], recent])
        merged.index.name = recent.index.name
        return merged
    hist = _parse_mof_frame(_read_mof_csv(hist_path))
    return pd.concat([hist[hist.index < recent.index[0]], recent])


def _get_jp10y_df():
    """Fetch Japan 10-year government bond yield.

    The last downloaded series is also kept in JP10Y_CACHE_FILE, so a failed download
    right after startup still returns yesterday's series instead of None. MOF publishes
    with a lag of a few business days, so on weekdays the series is always "stale" by
    _pdf_is_stale; a series downloaded within _JP10Y_REFETCH_TTL is served as it is
    (until 2026-11-16 every full refresh downloaded both CSVs again, about 2.6 s, and
    the index step waited for it).
    """
    with _JP10Y_LOCK:
        cached = _JP10Y_CACHE["df"]
        if cached is None:
            cached = _load_jp10y_disk_cache()
            _JP10Y_CACHE["df"] = cached
        if cached is not None and not _pdf_is_stale(cached):
            return cached
        # (frame, time) of the last download; it counts only for that same frame object.
        fetched = _JP10Y_CACHE.get("fetched")
        if (cached is not None and fetched is not None and fetched[0] is cached
                and datetime.now() - fetched[1] < _JP10Y_REFETCH_TTL):
            return cached
        try:
            df = _download_jp10y(cached)
            _JP10Y_CACHE["df"] = df
            _JP10Y_CACHE["fetched"] = (df, datetime.now())
            if df is not cached:
                _save_jp10y_disk_cache(df)
            return df
        except requests.RequestException as e:
            # Network-level failures (timeouts, refused connections, HTTP errors) are
            # transient; one line is enough and the next refresh retries.
            logger.warning("JP10Y fetch failed, keeping cached series: %s", e)
        except (KeyError, ValueError):
            # A changed CSV layout, which needs the traceback: pandas CSV errors (ParserError,
            # EmptyDataError, UnicodeDecodeError) are ValueErrors, a missing column a KeyError.
            logger.error("JP10Y parse failed", exc_info=True)
        return cached
