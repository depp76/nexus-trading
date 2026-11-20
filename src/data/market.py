"""data/market.py — Market data aggregation: per-market universe builds, single-stock lookup,
index/MA series. Listings, history and FX live one layer down (data.listing / data.history /
data.fx) and are re-exported here for existing callers."""
import re
import threading
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import polars as pl
from data.cache import fdr   # lazy: imported on first use (data.cache.LazyModule)
import logging

from data.cache import (
    PAYLOAD_ERRORS,
    start_date,
    _YF_SESSION,
    safe_float,
    per_from_eps,
    is_kr_code,
)
from data.frames import _to_polars
from data.indicators import (
    CHART_MA_WINDOWS,
    _compute_indicators,
    add_chart_mas,
    chart_ma_column,
    fetch_historical_changes,
)
from data.listing import (  # noqa: F401 - re-exported for callers of data.market
    get_stock_listing,
    _get_listing_with_norm,
    _fetch_kr_listing_fdr_fallback,
)
from data.history import get_historical_data, _fetch_historical_uncached  # noqa: F401 - re-exported
from data.fx import get_usd_krw_rate, get_usd_krw_rate_for_date  # noqa: F401 - re-exported
from data.collectors.naver import (
    _fast_kr_history,
    fetch_naver_realtime_prices,
    fetch_naver_per_batch,
    _fetch_naver_per_single,
    _fetch_naver_info,
    _fetch_kr_listing_naver,
    _get_kr3y_df,
)
from data.collectors.yahoo import fetch_us_market_data
from data.collectors.krx import (
    _get_vkospi_pdf,
    _get_jp10y_df,
)

logger = logging.getLogger(__name__)

# Mapping: display label -> (FDR ticker, color)
INDEX_TICKERS = {
    "KOSPI":     ("^KS11",  "#4a90d9"),
    "KOSDAQ":    ("^KQ11",  "#27ae60"),
    "S&P500":    ("^GSPC",  "#9b59b6"),
    "NASDAQ":    ("^IXIC",  "#e87040"),
    "NASDAQ 100":("^NDX",   "#d35400"),
    "Dow Jones": ("^DJI",   "#8e44ad"),
    "US10YT":    ("^TNX",   "#1abc9c"),
    "JP10YT":    ("JP10YT", "#e74c3c"),
    "KR3YT":     ("KR3YT",  "#2ecc71"),
    "VIX":       ("^VIX",   "#f39c12"),
    "WTI":       ("CL=F",   "#8e44ad"),
    "VKOSPI":    ("VKOSPI", "#c0392b"),
}

_INDEX_DISPLAY_NAMES = {
    "KOSPI":     "KOSPI",
    "KOSDAQ":    "KOSDAQ",
    "S&P500":    "S&P500",
    "NASDAQ":    "Nasdaq",
    "NASDAQ 100":"Nasdaq 100",
    "Dow Jones": "Dow Jones",
    "US10YT":    "US 10Y Treasury",
    "JP10YT":    "JP 10Y Bond",
    "KR3YT":     "KR 3Y Bond",
    "VIX":       "VIX",
    "WTI":       "WTI Crude Oil",
    "VKOSPI":    "VKOSPI",
}
_INDEX_ORDER = {
    "S&P500": 0, "Nasdaq": 1, "Nasdaq 100": 2, "Dow Jones": 3,
    "KOSPI": 4, "KOSDAQ": 5,
    "US 10Y Treasury": 6, "JP 10Y Bond": 7, "KR 3Y Bond": 8,
    "VIX": 9, "VKOSPI": 10, "WTI Crude Oil": 11,
}


