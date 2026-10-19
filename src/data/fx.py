"""data/fx.py — USD/KRW rate (Yahoo daily chart) with a session cache."""
from datetime import datetime
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
        if usd_cache["rate"] is not None and not _hist_df_is_stale(usd_cache["df"], usd_cache.get("fetched_at")):
            return usd_cache["rate"]
        fallback = usd_cache["rate"] if usd_cache["rate"] is not None else _USD_KRW_FALLBACK
        try:
            df = _to_polars(_fetch_usd_krw_history())
            if not df.is_empty():
                usd_cache["df"] = df
                usd_cache["fetched_at"] = datetime.now()
                close_s = df.get_column("Close").drop_nulls()
                rate = float(close_s[-1]) if len(close_s) > 0 else _USD_KRW_FALLBACK
            else:
                rate = fallback
        except requests.RequestException as e:
            # Network-level failures are transient; one line, the next call retries.
            rate = fallback
            logger.warning("USD/KRW rate fetch failed, using cached/fallback rate=%.1f: %s", rate, e)
        except Exception:
            # Anything else means the response layout changed, which needs the traceback.
            rate = fallback
            logger.error("USD/KRW rate parse failed, using cached/fallback rate=%.1f", rate, exc_info=True)
        usd_cache["rate"] = rate
        return rate


def get_usd_krw_rate_for_date(date_str: str) -> float:
    """Returns USD/KRW rate for a specific date (YYYY-MM-DD)."""
    get_usd_krw_rate()
    df = _USD_KRW_CACHE.get("df")
    if df is not None and not df.is_empty():
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            sub = df.filter(pl.col("Date") <= target)
            if not sub.is_empty():
                return float(sub.get_column("Close")[-1])
        except Exception:
            logger.debug("get_usd_krw_rate_for_date failed for date=%s", date_str, exc_info=True)
    return get_usd_krw_rate()
