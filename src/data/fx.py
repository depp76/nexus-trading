"""data/fx.py — USD/KRW rate (Yahoo daily chart) with a session cache."""
from datetime import datetime, timedelta
import logging
import time
import pandas as pd
import polars as pl
import requests

from data.cache import _USD_KRW_CACHE, _USD_KRW_LOCK, _hist_df_is_stale
from data.frames import _to_polars

logger = logging.getLogger(__name__)

# The same request fdr.DataReader('USD/KRW') sends (FinanceDataReader's YahooDailyReader:
# symbol KRW=X, daily bars from 1980-01-01), made here so it can carry a timeout — fdr's
# requests.get has none, so an unresponsive host blocked the caller for the OS connect timeout.
_USD_KRW_URL = "https://query2.finance.yahoo.com/v8/finance/chart/KRW=X"
_USD_KRW_HEADERS = {"user-agent": "Mozilla/5.0 AppleWebKit/537.36"}
_USD_KRW_START = datetime(1980, 1, 1)
_USD_KRW_TIMEOUT = (10, 15)  # (connect, read) seconds; connect covers three SYN retransmits
_USD_KRW_FALLBACK = 1450.0
# After a failed fetch with no series cached yet (a cold start while offline), how long
# the fallback rate is served before the next call tries the network again.
_USD_KRW_RETRY_AFTER = timedelta(seconds=60)


def _fetch_usd_krw_history() -> pd.DataFrame:
    """Daily USD/KRW OHLC + Adj Close indexed by date, shaped like fdr's frame."""
    params = {
        "period1": int(time.mktime(_USD_KRW_START.timetuple())),
        "period2": int(time.mktime(datetime.today().timetuple())),
        "interval": "1d",
        "includeAdjustedClose": "true",
    }
    res = requests.get(_USD_KRW_URL, params=params, headers=_USD_KRW_HEADERS, timeout=_USD_KRW_TIMEOUT)
    res.raise_for_status()
    result = res.json()["chart"]["result"][0]
    index = pd.to_datetime(result["timestamp"], unit="s").normalize()
    values = {**result["indicators"]["quote"][0], **result["indicators"]["adjclose"][0]}
    col_map = {"open": "Open", "high": "High", "low": "Low", "close": "Close",
               "volume": "Volume", "adjclose": "Adj Close"}
    df = pd.DataFrame(data=values, index=index).rename(columns=col_map)
    return df[list(col_map.values())]


def get_usd_krw_rate():
    """Returns USD/KRW FX rate."""
    usd_cache = _USD_KRW_CACHE
    with _USD_KRW_LOCK:
        if usd_cache["rate"] is not None and not _usd_krw_refetch_due(usd_cache):
            return usd_cache["rate"]
        fallback = usd_cache["rate"] if usd_cache["rate"] is not None else _USD_KRW_FALLBACK
        rate = None
        try:
            df = _to_polars(_fetch_usd_krw_history())
            close_s = df.get_column("Close").drop_nulls() if not df.is_empty() else None
            if close_s is not None and len(close_s) > 0:
                usd_cache["df"] = df
                usd_cache["fetched_at"] = datetime.now()
                usd_cache["failed_at"] = None
                rate = float(close_s[-1])
        except requests.RequestException as e:
            # Network-level failures are transient; one line, the next call retries.
            logger.warning("USD/KRW rate fetch failed, using cached/fallback rate=%.1f: %s", fallback, e)
        except Exception:
            # boundary: callers rely on always getting a rate; this also covers a changed reply layout (Key/Index/Type/ValueError) and the pandas->polars conversion.
            # Anything else means the response layout changed, which needs the traceback.
            logger.error("USD/KRW rate parse failed, using cached/fallback rate=%.1f", fallback, exc_info=True)
        if rate is None:
            # Remember the failure so a cold-start fallback (no series cached) is retried
            # after _USD_KRW_RETRY_AFTER instead of being served until the process ends
            # (review_agy.md C1).
            rate = fallback
            usd_cache["failed_at"] = datetime.now()
        usd_cache["rate"] = rate
        return rate


def _usd_krw_refetch_due(usd_cache: dict) -> bool:
    """True when get_usd_krw_rate must try the network although a rate is cached:
    the cached series is stale, or there is no series at all because the last
    fetch failed and _USD_KRW_RETRY_AFTER has passed since (review_agy.md C1).
    A rate without a series and without a failure stamp was seeded by a caller
    (tests) and counts as fresh."""
    df = usd_cache.get("df")
    if df is None:
        failed_at = usd_cache.get("failed_at")
        return failed_at is not None and datetime.now() - failed_at >= _USD_KRW_RETRY_AFTER
    return _hist_df_is_stale(df, usd_cache.get("fetched_at"))


def get_usd_krw_history() -> pl.DataFrame:
    """Daily USD/KRW (Date, Close) from the same cached Yahoo series the spot
    rate reads; empty when it cannot be fetched. The Trend Following research
    converts US prices to KRW with it (spec 9-25)."""
    get_usd_krw_rate()
    df = _USD_KRW_CACHE.get("df")
    if df is None or df.is_empty() or "Close" not in df.columns or "Date" not in df.columns:
        return pl.DataFrame()
    return df.select(["Date", "Close"])


def get_usd_krw_rate_for_date(date_str: str) -> float:
    """Returns USD/KRW rate for a specific date (YYYY-MM-DD)."""
    get_usd_krw_rate()
    df = _USD_KRW_CACHE.get("df")
    if df is not None and not df.is_empty():
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            sub = df.filter(pl.col("Date") <= target)
            closes = sub.get_column("Close").drop_nulls()
            if len(closes) > 0:
                return float(closes[-1])
        except (ValueError, pl.exceptions.PolarsError):
            logger.debug("get_usd_krw_rate_for_date failed for date=%s", date_str, exc_info=True)
    return get_usd_krw_rate()