def _build_kr_stock_res(code, name, market, marcap):
    if marcap <= 0 or not name or name.startswith('KR_'):
        n_nv, mc_nv = _fetch_naver_info(code)
        if n_nv:
            name = n_nv
        if mc_nv > 0:
            marcap = mc_nv

    naver_prices = fetch_naver_realtime_prices([code])
    current_price = _kr_price_with_fallback(code, naver_prices)
    changes = fetch_historical_changes(code, current_price)
    _, per_vals = _fetch_naver_per_single(code)
    return {
        "ticker": code,
        "name": name,
        "market": market,
        "price": current_price,
        "market_cap": int(marcap),
        "currency": "₩",
        "changes": changes,
        **_kr_per_fields(per_vals, current_price),
    }


def _kr_listing(market, top_n):
    """Top-`top_n` listing rows for `market`: the Naver scrape, else the FDR fallback ([] if both fail)."""
    try:
        results_list = _fetch_kr_listing_naver(market, top_n)
    except PAYLOAD_ERRORS:
        logger.warning("Naver market-listing scrape failed for market=%s", market, exc_info=True)
        results_list = []

    if not results_list:
        logger.info("fetch_kr_market_data: Naver listing empty for market=%s, trying FDR fallback", market)
        results_list = _fetch_kr_listing_fdr_fallback(market, top_n)
    return results_list


def _kr_price_with_fallback(ticker, naver_prices):
    """The Naver quote for `ticker`, or the last close in its daily history when it is
    missing or 0. The history is read with the same start_date() key that
    fetch_historical_changes uses next, so the fallback costs no extra request (it
    fetched a separate 10-day window until 2026-11-20, review_agy.md L2)."""
    current_price = naver_prices.get(ticker, 0.0)
    if current_price == 0:
        try:
            df_p = get_historical_data(ticker, start_date())
            closes = df_p.get_column('Close').drop_nulls() if not df_p.is_empty() else []
            if len(closes) > 0:
                current_price = float(closes[-1])
        except (pl.exceptions.PolarsError, IndexError, ValueError):
            logger.debug("Historical price fallback failed for ticker=%s", ticker, exc_info=True)
    return current_price


def _fetch_kr_rows(results_list, naver_prices, progress_callback):
    """Price + historical changes for every listing row on a 20-worker pool:
    {ticker: (name, marcap, price, changes)}, reporting (done, total) as rows finish."""
    total = len(results_list)
    done_count = 0
    _lock = threading.Lock()

    def _process_one(item):
        nonlocal done_count
        ticker = item['Code']
        name   = item['Name']
        marcap = item['Marcap']

        current_price = _kr_price_with_fallback(ticker, naver_prices)

        changes = fetch_historical_changes(ticker, current_price)
        with _lock:
            done_count += 1
            count = done_count
        if progress_callback:
            progress_callback(min(count, total), total)
        return ticker, str(name), marcap, current_price, changes

    hist_data: dict = {}
    with ThreadPoolExecutor(max_workers=20) as exe:
        futures = [exe.submit(_process_one, item) for item in results_list]
        for f in as_completed(futures):
            t, n, mc, cp, ch = f.result()
            hist_data[t] = (n, mc, cp, ch)
    return hist_data


def _kr_per_fields(vals, price) -> dict:
    """A KR row's PER/EPS fields from fetch_naver_per_batch's values: Naver's PERs,
    a trailing PER Naver left empty (negative EPS, "N/A") computed as price / EPS,
    and the EPS themselves for data.cache.refresh_row_per on the 60 s update."""
    vals = vals or {}
    tper = vals.get("tper")
    if tper is None:
        tper = per_from_eps(price, vals.get("eps"))
    return {
        "trailing_per": tper,
        "forward_per": vals.get("fper"),
        "eps_trailing": vals.get("eps"),
        "eps_forward": vals.get("feps"),
    }


def _assemble_kr_results(results_list, hist_data, market, per_by_code):
    """Universe rows in listing order; rows without fetched data are dropped."""
    results = []
    for item in results_list:
        ticker = item['Code']
        if ticker not in hist_data:
            continue
        name, marcap, current_price, changes = hist_data[ticker]
        results.append({
            "ticker": ticker,
            "name": name,
            "market": market,
            "price": current_price,
            "market_cap": marcap,
            "currency": "₩",
            "changes": changes,
            **_kr_per_fields(per_by_code.get(ticker), current_price),
        })
    return results


