"""data/collectors/yahoo.py — Yahoo Finance batch quotes, US market data, and futures curves."""
import time
import re
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import logging

from data.cache import (
    PAYLOAD_ERRORS,
    _YF_BULK_CACHE,
    _YF_SESSION,
    _NAVER_SESSION,
    _get_yf_crumb,
    start_date,
    safe_float,
)
from data.frames import _to_polars
from data.indicators import fetch_historical_changes
from data.history import get_historical_data
from data.listing import get_stock_listing
from data.fx import get_usd_krw_rate

logger = logging.getLogger(__name__)



def yf_quote_batch(symbols: list, timeout: int = 15, chunk_size: int = 50, max_retries: int = 3) -> dict:
    """Batch-fetch raw Yahoo Finance v7 quote results for a list of symbols.

    Handles chunking, crumb retrieval/refresh-on-401, and retry-with-backoff
    in one place, shared by every caller that needs Yahoo quote data.
    """
    if not symbols:
        return {}

    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]

    def _fetch_chunk(chunk):
        crumb = _get_yf_crumb()
        params = {"symbols": ",".join(chunk)}
        if crumb:
            params["crumb"] = crumb

        resp = None
        for attempt in range(max_retries):
            try:
                resp = _YF_SESSION.get(
                    "https://query2.finance.yahoo.com/v7/finance/quote",
                    params=params,
                    timeout=timeout,
                )
                # 401 -> crumb expired: refresh and retry immediately
                if resp.status_code == 401:
                    crumb = _get_yf_crumb(force_refresh=True)
                    params["crumb"] = crumb
                    resp = _YF_SESSION.get(
                        "https://query2.finance.yahoo.com/v7/finance/quote",
                        params=params,
                        timeout=timeout,
                    )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise e
                time.sleep(1)

        chunk_results = {}
        if resp:
            for item in resp.json().get("quoteResponse", {}).get("result") or []:
                sym = item.get("symbol", "")
                if sym:
                    chunk_results[sym] = item
        return chunk_results

    results: dict = {}
    if len(chunks) == 1:
        try:
            results.update(_fetch_chunk(chunks[0]))
        except PAYLOAD_ERRORS:
            logger.warning("YF v7 quote batch error", exc_info=True)
    else:
        with ThreadPoolExecutor(max_workers=min(len(chunks), 4)) as exe:
            futures = {exe.submit(_fetch_chunk, c): c for c in chunks}
            for fut in as_completed(futures):
                try:
                    results.update(fut.result())
                except PAYLOAD_ERRORS:
                    logger.warning("YF v7 quote batch error", exc_info=True)

    return results


def fetch_us_realtime_prices(tickers: list) -> dict:
    """Batch-fetch real-time prices for US stocks using yf_quote_batch."""
    if not tickers:
        return {}

    yf_symbols = [str(t).replace(".", "-") for t in tickers]
    orig_map = {s: t for s, t in zip(yf_symbols, tickers)}

    quotes = yf_quote_batch(yf_symbols, timeout=15)
    results = {}
    for sym, item in quotes.items():
        price = item.get("regularMarketPrice")
        if price is not None:
            results[orig_map.get(sym, sym)] = float(price)

    return results


def fetch_wti_futures_curve():
    """Fetches the latest prices for the next 8 WTI futures contracts."""
    from dateutil.relativedelta import relativedelta
    month_codes = {1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M', 7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'}
    now = datetime.now()

    symbols_to_try = []
    # Test up to next 12 months (some might be delisted or inactive)
    for i in range(12):
        dt = now + relativedelta(months=i)
        code = month_codes[dt.month]
        # YF usually uses 2-digit years for NYM futures
        yr = dt.strftime("%y")
        sym = f"CL{code}{yr}.NYM"
        symbols_to_try.append((sym, dt.strftime("%Y-%m")))

    import yfinance as yf
    td = yf.download(
        [s[0] for s in symbols_to_try],
        period="1d",
        group_by="ticker",
        progress=False,
        threads=True
    )

    results = []
    if td is not None and not td.empty:
        for sym, month_label in symbols_to_try:
            try:
                if len(symbols_to_try) == 1:
                    close_price = td['Close'].iloc[-1]
                else:
                    if sym in td.columns.levels[0]:
                        close_price = td[sym]['Close'].iloc[-1]
                    else:
                        continue

                if not np.isnan(close_price):
                    results.append({"Contract": month_label, "Symbol": sym, "Close": float(close_price)})
                    if len(results) == 8:
                        break
            except (KeyError, IndexError, TypeError, ValueError):
                logger.debug("WTI contract parse error for sym=%s", sym, exc_info=True)
    return results


