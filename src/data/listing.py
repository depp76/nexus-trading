"""data/listing.py — Stock listing lookups (FinanceDataReader) with a day-scoped,
single-flight cache. Sits below market.py so collectors can use listings without
importing the aggregation layer."""
import io
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
import logging
import pandas as pd
import requests
import FinanceDataReader as fdr

from data.cache import safe_float

logger = logging.getLogger(__name__)

# How long a provisional listing (see _listing_is_provisional) stays cached.
_PROVISIONAL_TTL = 30 * 60  # seconds

# The daily KRX listing files fdr.StockListing reads, rewritten up to three times a
# day. Files written before the session opens, and those of non-trading days, have
# Marcap empty for every stock. The URL mirrors FDR's internal KrxMarcapListingCache.
_MARCAP_FILE_URL = (
    "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/"
    "refs/heads/master/data/listing/krx/{date}.csv"
)
_MARCAP_LOOKBACK_DAYS = 10  # spans the longest market closures (Chuseok, Lunar New Year)
_MARCAP_TIMEOUT = 10  # seconds


def _listing_is_provisional(df) -> bool:
    """True for a listing a later fetch may improve on the same day: a failed
    fetch (empty frame), or a Marcap column with no values yet."""
    if df.empty:
        return True
    return 'Marcap' in df.columns and df['Marcap'].notna().sum() == 0


def _singleflight_cache(maxsize, provisional=None):
    """Like functools.lru_cache(maxsize), but de-duplicates concurrent calls:
    if a second thread requests a key that's still being computed by another
    thread, it waits for and reuses that in-flight result instead of firing
    its own redundant (here, network-bound) computation. Plain lru_cache only
    guarantees the cache structure itself isn't corrupted by concurrent
    access -- it does not prevent two threads from both missing on the same
    key and both calling the wrapped function.

    Entries expire at the day boundary: listings (constituents, market caps)
    change daily, and before this the cache lived for the whole process
    lifetime (roadmap 6-2f). A result for which `provisional(result)` is true
    expires after _PROVISIONAL_TTL instead.

    Only meant for single-argument functions keyed on that argument, which
    is all get_stock_listing/_get_listing_with_norm need."""
    def decorator(fn):
        cache: "OrderedDict[str, tuple]" = OrderedDict()  # key -> (date, result, expires_at)
        cache_lock = threading.Lock()
        key_locks: dict = {}

        def _lookup(key, today):
            hit = cache.get(key)
            if hit is not None and hit[0] == today and (hit[2] is None or time.monotonic() < hit[2]):
                cache.move_to_end(key)
                return hit[1]
            return None

        def wrapper(key):
            today = datetime.now().date()
            with cache_lock:
                cached = _lookup(key, today)
                if cached is not None:
                    return cached
                key_lock = key_locks.setdefault(key, threading.Lock())

            with key_lock:
                with cache_lock:
                    cached = _lookup(key, today)
                    if cached is not None:
                        return cached
                result = fn(key)
                with cache_lock:
                    expires_at = None
                    if provisional is not None and provisional(result):
                        expires_at = time.monotonic() + _PROVISIONAL_TTL
                    cache[key] = (today, result, expires_at)
                    cache.move_to_end(key)
                    if len(cache) > maxsize:
                        cache.popitem(last=False)
                    key_locks.pop(key, None)
                return result

        def cache_clear():
            with cache_lock:
                cache.clear()
                key_locks.clear()

        wrapper.cache_clear = cache_clear
        return wrapper
    return decorator


@_singleflight_cache(maxsize=16, provisional=_listing_is_provisional)
def get_stock_listing(market: str) -> pd.DataFrame:
    """Cached version of fdr.StockListing to prevent redundant network requests.

    FDR's live KrxStockListing is deliberately not used: it POSTs to the http://
    KRX URL, whose 301 to https turns the POST into a GET that returns HTML, so
    it always fails with JSONDecodeError (FDR 0.9.202)."""
    try:
        return fdr.StockListing(market)
    except Exception:
        logger.error("fdr.StockListing('%s') failed", market, exc_info=True)
        return pd.DataFrame(columns=['Symbol', 'Code', 'Name', 'Market'])


@_singleflight_cache(maxsize=4, provisional=_listing_is_provisional)
def _get_listing_with_norm(market: str) -> pd.DataFrame:
    """Cached listing with pre-computed NameNorm column (upper, stripped)."""
    df = get_stock_listing(market).copy()
    df['NameNorm'] = df['Name'].str.upper().str.replace(r'[\s_]+', '', regex=True)
    return df


def _fetch_recent_marcaps(today=None) -> tuple:
    """Market caps from the newest earlier daily file that has them, as
    (file_date, {code: marcap}); (None, {}) if none is found or the download fails."""
    today = today or datetime.now().date()
    for days_back in range(1, _MARCAP_LOOKBACK_DAYS + 1):
        day = (today - timedelta(days=days_back)).isoformat()
        try:
            r = requests.get(_MARCAP_FILE_URL.format(date=day), timeout=_MARCAP_TIMEOUT)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text), usecols=['Code', 'Marcap'], dtype={'Code': str})
        except Exception:
            logger.warning("Could not read the market-cap file of %s", day, exc_info=True)
            return None, {}
        df = df.dropna(subset=['Marcap'])
        if not df.empty:
            return day, {str(c).zfill(6): safe_float(m) for c, m in zip(df['Code'], df['Marcap'])}
    return None, {}


def _fetch_kr_listing_fdr_fallback(market, top_n):
    """Fallback KR market-listing source, ranked by market cap. When today's listing
    has no market caps, the newest earlier ones rank it instead; returns [] if there
    are none at all, because an unranked listing is not a top-N universe."""
    try:
        df = get_stock_listing(market)
        code_col = 'Code' if 'Code' in df.columns else ('Symbol' if 'Symbol' in df.columns else None)
        marcap_col = next((c for c in ('Marcap', 'MarCap', 'MarketCap') if c in df.columns), None)
        if df is None or df.empty or not code_col or 'Name' not in df.columns or not marcap_col:
            # KRX-DESC has no market caps but still supplies the codes and names
            df_desc = get_stock_listing('KRX-DESC')
            if df_desc is not None and not df_desc.empty and 'Market' in df_desc.columns:
                df = df_desc[df_desc['Market'].str.upper() == market.upper()]
                code_col = 'Code' if 'Code' in df.columns else ('Symbol' if 'Symbol' in df.columns else None)
                marcap_col = next((c for c in ('Marcap', 'MarCap', 'MarketCap') if c in df.columns), None)

        if df is None or df.empty or not code_col or 'Name' not in df.columns:
            return []

        rows = [
            {
                'Code': str(row[code_col]).zfill(6),
                'Name': str(row['Name']),
                'Marcap': safe_float(row[marcap_col]) if marcap_col else 0.0,
            }
            for _, row in df.iterrows()
        ]
        if not any(r['Marcap'] > 0 for r in rows):
            file_date, marcaps = _fetch_recent_marcaps()
            for r in rows:
                r['Marcap'] = marcaps.get(r['Code'], 0.0)
            if not any(r['Marcap'] > 0 for r in rows):
                logger.error("FDR listing fallback has no market caps for market=%s, cannot rank it", market)
                return []
            logger.warning("FDR listing for market=%s has no market caps, ranking by those of %s", market, file_date)
        rows.sort(key=lambda x: -x['Marcap'])
        return rows[:top_n]
    except Exception:
        logger.warning("FDR listing fallback failed for market=%s", market, exc_info=True)
        return []