def fetch_kr_market_data(market="KOSPI", top_n=200, progress_callback=None):
    try:
        results_list = _kr_listing(market, top_n)
        if not results_list:
            logger.error("fetch_kr_market_data: all listing sources failed for market=%s", market)
            return []

        all_codes = [r['Code'] for r in results_list]
        _bg_exe = ThreadPoolExecutor(max_workers=2)
        try:
            _f_prices = _bg_exe.submit(fetch_naver_realtime_prices, all_codes)
            _f_per    = _bg_exe.submit(fetch_naver_per_batch, all_codes)
            naver_prices = _f_prices.result()
            hist_data = _fetch_kr_rows(results_list, naver_prices, progress_callback)
            per_by_code = _f_per.result()
        finally:
            _bg_exe.shutdown(wait=False)

        return _assemble_kr_results(results_list, hist_data, market, per_by_code)
    except Exception:
        # boundary: contract is [] on any failure; one market's build must not take the refresh down.
        logger.error("fetch_kr_market_data failed for market=%s", market, exc_info=True)
        return []


def fetch_market_data(market, top_n, progress_callback=None):
    if market in ("KOSPI", "KOSDAQ"):
        return fetch_kr_market_data(market, top_n, progress_callback)
    return fetch_us_market_data(market, top_n, progress_callback)


def _match_krx_rows(ticker, search_term):
    """Rows of the KRX listing for `ticker`: by code, then ISU_CD, then the exact and
    finally the partial normalised name."""
    df_krx = get_stock_listing('KRX')
    row_krx = df_krx[df_krx['Code'] == ticker]
    if row_krx.empty and 'ISU_CD' in df_krx.columns:
        row_krx = df_krx[df_krx['ISU_CD'] == ticker]

    if row_krx.empty:
        df_krx_norm = _get_listing_with_norm('KRX')
        row_exact = df_krx_norm[df_krx_norm['NameNorm'] == search_term]
        row_krx = row_exact if not row_exact.empty else \
                  df_krx_norm[df_krx_norm['NameNorm'].str.contains(search_term, na=False, regex=False)]
    return row_krx


def _search_krx_and_etf(market, ticker):
    """First KR lookup: the KRX listing, then the KR ETF listing. Returns
    fetch_single_stock's (result, None) on a match, else None."""
    search_term = re.sub(r'[\s_]+', '', ticker.upper())
    row_krx = _match_krx_rows(ticker, search_term)

    if row_krx.empty:
        df_etf_norm = _get_listing_with_norm('ETF/KR')
        row_etf = df_etf_norm[df_etf_norm['Symbol'] == ticker]
        if row_etf.empty:
            row_etf = df_etf_norm[df_etf_norm['NameNorm'].str.contains(search_term, na=False, regex=False)]
        if not row_etf.empty:
            target = row_etf.iloc[0]
            code = str(target.get('Symbol', ticker))
            if code.isdigit():
                code = code.zfill(6)
            name = str(target.get('Name', ticker))
            return _build_kr_stock_res(code, name, market, 0), None
    else:
        target = row_krx.iloc[0]
        code = str(target.get('Code', ticker)).zfill(6)
        name = str(target.get('Name', ticker))
        marcap = safe_float(target.get('Marcap', 0))
        return _build_kr_stock_res(code, name, market, marcap), None
    return None


