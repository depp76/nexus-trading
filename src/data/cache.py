"""data/cache.py — In-memory caching, sessions, and timing helpers."""
from contextlib import contextmanager
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

class KeyedLocks:
    """One lock per key for single-flight fetches, removed when its last holder or
    waiter is done. Dropping a key's lock as soon as its first holder finished (the
    rule until 2026-11-20) let a newcomer take a fresh lock and run alongside a
    waiter still queued on the old one (review_agy.md H5); the count of threads
    using an entry keeps it until all of them have left."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict = {}   # key -> [lock, number of threads holding or waiting]

    @contextmanager
    def hold(self, key):
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [threading.Lock(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] <= 0 and self._locks.get(key) is entry:
                    del self._locks[key]

    def clear(self) -> None:
        with self._guard:
            self._locks.clear()

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)


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

# Module-level USD/KRW rate cache (data/fx.py). "failed_at" stamps the last failed
# fetch so a fallback rate served without a series is retried (review_agy.md C1).
_USD_KRW_CACHE: dict = {"rate": None, "df": None, "fetched_at": None, "failed_at": None}
_JP10Y_CACHE: dict = {"df": None}
_KR3Y_CACHE: dict = {"df": None}

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
# Up to six chunk threads of one run, and two runs at once (NASDAQ + S&P 500 share
# about 90 symbols), write it, so every access goes through the helpers below under
# _YF_BULK_LOCK; _YF_BULK_REFS counts the runs holding each key, and a key is dropped
# only when the last of them releases it (review_agy.md H2).
_YF_BULK_CACHE: dict = {}
_YF_BULK_REFS: dict = {}
_YF_BULK_LOCK = threading.Lock()


def yf_bulk_put(key, df, held: set) -> None:
    """Stage one bulk-downloaded frame under `key` for the run whose held-key set
    is `held` (the set is updated here, under the lock)."""
    with _YF_BULK_LOCK:
        _YF_BULK_CACHE[key] = df
        if key not in held:
            held.add(key)
            _YF_BULK_REFS[key] = _YF_BULK_REFS.get(key, 0) + 1


def yf_bulk_get(key):
    """The staged frame for `key`, or None."""
    with _YF_BULK_LOCK:
        return _YF_BULK_CACHE.get(key)


def yf_bulk_release(held: set) -> None:
    """Release a finished run's keys; a key another run still holds stays."""
    with _YF_BULK_LOCK:
        for key in held:
            refs = _YF_BULK_REFS.get(key, 0) - 1
            if refs > 0:
                _YF_BULK_REFS[key] = refs
            else:
                _YF_BULK_REFS.pop(key, None)
                _YF_BULK_CACHE.pop(key, None)
        held.clear()

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
_HIST_CACHE_STATS_LOCK = threading.Lock()   # the counters are bumped from every fetch thread
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
# Pool size: fetch_us_stock_data_bulk runs up to 6 chunks x 5 symbol workers on this
# session at once; the default pool of 10 discarded connections under that load (H3).
_YF_SESSION.mount("https://", YFTlsAdapter(pool_maxsize=32))
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


def _get_yf_crumb(force_refresh: bool = False, stale_crumb=None):
    """The cached Yahoo crumb, fetched on first use. `force_refresh` (after a 401)
    fetches a new one, unless `stale_crumb` -- the crumb the caller was rejected
    with -- has already been replaced by another thread, so a burst of 401s from
    parallel chunks triggers one refresh (review_agy.md H3). A failed fetch
    returns "" and leaves the cache empty, so the next call tries again."""
    global _YF_CRUMB
    with _YF_CRUMB_LOCK:
        if _YF_CRUMB is not None and not force_refresh:
            return _YF_CRUMB
        if force_refresh and stale_crumb is not None and _YF_CRUMB and _YF_CRUMB != stale_crumb:
            return _YF_CRUMB
        _YF_CRUMB = None  # reset before re-fetch
        try:
            _YF_SESSION.get("https://fc.yahoo.com", timeout=5)
            res = _YF_SESSION.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=5)
            res.raise_for_status()
            crumb = res.text.strip()
            # A crumb is a short token; anything else is an error page, not a crumb.
            if crumb and len(crumb) <= 64 and not any(c.isspace() or c in "<>{}" for c in crumb):
                _YF_CRUMB = crumb
            else:
                logger.warning("Yahoo Finance crumb reply was not a crumb (%d chars)", len(crumb))
        except requests.RequestException:
            logger.warning("Failed to fetch Yahoo Finance crumb, quote batches may fail", exc_info=True)
        return _YF_CRUMB or ""


def _record_hist_cache_lookup(hit: bool) -> None:
    """Count one _HIST_CACHE lookup and emit the periodic stats line."""
    with _HIST_CACHE_STATS_LOCK:
        _HIST_CACHE_STATS["hits" if hit else "misses"] += 1
        hits, misses = _HIST_CACHE_STATS["hits"], _HIST_CACHE_STATS["misses"]
    _log_hist_cache_stats(hits, misses)


def _log_hist_cache_stats(hits=None, misses=None) -> None:
    """Log _HIST_CACHE's cumulative hit/miss ratio at INFO level every
    _HIST_CACHE_LOG_INTERVAL lookups, so cache effectiveness is visible in app.log
    without adding per-call noise. `hits`/`misses` are the counts the caller
    read under the stats lock (default: the current counters)."""
    if hits is None or misses is None:
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
