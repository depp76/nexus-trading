"""data/cache.py — In-memory caching, sessions, and timing helpers."""
from datetime import datetime, timedelta
from collections import OrderedDict
import math
import ssl
import threading
import logging
import polars as pl
import urllib3
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


class LazyModule:
    """A module imported on first attribute access. FinanceDataReader pulls in
    plotly (about 0.24 s of the app's start, 2026-11-16 measurement) and only
    worker threads call it, so `fdr = LazyModule("FinanceDataReader")` keeps
    it off the path to the first window. Tests can still patch the name."""

    def __init__(self, name: str):
        self._lazy_name = name
        self._lazy_mod = None

    def __getattr__(self, attr: str):
        mod = self.__dict__["_lazy_mod"]
        if mod is None:
            import importlib
            mod = importlib.import_module(self.__dict__["_lazy_name"])
            self.__dict__["_lazy_mod"] = mod
        return getattr(mod, attr)


fdr = LazyModule("FinanceDataReader")

# What fetching and parsing one external payload can raise when the source is down or
# answers with something unexpected: transport/HTTP errors, an undecodable body
# (json.JSONDecodeError is a ValueError), and a payload whose shape differs from the
# parser's (a missing key, a short list, a list or None where a dict was expected, a
# number where a string was), which surfaces as KeyError/IndexError/TypeError/
# AttributeError. Inner excepts around such a fetch catch this instead of Exception so
# a NameError/ImportError/RuntimeError from our own code is not mistaken for "no data".
PAYLOAD_ERRORS = (requests.RequestException, ValueError, KeyError, IndexError, TypeError, AttributeError)

def start_date() -> str:
    """Lookback start (YYYY-MM-DD) for the daily-history fetches: 410 calendar
    days back (about 285 trading days) safely covers the 200D MA + YTD windows.
    Computed per call rather than once at import so a session left running
    across midnight keeps a correct window (roadmap 6-2f)."""
    return (datetime.now() - timedelta(days=410)).strftime("%Y-%m-%d")


def is_kr_code(ticker) -> bool:
    """True for a 6-character KRX stock/ETF/ETN code (digits, occasionally with
    a letter for preferred/ETN issues), as opposed to a US symbol or an index
    alias. Shared by the fetch routing in data/market.py and the KR/US split in
    ui/history_tab.py (roadmap 6-2e)."""
    t = str(ticker or "")
    return len(t) == 6 and "." not in t and any(c.isdigit() for c in t)


# Market labels that denote a US-listed position. "US" is the generic label
# PositionPriceFetchThread assigns after a Yahoo lookup; the exchange names are
# what TradeEntryDialog's combo offers; "NASDAQ 100" / "S&P500" are the
# universe labels used while the US universe paths were active.
_US_MARKETS = frozenset({"US", "NASDAQ", "NYSE", "AMEX", "NASDAQ 100", "S&P500", "S&P 500"})


def is_us_market(market) -> bool:
    """True when a trade record's `market` label refers to a US listing.

    Single source of truth for the KR/US split of Trading History positions;
    PositionPriceFetchThread (price routing) and TradingHistoryTab (KR/US
    P/L aggregation) used to carry their own, slightly different, tuples.
    """
    return str(market or "").strip().upper() in _US_MARKETS


_TD_PERIODS = {"1d": 1, "3d": 3, "5d": 5, "10d": 10, "20d": 20, "60d": 60, "120d": 120}
_CHANGE_KEYS = tuple(_TD_PERIODS.keys())

# Tickers that FDR resolves natively but yfinance cannot (needs ^ prefix, or is KR-only).
# These are routed directly to fdr.DataReader, bypassing yfinance entirely to
# prevent "possibly delisted; no timezone found" warnings.
_FDR_ONLY_TICKERS = frozenset({
    "JP10YT", "KR3YT",                       # Bond yields (handled elsewhere too)
    "USD/KRW",                               # FX pair
    "KS11", "KQ11",                          # Korean indices without caret
})

# Module-level USD/KRW rate cache (refreshed once per process run)
_USD_KRW_CACHE: dict = {"rate": None, "df": None, "fetched_at": None}
_INDEX_CLOSE_CACHE: dict = {}
_JP10Y_CACHE: dict = {"df": None}
_KR3Y_CACHE: dict = {"df": None}
_VKOSPI_CACHE: dict = {}

# One lock per low-traffic cache above, serializing its check-then-fetch-then-set
# cycle so concurrent callers (e.g. fetch_major_indices_as_stocks fanning out one
# thread per index) don't each redundantly fetch on a cold/stale cache. They used
# to share a single lock, which let one slow download (a timed-out MOF CSV) stall
# the unrelated lookups behind it.
_USD_KRW_LOCK = threading.Lock()
_JP10Y_LOCK = threading.Lock()
_KR3Y_LOCK = threading.Lock()

# Staging cache for yfinance bulk downloads: fetch_us_stock_data_bulk() fills it
# per chunk and _fetch_historical_uncached() reads it before hitting the network.
_YF_BULK_CACHE: dict = {}