def _fetch_single_kr(market, ticker):
    """fetch_single_stock for KOSPI/KOSDAQ: KRX/ETF search, then a bare KR code, then
    the market's own listing by code or name."""
    try:
        found = _search_krx_and_etf(market, ticker)
        if found is not None:
            return found
    except Exception:
        # boundary: best-effort first lookup over pandas listings and the network; any failure falls through to the lookups below.
        logger.warning("KRX search error for ticker='%s'", ticker, exc_info=True)

    if is_kr_code(ticker):
        return _build_kr_stock_res(ticker, "", market, 0), None

    df_listing = get_stock_listing(market)
    row = df_listing[df_listing['Code'] == ticker]
    if row.empty:
        row = df_listing[df_listing['Name'].str.upper().str.contains(ticker.upper(), na=False, regex=False)]
    if not row.empty:
        target = row.iloc[0]
        code = str(target.get('Code', ticker)).zfill(6)
        name = str(target.get('Name', ticker))
        marcap = safe_float(target.get('Marcap', target.get('MarCap', 0)))
        return _build_kr_stock_res(code, name, market, marcap), None

    return None, f"Ticker/Name '{ticker}' not found in {market}"


def _fetch_us_close(ticker):
    """Daily history from FinanceDataReader and its last non-null close in USD
    (0.0 when there is none)."""
    df_p = pl.DataFrame()
    usd_price = 0.0
    try:
        df_pd = fdr.DataReader(ticker, start_date())
        df_p = _to_polars(df_pd)
        if not df_p.is_empty():
            close_s = df_p.get_column("Close").drop_nulls()
            usd_price = float(close_s[-1]) if len(close_s) > 0 else 0.0
    except Exception:
        # kept broad: FinanceDataReader raises arbitrary third-party exception types.
        logger.debug("fdr.DataReader failed for ticker=%s", ticker, exc_info=True)
    return df_p, usd_price


def _yf_name_and_marcap(ticker, yf_symbol):
    """(name, USD market cap, info dict) from yfinance. On failure the name stays the
    ticker, the cap 0, and info whatever yfinance returned before the failure."""
    name = ticker
    usd_marcap = 0
    inf: dict = {}

    try:
        import yfinance as yf
        yft = yf.Ticker(yf_symbol, session=_YF_SESSION)
        inf = yft.info
        usd_marcap = inf.get('marketCap') or inf.get('totalAssets') or 0
        name = inf.get('longName') or inf.get('shortName') or ticker
    except Exception:
        # kept broad: yfinance raises arbitrary third-party exception types.
        logger.debug("yfinance info failed for %s", yf_symbol, exc_info=True)
    return name, usd_marcap, inf


def _yq_name_and_marcap(ticker, yf_symbol, name, usd_marcap):
    """yahooquery fallback for the name and USD market cap; whatever it cannot fill
    is returned unchanged."""
    try:
        from yahooquery import Ticker as YQTicker
        yq = YQTicker(yf_symbol)
        detail = yq.summary_detail.get(yf_symbol, {})
        qt = yq.quote_type.get(yf_symbol, {})
        if isinstance(detail, dict):
            usd_marcap = detail.get('marketCap') or detail.get('totalAssets', 0) or 0
        if isinstance(qt, dict):
            name = qt.get('longName') or qt.get('shortName') or ticker
    except Exception:
        # kept broad: yahooquery raises arbitrary third-party exception types.
        logger.debug("yahooquery summary_detail failed for %s", yf_symbol, exc_info=True)
    return name, usd_marcap


def _per_from_info(inf, ticker):
    """(trailing, forward) PER from a yfinance info dict, rounded to 0.1; None for a
    missing, non-numeric or NaN value."""
    trailing_per = None
    forward_per = None
    try:
        tpe = inf.get('trailingPE') if isinstance(inf, dict) else None
        fpe = inf.get('forwardPE') if isinstance(inf, dict) else None
        if tpe is not None and isinstance(tpe, (int, float)) and tpe == tpe:
            trailing_per = round(float(tpe), 1)
        if fpe is not None and isinstance(fpe, (int, float)) and fpe == fpe:
            forward_per = round(float(fpe), 1)
        if trailing_per is None and isinstance(inf, dict):
            # No trailingPE for a negative trailing EPS: show the negative PER.
            trailing_per = per_from_eps(inf.get('currentPrice') or inf.get('regularMarketPrice'),
                                        inf.get('trailingEps'))
    except (TypeError, ValueError):
        logger.debug("PER extraction failed for %s", ticker, exc_info=True)
    return trailing_per, forward_per


