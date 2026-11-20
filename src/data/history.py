"""data/history.py — Daily OHLCV history with the shared LRU cache (_HIST_CACHE).

Lowest fetch layer above the collectors: routes KR codes/indices to Naver, bond
yields to their cached pandas series, and everything else to yfinance ->
yahooquery -> FinanceDataReader. Does not import data.market or
data.collectors.yahoo, so indicators/yahoo can import it without a cycle."""
import logging
from datetime import datetime
import pandas as pd
import polars as pl
from data.cache import fdr   # lazy: imported on first use (data.cache.LazyModule)

import data.cache as _dc
from data.cache import (
    KeyedLocks,
    _FDR_ONLY_TICKERS,
    _hist_df_is_stale,
    _record_hist_cache_lookup,
    is_kr_code,
    yf_bulk_get,
)
from data.frames import _to_polars
from data.collectors.naver import _fast_kr_history, _get_kr3y_df

logger = logging.getLogger(__name__)

# Per-(ticker, start) locks for the single-flight path in get_historical_data,
# keyed the same as _HIST_CACHE.
_HIST_KEY_LOCKS = KeyedLocks()


def _cached_entry(cache_key):
    """(cached df or None, fresh) for one _HIST_CACHE key; a hit is moved to the
    LRU end. A stale df is still returned so a failed refresh can fall back to it."""
    with _dc._HIST_CACHE_LOCK:
        cached = _dc._HIST_CACHE.get(cache_key)
        fetched_at = _dc._HIST_CACHE_FETCHED_AT.get(cache_key)
        if cached is not None:
            _dc._HIST_CACHE.move_to_end(cache_key)
    if cached is None:
        return None, False
    return cached, not _hist_df_is_stale(cached, fetched_at)


def get_historical_data(ticker: str, start: str) -> pl.DataFrame:
    """Historical data with a smart cache that skips empty DataFrames.

    _HIST_CACHE is a plain OrderedDict shared across every fetch thread
    (up to 20 concurrent workers during a full-universe refresh), so all
    reads/writes to it are serialized under _HIST_CACHE_LOCK.

    Single-flight: on a miss, the calling thread claims a per-key lock
    (_HIST_KEY_LOCKS) before fetching, so if 20 threads miss on the same
    ticker at once (a full-universe refresh), only the first actually hits
    the network -- the rest block on the lock and then re-check the cache,
    which the first thread has by then populated (review_agy.md #4; before
    this every one of them fired its own redundant Naver/yfinance request).
    The network fetch itself still happens outside _HIST_CACHE_LOCK, so
    concurrent misses on *different* keys still run in parallel.

    A stale entry whose refresh comes back empty is returned as it is
    rather than an empty frame (review_agy.md H5).

    The cache dict/lock/max are read through the `data.cache` module object
    (`_dc`) rather than imported names so tests can rebind e.g.
    `data.cache._HIST_CACHE_MAX` and have it take effect here. Tests patch
    `data.history._fetch_historical_uncached` / `_hist_df_is_stale`.
    """
    cache_key = (ticker, start)

    cached, fresh = _cached_entry(cache_key)
    if fresh:
        _record_hist_cache_lookup(hit=True)
        return cached

    with _HIST_KEY_LOCKS.hold(cache_key):
        cached, fresh = _cached_entry(cache_key)
        if fresh:
            _record_hist_cache_lookup(hit=True)
            return cached

        _record_hist_cache_lookup(hit=False)

        df = _fetch_historical_uncached(ticker, start)
        if df.is_empty():
            return cached if cached is not None else df
        max_size = _dc._HIST_CACHE_MAX
        with _dc._HIST_CACHE_LOCK:
            if cache_key not in _dc._HIST_CACHE and len(_dc._HIST_CACHE) >= max_size:
                try:
                    evicted_key, _ = _dc._HIST_CACHE.popitem(last=False)
                    _dc._HIST_CACHE_FETCHED_AT.pop(evicted_key, None)
                except KeyError:
                    logger.debug("LRU cache eviction failed", exc_info=True)
            _dc._HIST_CACHE[cache_key] = df
            _dc._HIST_CACHE.move_to_end(cache_key)
            _dc._HIST_CACHE_FETCHED_AT[cache_key] = datetime.now()
        return df


def _fetch_historical_uncached(ticker: str, start: str) -> pl.DataFrame:
    """Actual fetch — called only on cache miss."""
    try:
        # Fast path for Korean indices via Naver (includes intraday live data)
        if ticker in ("^KS11", "KS11", "KOSPI"):
            df = _fast_kr_history("KOSPI", start)
            if not df.is_empty():
                return df
        elif ticker in ("^KQ11", "KQ11", "KOSDAQ"):
            df = _fast_kr_history("KOSDAQ", start)
            if not df.is_empty():
                return df

        if is_kr_code(ticker):
            df = _fast_kr_history(ticker, start)
            if not df.is_empty():
                return df

        if ticker in _FDR_ONLY_TICKERS:
            if ticker == "KR3YT":
                df_pd = _get_kr3y_df()
                if df_pd is not None and not df_pd.empty:
                    if start:
                        df_pd = df_pd[df_pd.index >= start]
                    return _to_polars(df_pd)
                return pl.DataFrame()

            df_pd = fdr.DataReader(ticker, start)
            return _to_polars(df_pd)

        staged = yf_bulk_get(f"{ticker}_{start}")
        if staged is not None:
            return staged

        import yfinance as yf
        yf_ticker = ticker.replace(".", "-")
        df_pd = None
        try:
            _yf_df = yf.Ticker(yf_ticker).history(start=start, timeout=10, auto_adjust=True)
            if _yf_df is not None and not _yf_df.empty:
                if isinstance(_yf_df.columns, pd.MultiIndex):
                    _yf_df.columns = _yf_df.columns.get_level_values(0)
                df_pd = _yf_df
        except Exception:
            # kept broad: yfinance raises arbitrary third-party exception types.
            logger.debug("yfinance history fetch failed for %s", ticker, exc_info=True)

        if df_pd is None or (hasattr(df_pd, 'empty') and df_pd.empty):
            try:
                from yahooquery import Ticker as YQTicker
                _yq = YQTicker(ticker, asynchronous=False)
                _yq_df = _yq.history(start=start)
                if isinstance(_yq_df, pd.DataFrame) and not _yq_df.empty:
                    _yq_df = _yq_df.reset_index()
                    if 'date' in _yq_df.columns:
                        _yq_df = _yq_df.rename(columns={'date': 'Date', 'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'volume': 'Volume'})
                    df_pd = _yq_df
            except Exception:
                # kept broad: yahooquery raises arbitrary third-party exception types.
                logger.debug("yahooquery history fetch failed for %s", ticker, exc_info=True)

        if df_pd is None or (hasattr(df_pd, 'empty') and df_pd.empty):
            df_pd = fdr.DataReader(ticker, start)

        return _to_polars(df_pd)
    except Exception:
        # boundary: contract is an empty frame on any failure; the FDR fallback above raises arbitrary types.
        logger.warning("All history sources failed for ticker=%s, returning empty DataFrame", ticker, exc_info=True)
        return pl.DataFrame()