# Shared history cache — only non-empty DataFrames are stored,
# so transient fetch failures (e.g. during parallel startup) are retried.
# Capped at _HIST_CACHE_MAX entries; least-recently-used entries are evicted first.
_HIST_CACHE: OrderedDict = OrderedDict()  # (ticker, start) -> polars DataFrame
_HIST_CACHE_LOCK = threading.Lock()
_HIST_CACHE_MAX = 1000  # Maximum number of tickers cached in memory
# Wall-clock time of the last successful network fetch per _HIST_CACHE key (same
# key, same lock; data.history keeps the two dicts in step). Lets _hist_df_is_stale()
# stop re-fetching a frame that merely has no bar for today yet (review_agy.md 2.1).
_HIST_CACHE_FETCHED_AT: dict = {}
# How long a frame whose last bar predates today is still served on a weekday
# before it is fetched again to look for today's bar.
_HIST_CACHE_STALE_TTL = timedelta(minutes=30)

# ── Cache efficiency monitoring (see get_historical_data / _log_hist_cache_stats) ──
# Counters live in one mutable dict rather than two module-level ints so that
# every importer (data.market, the data_fetcher facade, tests) shares the same
# object instead of a stale copy of an int (roadmap 6-2a).
_HIST_CACHE_STATS: dict = {"hits": 0, "misses": 0}
_HIST_CACHE_LOG_INTERVAL = 100  # log the hit/miss ratio every N lookups


class SharedTlsAdapter(HTTPAdapter):
    """An HTTPS adapter whose connections share one SSLContext with the CA
    bundle loaded once.

    requests (2.32.3 and later) hands every new connection the certifi path,
    and urllib3 then parses the whole bundle again for that connection: about
    0.1 s of CPU each, serialized by the GIL. A refresh opens 20-30 Naver
    connections at once, so its first batch waited about 3 s on that alone
    (200 PER requests on 30 workers: 3.4 s -> 0.3 s, measured 2026-11-16).
    Verification is unchanged: the same bundle, CERT_REQUIRED, host name check.
    Only the default `verify=True` is handled here; a custom bundle path or
    verify=False keeps requests' own per-connection behaviour."""

    _ctx = None                      # built at the first request, shared by every instance of the class
    _ctx_lock = threading.Lock()

    @staticmethod
    def _make_context():
        ctx = urllib3.util.ssl_.create_urllib3_context()
        ctx.load_verify_locations(requests.certs.where())
        return ctx

    @classmethod
    def shared_context(cls):
        """The class's one context. Built on first use, in the worker thread
        that makes the first request, so importing this module stays cheap."""
        if cls.__dict__.get("_ctx") is None:
            with cls._ctx_lock:
                if cls.__dict__.get("_ctx") is None:
                    cls._ctx = cls._make_context()
        return cls.__dict__["_ctx"]

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True and host_params.get("scheme") == "https":
            pool_kwargs.pop("ca_certs", None)
            pool_kwargs.pop("ca_cert_dir", None)
            pool_kwargs["ssl_context"] = self.shared_context()
        return host_params, pool_kwargs

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if verify is True:
            conn.ca_certs = None
            conn.ca_cert_dir = None


class YFTlsAdapter(SharedTlsAdapter):
    _ctx = None

    @staticmethod
    def _make_context():
        ctx = SharedTlsAdapter._make_context()
        try:
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        except ssl.SSLError:
            logger.debug("TLS cipher SECLEVEL=1 not supported, using default context")
        return ctx


_YF_SESSION = requests.Session()
_YF_SESSION.mount("https://", YFTlsAdapter())
_YF_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})
_YF_CRUMB = None
_YF_CRUMB_LOCK = threading.Lock()

# Shared session for all Naver Finance endpoints — reuses TCP/TLS connections
# across the many per-ticker requests fired by the collectors in
# data/collectors/naver.py (real-time price batches, PER lookups, listing
# pages, investor-trend pages), instead of each call opening a fresh connection.
_NAVER_SESSION = requests.Session()
_NAVER_SESSION.mount("https://", SharedTlsAdapter(pool_maxsize=100))
_NAVER_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# Session for the Japan MOF JGB CSVs (data/collectors/krx.py::_get_jp10y_df). On 2026-10-19
# a share of new connections to it never completed (2 of 12 attempts not connected within
# 10 s, while the rest connected in ~0.02 s and an immediate new attempt usually succeeded;
# a 15-run check later that day needed 11 retries, 3 of them a second one). The same day
# connects to Yahoo, KRX and GitHub dropped too, so the cause is more likely the local
# network than the MOF host. Either way, connection failures are retried right away
# instead of waiting longer on one attempt. Reads are not retried. One session also lets
# the two CSVs reuse a connection when the server keeps it open.
_MOF_SESSION = requests.Session()
_MOF_SESSION.mount("https://", SharedTlsAdapter(max_retries=urllib3.util.Retry(
    total=None, connect=3, read=0, status=0, other=0, backoff_factor=0)))

# Shared session for the KIS (Korea Investment & Securities) Open API (data/collectors/kis.py).
_KIS_SESSION = requests.Session()
_KIS_SESSION.mount("https://", SharedTlsAdapter())