def _eps_from_info(inf) -> dict:
    """The row's EPS fields (USD) from a yfinance info dict, for data.cache.refresh_row_per;
    None for a missing or NaN value."""
    out = {}
    for key, src in (("eps_trailing", "trailingEps"), ("eps_forward", "forwardEps")):
        v = inf.get(src) if isinstance(inf, dict) else None
        out[key] = float(v) if isinstance(v, (int, float)) and v == v else None
    return out


def _fetch_single_us(market, ticker):
    """fetch_single_stock for a US market: FDR price history, name and market cap
    from yfinance (yahooquery when the cap is missing), changes and PER."""
    fx_rate = get_usd_krw_rate()
    yf_symbol = ticker.replace(".", "-")

    df_p, usd_price = _fetch_us_close(ticker)
    if usd_price == 0:
        return None, f"Could not fetch price for '{ticker}'"

    name, usd_marcap, inf = _yf_name_and_marcap(ticker, yf_symbol)
    if usd_marcap == 0:
        name, usd_marcap = _yq_name_and_marcap(ticker, yf_symbol, name, usd_marcap)

    changes = fetch_historical_changes(yf_symbol, usd_price, df_p)
    trailing_per, forward_per = _per_from_info(inf, ticker)

    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "price": usd_price * fx_rate,
        "market_cap": usd_marcap * fx_rate,
        "usd_market_cap": usd_marcap,
        "usd_price": usd_price,
        "currency": "$",
        "changes": changes,
        "trailing_per": trailing_per,
        "forward_per": forward_per,
        **_eps_from_info(inf),
    }, None


def fetch_single_stock(market, ticker):
    """Fetches data for a single stock by market and ticker symbol."""
    try:
        if market in ("KOSPI", "KOSDAQ"):
            return _fetch_single_kr(market, ticker)
        return _fetch_single_us(market, ticker)
    except Exception as e:
        # boundary: contract is (None, message) on any failure; the caller shows the message.
        logger.warning("fetch_single_stock failed for market=%s ticker=%s", market, ticker, exc_info=True)
        return None, str(e)


def _index_lookback_start(target: date) -> str:
    """Start of the history window for a `target` date lookup. The default
    watchlist window (start_date(), 410 days) is reused while the target falls
    inside it; older targets widen it to Jan 1 of the year before, so the
    Total Assets tab keeps its KOSPI columns once its first snapshot ages
    past 410 days (review_agy.md 2.3, 2026-09-29). Year-aligned starts keep
    the number of distinct _HIST_CACHE keys small."""
    default = start_date()
    if target.isoformat() >= default:
        return default
    return f"{target.year - 1}-01-01"