_US_BULK_CHUNK_SIZE = 20
_YQ_META_TIMEOUT = 35  # seconds to wait for each yahooquery metadata call


@dataclass
class _UsBulkRun:
    """Shared state of one fetch_us_stock_data_bulk call (read by every chunk worker)."""
    market_name: str
    fx_rate: float
    total: int
    progress_callback: object
    naver_mcaps: dict
    lock: object
    yf_semaphore: object
    yf_bulk_keys_added: set = field(default_factory=set)
    done_count: int = 0

    def tick(self) -> None:
        """Count one finished symbol and report progress (called from each symbol worker)."""
        with self.lock:
            self.done_count += 1
            _c = self.done_count
        if self.progress_callback:
            self.progress_callback(min(_c, self.total), self.total)


@dataclass
class _UsChunkMeta:
    """Per-chunk metadata keyed by Yahoo symbol: market cap, display name, trailing/forward PER."""
    market_cap: dict = field(default_factory=dict)
    name: dict = field(default_factory=dict)
    trailing_per: dict = field(default_factory=dict)
    forward_per: dict = field(default_factory=dict)


def _is_valid_pe(v) -> bool:
    """True for a real (non-NaN) int/float PER value."""
    return v is not None and isinstance(v, (int, float)) and v == v


def _prefetch_naver_us_market_caps(market_name: str, total: int) -> dict:
    """Naver market caps (USD) by bare symbol, fetched IN PARALLEL across pages; partial on failure."""
    naver_mcaps = {}
    try:
        nm = "NASDAQ" if "NASDAQ" in market_name.upper() else ("NYSE" if "NYSE" in market_name.upper() else "AMEX")
        num_pages = max(3, (total // 100) + 2)

        def _fetch_naver_mcap_page(p):
            r = _NAVER_SESSION.get(
                f"https://api.stock.naver.com/stock/exchange/{nm}/marketValue?page={p}&pageSize=100",
                timeout=5
            )
            d = r.json()
            page_data = {}
            if 'stocks' in d and d['stocks']:
                for s in d['stocks']:
                    page_data[s.get('reutersCode', '').split('.')[0]] = float(s.get('marketValueRaw') or 0)
            return page_data

        with ThreadPoolExecutor(max_workers=min(num_pages, 8)) as exe:
            for page_result in exe.map(_fetch_naver_mcap_page, range(1, num_pages + 1)):
                naver_mcaps.update(page_result)
    except PAYLOAD_ERRORS:
        logger.warning("Naver market cap prefetch failed, market caps may be incomplete", exc_info=True)
    return naver_mcaps


def _store_bulk_history(orig_sym: str, single_df, run: _UsBulkRun) -> None:
    """Put one symbol's bulk-downloaded history into _YF_BULK_CACHE (skipped when empty)."""
    if not single_df.empty:
        pl_df = _to_polars(single_df)
        key = f"{orig_sym}_{start_date()}"
        _YF_BULK_CACHE[key] = pl_df
        run.yf_bulk_keys_added.add(key)


def _prefetch_chunk_history(yf_symbols: list, run: _UsBulkRun) -> None:
    """Step 0: bulk pre-fetch one chunk's history via yfinance into _YF_BULK_CACHE."""
    try:
        import yfinance as yf
        with run.yf_semaphore:
            bulk_df = yf.download(
                yf_symbols,
                start=start_date(),
                progress=False,
                timeout=15,
                auto_adjust=True,
                threads=False
            )
        if bulk_df is not None and not bulk_df.empty:
            for sym in yf_symbols:
                try:
                    orig_sym = sym.replace("-", ".")
                    if isinstance(bulk_df.columns, pd.MultiIndex):
                        if sym in bulk_df.columns.get_level_values(1):
                            _store_bulk_history(orig_sym, bulk_df.xs(sym, level=1, axis=1).dropna(how='all'), run)
                    else:
                        if len(yf_symbols) == 1 and sym == yf_symbols[0]:
                            _store_bulk_history(orig_sym, bulk_df.dropna(how='all'), run)
                except Exception:
                    # boundary: one symbol's slice must not drop the chunk; _to_polars on a yfinance frame can raise polars/pyarrow types.
                    logger.debug("YF bulk history slice failed for sym=%s, skipping", sym, exc_info=True)
    except Exception:
        # kept broad: yf.download raises arbitrary third-party exception types.
        logger.warning("YF bulk history fetch error (chunk)", exc_info=True)


def _apply_v7_quotes(yf_symbols: list, meta: _UsChunkMeta) -> None:
    """Primary metadata source: the Yahoo Finance v7 quote API."""
    try:
        quotes = yf_quote_batch(yf_symbols, timeout=25, chunk_size=len(yf_symbols) or 1)
        for sym, item in quotes.items():
            mc = item.get("marketCap") or item.get("totalAssets") or 0
            meta.market_cap[sym] = float(mc)
            n = item.get("longName") or item.get("shortName")
            if n:
                meta.name[sym] = n
            tpe = item.get("trailingPE")
            fpe = item.get("forwardPE")
            if _is_valid_pe(tpe):
                meta.trailing_per[sym] = round(float(tpe), 1)
            if _is_valid_pe(fpe):
                meta.forward_per[sym] = round(float(fpe), 1)
    except PAYLOAD_ERRORS:
        logger.warning("YF v7 quote fetch error (chunk)", exc_info=True)


def _fetch_yq_meta(missing: list):
    """yahooquery (summary_detail, quote_type) for `missing`, fetched in parallel; {} for a failed call."""
    from yahooquery import Ticker as YQTicker   # at first use: off the app's start-up path
    yq = YQTicker(missing, asynchronous=False, timeout=30)

    def _get_summary(): return yq.summary_detail
    def _get_qt():      return yq.quote_type

    with ThreadPoolExecutor(max_workers=2) as yq_exe:
        f_summary = yq_exe.submit(_get_summary)
        f_qt      = yq_exe.submit(_get_qt)
        try:
            details = f_summary.result(timeout=_YQ_META_TIMEOUT)
        except Exception:
            # kept broad: yahooquery raises arbitrary types (and result() raises TimeoutError); {} means "no fallback data".
            details = {}
        try:
            qt = f_qt.result(timeout=_YQ_META_TIMEOUT)
        except Exception:
            # kept broad: as above.
            qt = {}
    return details, qt


def _apply_yq_fallback(yf_symbols: list, meta: _UsChunkMeta) -> None:
    """Fallback: yahooquery for any symbols the v7 quote left without a market cap and a name."""
    missing = [s for s in yf_symbols if s not in meta.market_cap and s not in meta.name]
    if not missing:
        return
    try:
        details, qt = _fetch_yq_meta(missing)

        if isinstance(details, dict):
            for sym, data in details.items():
                if isinstance(data, dict):
                    mc = data.get('marketCap') or data.get('totalAssets', 0)
                    if sym not in meta.market_cap:
                        meta.market_cap[sym] = mc or 0
                    tpe = data.get('trailingPE')
                    fpe = data.get('forwardPE')
                    if _is_valid_pe(tpe):
                        meta.trailing_per.setdefault(sym, round(float(tpe), 1))
                    if _is_valid_pe(fpe):
                        meta.forward_per.setdefault(sym, round(float(fpe), 1))
        if isinstance(qt, dict):
            for sym, data in qt.items():
                if isinstance(data, dict):
                    n = data.get('longName') or data.get('shortName')
                    if n and sym not in meta.name:
                        meta.name[sym] = n
    except Exception:
        # kept broad: yahooquery raises arbitrary third-party exception types.
        logger.warning("yahooquery fallback error (chunk)", exc_info=True)


def _resolve_us_market_cap(symbol: str, yf_symbol: str, meta: _UsChunkMeta, run: _UsBulkRun) -> float:
    """USD market cap: chunk quote metadata, then the Naver prefetch, then yfinance fast_info."""
    usd_marcap = meta.market_cap.get(yf_symbol, 0)
    if usd_marcap == 0:
        usd_marcap = run.naver_mcaps.get(symbol.split('.')[0], 0)
    if usd_marcap == 0:
        try:
            import yfinance as yf
            fi = yf.Ticker(yf_symbol, session=_YF_SESSION).fast_info
            usd_marcap = float(getattr(fi, 'market_cap', 0) or 0)
        except Exception:
            # kept broad: yfinance raises arbitrary third-party exception types.
            logger.debug("yfinance fast_info failed for %s", yf_symbol, exc_info=True)
    return usd_marcap


def _process_us_symbol(symbol: str, name: str, meta: _UsChunkMeta, run: _UsBulkRun):
    """One symbol's result row (prices in KRW via run.fx_rate), or None when it has no price."""
    yf_symbol = symbol.replace(".", "-")
    try:
        df_history = get_historical_data(symbol, start_date())
        usd_price = 0.0
        if not df_history.is_empty() and "Close" in df_history.columns:
            close_valid = df_history.get_column('Close').drop_nulls()
            if len(close_valid) > 0:
                usd_price = safe_float(close_valid[-1])

        if usd_price == 0:
            return None

        usd_marcap = _resolve_us_market_cap(symbol, yf_symbol, meta, run)

        display_name = meta.name.get(yf_symbol) or name or symbol
        changes = fetch_historical_changes(symbol, usd_price, df_history)
        return {
            "ticker": symbol,
            "name": display_name,
            "market": run.market_name,
            "price": usd_price * run.fx_rate,
            "market_cap": usd_marcap * run.fx_rate,
            "usd_market_cap": usd_marcap,
            "usd_price": usd_price,
            "currency": "$",
            "changes": changes,
            "trailing_per": meta.trailing_per.get(yf_symbol),
            "forward_per": meta.forward_per.get(yf_symbol),
        }
    except Exception:
        # boundary: one bad symbol must not sink the chunk; logged with traceback.
        logger.error("US stock data fetch failed for symbol=%s", symbol, exc_info=True)
        return None
    finally:
        run.tick()


def _process_us_chunk(chunk: list, run: _UsBulkRun) -> list:
    """One chunk of (symbol, name): history prefetch, metadata, then per-symbol rows in parallel."""
    yf_symbols = [s.replace(".", "-") for s, _ in chunk]
    meta = _UsChunkMeta()

    _prefetch_chunk_history(yf_symbols, run)
    _apply_v7_quotes(yf_symbols, meta)
    _apply_yq_fallback(yf_symbols, meta)

    chunk_results = []
    with ThreadPoolExecutor(max_workers=5) as exe:
        futures = [exe.submit(_process_us_symbol, s, n, meta, run) for s, n in chunk]
        for f in as_completed(futures):
            res = f.result()
            if res:
                chunk_results.append(res)

    return chunk_results


def fetch_us_stock_data_bulk(symbols_with_names, market_name, fx_rate, progress_callback=None):
    """Fetches US stock data using yahooquery (batched history + meta, per-stock parallel processing)."""
    total = len(symbols_with_names)
    chunk_size = _US_BULK_CHUNK_SIZE
    chunks = [symbols_with_names[i:i + chunk_size] for i in range(0, total, chunk_size)]
    num_chunks = len(chunks)

    naver_mcaps = _prefetch_naver_us_market_caps(market_name, total)

    run = _UsBulkRun(
        market_name=market_name,
        fx_rate=fx_rate,
        total=total,
        progress_callback=progress_callback,
        naver_mcaps=naver_mcaps,
        lock=threading.Lock(),
        yf_semaphore=threading.Semaphore(3),
    )

    results = []
    with ThreadPoolExecutor(max_workers=min(num_chunks, 6)) as executor:
        for rs in executor.map(lambda c: _process_us_chunk(c, run), chunks):
            results.extend(rs)

    # Remove only the entries this call added, so an overlapping concurrent
    # call to fetch_us_stock_data_bulk (e.g. NASDAQ 100 + S&P 500 refreshes
    # overlapping) doesn't wipe the other call's still-in-use bulk cache entries.
    for _k in run.yf_bulk_keys_added:
        _YF_BULK_CACHE.pop(_k, None)
    return results


def fetch_us_market_data(market="NASDAQ 100", top_n=200, progress_callback=None):
    try:
        df_list = pd.DataFrame()
        if market == "NASDAQ 100":
            try:
                import io
                res = requests.get('https://en.wikipedia.org/wiki/Nasdaq-100', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                tables = pd.read_html(io.StringIO(res.text), flavor='lxml')
                for tbl in tables:
                    if 'Ticker' in tbl.columns:
                        df_list = tbl.rename(columns={'Ticker': 'Symbol', 'Company': 'Name'})
                        break
                    elif 'Symbol' in tbl.columns:
                        df_list = tbl.rename(columns={'Company': 'Name'})
                        break
            except Exception:
                # kept broad: pd.read_html/lxml raise assorted types (ValueError, ImportError, parser errors).
                logger.debug("Wikipedia NASDAQ-100 table fetch error, falling back to NASDAQ listing", exc_info=True)

            if df_list.empty:
                df_list = get_stock_listing('NASDAQ')
        else:
            df_list = get_stock_listing(market)

        if df_list.empty:
            return []

        _VALID_SYM = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')
        mask = df_list['Symbol'].astype(str).str.strip().str.upper().str.match(_VALID_SYM)
        df_list = df_list[mask]

        raw_list = df_list[['Symbol', 'Name']].head(top_n).values.tolist()
        symbols_with_names = [
            (str(sym), str(nm))
            for sym, nm in raw_list
        ]
        fx_rate = get_usd_krw_rate()
        return fetch_us_stock_data_bulk(symbols_with_names, market, fx_rate, progress_callback)
    except Exception:
        # boundary: contract is [] on any failure; the bulk fetch below spans several third-party libraries.
        logger.error("fetch_us_market_data failed for market=%s", market, exc_info=True)
        return []


_US_LISTING_SYM = re.compile(r'^[A-Z]{1,5}([.-][A-Z]{1,2})?$')


def get_us_listing_ranked(market: str, pool_size: int) -> list:
    """The largest common stocks of a US exchange ("NASDAQ" | "NYSE"), largest first:
    [{"Code": Yahoo symbol, "Name", "Marcap": USD market cap}]. The FDR listing comes in
    roughly market-cap order, so only its first `pool_size` symbols are quoted; Yahoo's
    marketCap ranks them and its quoteType drops ETFs / funds / preferreds. Empty on failure."""
    try:
        df = get_stock_listing(market)
    except Exception:
        # boundary: contract is [] on failure; get_stock_listing wraps FDR, which raises arbitrary types.
        logger.warning("US listing failed for %s", market, exc_info=True)
        return []
    if df is None or df.empty or "Symbol" not in df.columns:
        return []
    names = {}
    for sym, nm in df[["Symbol", "Name"]].head(max(pool_size, 1)).values.tolist():
        ysym = str(sym).strip().upper().replace(" ", "-").replace(".", "-")
        if _US_LISTING_SYM.match(ysym):
            names.setdefault(ysym, str(nm))
    quotes = yf_quote_batch(list(names))
    rows = []
    for ysym, nm in names.items():
        q = quotes.get(ysym) or {}
        cap = q.get("marketCap")
        if q.get("quoteType", "EQUITY") != "EQUITY" or not cap:
            continue
        rows.append({"Code": ysym, "Name": nm, "Marcap": float(cap)})
    rows.sort(key=lambda r: -r["Marcap"])
    return rows