def _get_yf_crumb(force_refresh: bool = False):
    global _YF_CRUMB
    with _YF_CRUMB_LOCK:
        if _YF_CRUMB is not None and not force_refresh:
            return _YF_CRUMB
        _YF_CRUMB = None  # reset before re-fetch
        try:
            _YF_SESSION.get("https://fc.yahoo.com", timeout=5)
            _YF_CRUMB = _YF_SESSION.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=5).text
        except requests.RequestException:
            logger.warning("Failed to fetch Yahoo Finance crumb, quote batches may fail", exc_info=True)
            _YF_CRUMB = ""
        return _YF_CRUMB


def _record_hist_cache_lookup(hit: bool) -> None:
    """Count one _HIST_CACHE lookup and emit the periodic stats line."""
    _HIST_CACHE_STATS["hits" if hit else "misses"] += 1
    _log_hist_cache_stats()


def _log_hist_cache_stats() -> None:
    """Log _HIST_CACHE's cumulative hit/miss ratio at INFO level every
    _HIST_CACHE_LOG_INTERVAL lookups, so cache effectiveness is visible in app.log
    without adding per-call noise."""
    hits = _HIST_CACHE_STATS["hits"]
    misses = _HIST_CACHE_STATS["misses"]
    total = hits + misses
    if total and total % _HIST_CACHE_LOG_INTERVAL == 0:
        hit_rate = hits / total * 100
        logger.info(
            "_HIST_CACHE stats: %d hits / %d misses (%.1f%% hit rate), size=%d/%d",
            hits, misses, hit_rate, len(_HIST_CACHE), _HIST_CACHE_MAX,
        )


def _hist_df_is_stale(df, fetched_at=None) -> bool:
    """True if a cached polars daily-history df (with a "Date" column) should be
    fetched again: its last bar predates today, today is a weekday, and the frame
    was not fetched within the last _HIST_CACHE_STALE_TTL.

    The last-bar test alone (the rule until 2026-09-28) made every weekday lookup
    a miss whenever the source had no bar for today yet -- before the KR open, on
    KR holidays, and for US-hours instruments all through the Korean day -- so the
    cache re-fetched on every call (review_agy.md 2.1). The TTL bounds that to one
    fetch per key per window while still picking up today's bar once it is
    published. `fetched_at` is the datetime of the entry's last network fetch;
    None (unknown) keeps the plain last-bar rule."""
    if df is None or df.is_empty():
        return False
    now = datetime.now()
    if now.weekday() >= 5:  # Sat/Sun — markets closed, nothing new to fetch
        return False
    if fetched_at is not None and now - fetched_at < _HIST_CACHE_STALE_TTL:
        return False
    try:
        last_date = df.get_column("Date")[-1]
    except (pl.exceptions.ColumnNotFoundError, IndexError):
        logger.debug("_hist_df_is_stale: failed to read last date from df", exc_info=True)
        return False
    return last_date < now.date()


def _pdf_is_stale(pdf) -> bool:
    """Same as _hist_df_is_stale but for pandas DataFrames indexed by date
    (used by the JP10Y/KR3Y/VKOSPI wrappers)."""
    if pdf is None or pdf.empty:
        return False
    if datetime.now().weekday() >= 5:
        return False
    try:
        last_date = pdf.index[-1]
        if hasattr(last_date, "date"):
            last_date = last_date.date()
    except IndexError:
        logger.debug("_pdf_is_stale: failed to read last date from pdf index", exc_info=True)
        return False
    return last_date < datetime.now().date()


def safe_float(value, default=0.0):
    """Safely converts a value to float. Returns default if conversion fails or value is None/NaN."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return default if math.isnan(value) else float(value)
    try:
        val_str = str(value).replace(',', '').strip()
        f = float(val_str)
        return default if math.isnan(f) else f
    except (ValueError, TypeError):
        return default


def per_from_eps(price, eps):
    """price / EPS rounded to 0.1 -- negative for a loss-making company -- or None
    when either is missing, NaN or zero. Fills the trailing PER that Naver
    ("N/A") and Yahoo (no trailingPE) leave empty when trailing EPS is negative."""
    p = safe_float(price, 0.0)
    e = safe_float(eps, 0.0)
    if p <= 0 or e == 0:
        return None
    return round(p / e, 1)


def refresh_row_per(item: dict) -> None:
    """Recompute a Universe row's tPER / fPER from its current price and the
    EPS the full refresh stored (`eps_trailing` / `eps_forward`, in the quote
    currency), so the 60 s price update keeps PER in step with the price.
    Both sources define PER as price / EPS (Naver: 4-quarter and consensus EPS;
    Yahoo: epsTrailingTwelveMonths and epsForward). A row without the EPS
    (cached before 2026-11-20, or no EPS published) keeps its PER as is."""
    price = item.get("usd_price") if item.get("currency") == "$" else item.get("price")
    for per_key, eps_key in (("trailing_per", "eps_trailing"), ("forward_per", "eps_forward")):
        eps = item.get(eps_key)
        if eps is None:
            continue
        per = per_from_eps(price, eps)
        if per is not None:
            item[per_key] = per