def get_index_close_for_date(ticker: str, date_str: str) -> float:
    """Returns the closing price for an index/ticker for a specific date (YYYY-MM-DD)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.debug("get_index_close_for_date: bad date %r", date_str)
        return 0.0
    df = get_historical_data(ticker, _index_lookback_start(target))
    if df is not None and not df.is_empty():
        try:
            closes = df.filter(pl.col("Date") <= target).get_column("Close").drop_nulls()
            if len(closes) > 0:
                return float(closes[-1])
        except (pl.exceptions.PolarsError, IndexError):
            logger.debug("get_index_close_for_date failed for ticker=%s date=%s", ticker, date_str, exc_info=True)
    return 0.0


# Index MA window lines (docs/ma60_unification_2026-11-18.md 1.2-4): the
# strategy's 60-day line and the long 200-day reference.
INDEX_MA_WINDOWS = (60, 200)


def fetch_index_mas(fdr_ticker, days=365):
    """Close + strict SMA60 / SMA200 (min_samples=window) of an index for the
    last `days` calendar days. The averages are computed over a longer frame
    (_load_with_warmup: 199 valid sessions before the first shown day) and the
    result is cut to the shown range afterwards, so the first shown day has a
    200-day value whenever the history exists; where it does not, it stays
    null (the dialog says "데이터 부족")."""
    try:
        display_start = (datetime.now() - timedelta(days=days)).date()
        full = _load_with_warmup(fdr_ticker, display_start)
        if full.is_empty() or "Close" not in full.columns:
            return None, f"No data for ticker '{fdr_ticker}'."
        df = add_chart_mas(full.select([pl.col("Date"), pl.col("Close").cast(pl.Float64)]), INDEX_MA_WINDOWS)
        df = df.filter(pl.col("Date") >= _date_lit(df, display_start))
        if df.is_empty():
            return None, f"No data for ticker '{fdr_ticker}'."
        return df, None
    except Exception as e:
        # boundary: contract is (None, message) on any failure; FinanceDataReader raises arbitrary types.
        logger.warning("fetch_index_mas failed for ticker=%s", fdr_ticker, exc_info=True)
        return None, str(e)


def fetch_all_indices_mas(days=365):
    """fetch_index_mas for the major indices, concurrently."""
    def _fetch(label_ticker):
        label, (fdr_ticker, _color) = label_ticker
        df, err = fetch_index_mas(fdr_ticker, days)
        return label, df, err

    results = {}
    items = [item for item in INDEX_TICKERS.items()
             if item[0] not in ("Dow Jones", "US10YT", "JP10YT", "KR3YT", "VIX", "WTI", "VKOSPI")]
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = {executor.submit(_fetch, item): item for item in items}
        for future in as_completed(futures):
            label, df, err = future.result()
            results[label] = (df, err)
    return results


_PANDAS_SERIES_LOADERS = {
    "JP10YT": _get_jp10y_df,
    "KR3YT": _get_kr3y_df,
    "VKOSPI": _get_vkospi_pdf,
}


def _naver_code_for(ticker: str):
    """Naver siseJson symbol for a ticker, or None if Naver is not its source."""
    if ticker in ("^KS11", "KS11", "KOSPI"):
        return "KOSPI"
    if ticker in ("^KQ11", "KQ11", "KOSDAQ"):
        return "KOSDAQ"
    if is_kr_code(ticker):
        return ticker
    return None


def _load_ohlcv_window(ticker: str, start: str, end: str = None) -> pl.DataFrame:
    """Daily OHLCV for [start, end] (end=None -> today) as polars, picking the
    source by ticker type: the day-cached pandas series for bonds/VKOSPI, Naver
    for KR indices and KRX codes, FinanceDataReader for everything else (and as
    the fallback when Naver returns nothing)."""
    loader = _PANDAS_SERIES_LOADERS.get(ticker)
    if loader is not None:
        df_pd = loader()
        if df_pd is None:
            return pl.DataFrame()
        df_pd = df_pd[df_pd.index >= start]
        if end:
            df_pd = df_pd[df_pd.index <= end]
        return _to_polars(df_pd)

    naver_code = _naver_code_for(ticker)
    if naver_code is not None:
        df_kr = _fast_kr_history(naver_code, start)
        if not df_kr.is_empty():
            if end:
                end_dt = datetime.strptime(end, "%Y-%m-%d").date()
                df_kr = df_kr.filter(pl.col("Date") <= end_dt)
            return df_kr

    df_pd = fdr.DataReader(ticker, start, end) if end else fdr.DataReader(ticker, start)
    return _to_polars(df_pd)


# Valid sessions loaded ahead of the first shown day so a 200-day average
# has a value there (docs/ma60_unification_2026-11-18.md 1.6).
_WARMUP_BARS = 199


def _date_lit(df: pl.DataFrame, d: date) -> pl.Expr:
    """`d` as a literal of `df`'s Date dtype (Date or Datetime)."""
    return pl.lit(d).cast(df.schema["Date"])


def _as_date(v) -> date:
    return v.date() if isinstance(v, datetime) else v


def _load_with_warmup(ticker: str, display_start: date, end: date = None,
                      bars: int = _WARMUP_BARS) -> pl.DataFrame:
    """_load_ohlcv_window from early enough that `bars` valid closes precede
    `display_start`. A calendar guess (~1.5 calendar days a session) is
    doubled while the count falls short (a trading halt before the shown
    range), until a longer request brings no older bar: then the history
    simply begins there (a new listing) and the frame is returned as it is --
    its first averages stay missing, they are never back-filled."""
    end_s = end.strftime("%Y-%m-%d") if end else None
    days = int(bars * 365 / 240) + 14
    df, first = pl.DataFrame(), None
    for _ in range(4):
        start = display_start - timedelta(days=days)
        longer = _load_ohlcv_window(ticker, start.strftime("%Y-%m-%d"), end_s)
        if longer.is_empty() or "Close" not in longer.columns:
            return df if not df.is_empty() else longer
        new_first = _as_date(longer.get_column("Date")[0])
        if first is not None and new_first >= first:
            return df                       # nothing older exists
        df, first = longer, new_first
        before = df.filter((pl.col("Date") < _date_lit(df, display_start)) & pl.col("Close").is_not_null()).height
        if before >= bars:
            return df
        days *= 3
    return df


def fetch_stock_ma_multi(ticker, market, windows=(10, 20, 60), days=3650, target_year=None):
    """Fetches OHLCV for a single stock and computes MA/RSI using Polars.

    target_year=None: the trailing `days` calendar days (at least 3x the
    widest window) are shown. target_year=YYYY: Sep 1 of the prior year
    through Dec 31 of that year.

    RSI, the MA<w> columns and the divergence ratios come from
    _compute_indicators over the shown range exactly as before. The chart's
    strict SMA20/50/60/200 columns are computed over a longer frame
    (_load_with_warmup) and joined on Date, so they have values from the
    first shown day whenever the history exists (2026-11-18).
    """
    try:
        if target_year is not None:
            display_start = date(target_year - 1, 9, 1)
            end = date(target_year, 12, 31)
        else:
            max_window = max(windows)
            display_start = (datetime.now() - timedelta(days=max(days, max_window * 3))).date()
            end = None

        full = _load_with_warmup(ticker, display_start, end)
        if full.is_empty():
            return None, f"No data for '{ticker}'."
        shown = full.filter(pl.col("Date") >= _date_lit(full, display_start))
        if shown.is_empty():
            return None, f"No data for '{ticker}'."
        df = _compute_indicators(shown, windows)
        sma_cols = [chart_ma_column(w) for w in CHART_MA_WINDOWS]
        chart = (add_chart_mas(full.select(["Date", "Close"]), CHART_MA_WINDOWS)
                 .select(["Date", *sma_cols])
                 .unique(subset="Date", keep="last", maintain_order=True)
                 .with_columns(pl.col("Date").cast(df.schema["Date"])))
        return df.join(chart, on="Date", how="left"), None
    except Exception as e:
        # boundary: contract is (None, message) on any failure; FinanceDataReader raises arbitrary types.
        logger.warning("fetch_stock_ma_multi failed for ticker=%s", ticker, exc_info=True)
        return None, str(e)


# Stock markets whose MA chart can overlay the market's index (user direction
# 2026-11-17); each name is also its INDEX_TICKERS label.
MARKET_INDEX_MARKETS = ("KOSPI", "KOSDAQ", "NASDAQ 100", "S&P500")


def attach_market_index(df: pl.DataFrame, market: str) -> pl.DataFrame:
    """`df` (a stock's daily frame) with a "MarketIndex" column: the close of
    the index of `market` on each of its dates (null where the index has no
    bar). Returned unchanged for other markets or when the index cannot be
    loaded -- the chart then simply has no index line."""
    if market not in MARKET_INDEX_MARKETS or df is None or df.is_empty() or "Date" not in df.columns:
        return df
    fdr_ticker = INDEX_TICKERS[market][0]
    try:
        first = df.get_column("Date")[0]
        idx = _load_ohlcv_window(fdr_ticker, first.strftime("%Y-%m-%d"))
        if idx.is_empty() or "Close" not in idx.columns:
            return df
        idx = idx.select([
            pl.col("Date").cast(df.schema["Date"]),
            pl.col("Close").cast(pl.Float64).alias("MarketIndex"),
        ]).unique(subset="Date", keep="last")
        return df.join(idx, on="Date", how="left")
    except Exception:
        # kept broad: FinanceDataReader raises arbitrary third-party exception types.
        logger.warning("attach_market_index failed for market=%s", market, exc_info=True)
        return df


def fetch_indice_as_stock(label_ticker):
    label, (fdr_ticker, _color) = label_ticker
    is_bond = label in ("US10YT", "JP10YT", "KR3YT")

    try:
        if label == "JP10YT":
            df = _to_polars(_get_jp10y_df())
        elif label == "KR3YT":
            df = _to_polars(_get_kr3y_df())
        elif label == "VKOSPI":
            df = _to_polars(_get_vkospi_pdf())
        elif label in ("KOSPI", "KOSDAQ") or fdr_ticker in ("^KS11", "^KQ11"):
            naver_code = "KOSPI" if label == "KOSPI" or fdr_ticker in ("^KS11", "KS11") else "KOSDAQ"
            df = _fast_kr_history(naver_code, start_date())
        else:
            df = get_historical_data(fdr_ticker, start_date())

        if df.is_empty():
            return None
        close_series = df.get_column("Close").drop_nulls()
        val = close_series[-1] if len(close_series) > 0 else None
        current_price = float(val) if val is not None else 0.0

        # For domestic indices, attempt to get exact real-time quote from polling API
        if label in ("KOSPI", "KOSDAQ") or fdr_ticker in ("^KS11", "^KQ11"):
            try:
                from data.collectors.naver import fetch_naver_realtime_index_prices
                rt_dict = fetch_naver_realtime_index_prices([label])
                rt_val = rt_dict.get(fdr_ticker) or rt_dict.get(label)
                if rt_val is not None and rt_val > 0:
                    current_price = float(rt_val)
            except (TypeError, ValueError):
                logger.debug("Realtime index quote for %s failed; keeping last close", label, exc_info=True)

        if is_bond:
            chg_mode = 'bp'
        elif label in ('VIX', 'WTI', 'VKOSPI'):
            chg_mode = 'abs'
        else:
            chg_mode = 'pct'

        changes = fetch_historical_changes(fdr_ticker, current_price, df, mode=chg_mode)
        name = _INDEX_DISPLAY_NAMES.get(label, label)
        order = _INDEX_ORDER.get(name, 99)

        return {
            "ticker": fdr_ticker,
            "name": name,
            "market": "Index",
            "price": current_price,
            "market_cap": float('inf'),
            "usd_price": current_price,
            "currency": "%" if is_bond else ("$" if label == "WTI" else ""),
            "changes": changes,
            "is_index": True,
            "is_bond": is_bond,
            "change_mode": chg_mode,
            "index_order": order,
        }
    except Exception:
        # boundary: one index must not drop the others; contract is None on any failure.
        logger.error("Index fetch failed for label=%s", label, exc_info=True)
        return None


def fetch_major_indices_as_stocks():
    n = len(INDEX_TICKERS)
    results = []
    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = {executor.submit(fetch_indice_as_stock, item): item for item in INDEX_TICKERS.items()}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
    results.sort(key=lambda x: x.get('index_order', 99))
    return results
