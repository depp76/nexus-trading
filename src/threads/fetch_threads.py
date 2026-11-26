"""threads/fetch_threads.py — Background QThread classes (Phase 3-1 split)

Split out from: main.py (2026-08-29 feat/3-1-modularize)
Contains:
  IndexMaThread, StockMaThread,
  SingleStockFetchThread, AllDataFetchThread,
  UniverseLightweightFetchThread, PositionPriceFetchThread,
  AutoBackupThread, KisAccountThread, GeminiStockReportThread
"""
import os
import re
import shutil
import datetime as _dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from PyQt6.QtCore import QThread, pyqtSignal

from i18n import tr
from paths import BASE_DIR, DB_FILE, CUSTOM_SETTINGS_FILE, ARCHIVE_DIR
from data_fetcher import (
    fetch_market_data,
    fetch_single_stock,
    fetch_all_indices_mas,
    fetch_stock_ma_multi,
    _fetch_naver_info,
    get_usd_krw_rate,
    get_usd_krw_rate_for_date,
    get_index_close_for_date,
    get_historical_data,
    is_us_market,
    start_date,
    INDEX_TICKERS,
    MARKET_INDEX_MARKETS,
    attach_market_index,
)

logger = logging.getLogger(__name__)

# portfolio.db holds both the trade log and (since 2026-09-19) the Total Assets
# snapshots, so it and custom_settings.json are everything typed in by hand.
_AUTO_BACKUP_FILES = [DB_FILE, CUSTOM_SETTINGS_FILE]
_AUTO_BACKUP_MAX_KEEP = 7  # keep only the most recent N automatic backups


# KRX regular session ends 15:30; the day's bar is final a little after.
_KR_CLOSE_FINAL = _dt.time(15, 40)
# Index tickers whose panels get the regime shading (each its own market's
# index, docs/ma60_unification_2026-11-18.md 1.2-4), and how far back their
# history is read: MA60 + its 5-session slope + the previous weekly check
# before the first shown day, with room to spare.
_REGIME_INDEX = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}
_REGIME_LOOKBACK_DAYS = 200


def kr_bar_is_provisional(last_date, now=None) -> bool:
    """True when a KR daily bar dated `last_date` is still the intraday one
    (today, a weekday, before the close is final)."""
    now = now or _dt.datetime.now()
    if isinstance(last_date, _dt.datetime):
        last_date = last_date.date()
    return last_date == now.date() and now.weekday() < 5 and now.time() < _KR_CLOSE_FINAL


def index_regimes(results: dict, variant: str | None = None, now=None) -> dict | None:
    """The KOSPI / KOSDAQ panels' regime shading under `variant` (default:
    the screens' default variant): {"variant", "uses", "legend": (label,
    tooltip), "spans": {panel label: [RegimeSpan]}}. Each index is judged on
    its own history, read from before the first shown day so the state on
    that day is the one the strategy would have. None when the strategy layer
    cannot be loaded."""
    from strategy import signal_api
    try:
        vid = variant or signal_api.default_variant()
        out = {"variant": vid, "uses": signal_api.uses_regime(vid),
               "legend": signal_api.regime_legend(vid), "spans": {}}
    except Exception:
        # boundary: the strategy registry imports every provider module.
        logger.warning("regime shading unavailable", exc_info=True)
        return None
    if not out["uses"]:
        return out
    for label, ticker in _REGIME_INDEX.items():
        df = (results.get(label) or (None, None))[0]
        if df is None or df.is_empty():
            continue
        try:
            first = df.get_column("Date")[0]
            first = first.date() if isinstance(first, _dt.datetime) else first
            start = (first - _dt.timedelta(days=_REGIME_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            hist = get_historical_data(ticker, start)
            if hist is None or hist.is_empty():
                continue
            dates = list(hist.get_column("Date"))
            closes = hist.get_column("Close").to_numpy()
            out["spans"][label] = signal_api.regime_spans(
                vid, label, dates, closes, provisional_last=kr_bar_is_provisional(dates[-1], now))
        except Exception:
            # kept broad: one panel's history (third-party sources) must not drop the others.
            logger.warning("regime spans failed for %s", label, exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Index MA fetch thread
# ---------------------------------------------------------------------------
class IndexMaThread(QThread):
    # {label: (df|None, err)}, index_regimes() result (or None)
    finished = pyqtSignal(object, object)

    def __init__(self, variant: str | None = None):
        super().__init__()
        self.variant = variant

    def run(self):
        try:
            results = fetch_all_indices_mas()
            regimes = index_regimes(results, self.variant)
        except Exception:
            # boundary: QThread.run; always emit so the dialog leaves its loading state (review_agy H7).
            logger.error("IndexMaThread failed", exc_info=True)
            results, regimes = {}, None
        self.finished.emit(results, regimes)


def stock_chart_overlays(ticker: str, market: str, df, variant: str | None = None, now=None) -> list:
    """The strategy's price-panel lines for a stock chart (the breakout
    channel, strategy.signal_api.chart_overlays) under `variant` (default:
    the screens' default variant); [] for other markets or on failure."""
    if df is None or df.is_empty() or not all(c in df.columns for c in ("Date", "High", "Low", "Close")):
        return []
    try:
        from strategy import signal_api
        vid = variant or signal_api.default_variant()
        last = df.get_column("Date")[-1]
        return signal_api.chart_overlays(vid, ticker, market, df,
                                         provisional_last=kr_bar_is_provisional(last, now))
    except Exception:
        # boundary: provider code runs here; the chart opens without the channel.
        logger.warning("chart overlays failed for %s", ticker, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Stock MA fetch thread
# ---------------------------------------------------------------------------
class StockMaThread(QThread):
    """Background thread: one stock's daily frame for the MA chart -- RSI,
    the divergence ratios and the chart's strict SMA20/50/60/200 (with their
    warm-up history, data.market.fetch_stock_ma_multi) in one call."""
    # ticker, name, df|None, error, investor_data, market, change_mode, overlays --
    # market / change_mode echo the constructor args so slots can be plain bound
    # methods instead of closures capturing per-request context (CLAUDE.md threads
    # rule); overlays are the strategy's chart lines (stock_chart_overlays).
    finished = pyqtSignal(str, str, object, str, list, str, str, object)

    def __init__(self, ticker, name, market, change_mode='pct', variant=None):
        super().__init__()
        self.ticker = ticker
        self.name = name
        self.market = market
        self.change_mode = change_mode
        self.variant = variant      # the strategy variant whose chart lines to draw (None: default)

    def run(self):
        try:
            self._run()
        except Exception as e:
            # boundary: QThread.run; always emit so the caller's loading state ends (review_agy H7).
            logger.error("StockMaThread failed for %s", self.ticker, exc_info=True)
            self.finished.emit(self.ticker, self.name, None, str(e) or type(e).__name__, [],
                               self.market, self.change_mode, [])

    def _run(self):
        # MA20 feeds the MA20 divergence ratio; the chart's lines are the SMA columns.
        df, err = fetch_stock_ma_multi(self.ticker, self.market, windows=(20,))

        # --- Add Equal Weight Index for KOSPI ---
        if self.ticker in ("KS11", "^KS11") and df is not None and not df.is_empty():
            try:
                import FinanceDataReader as fdr
                import polars as pl
                from data_fetcher import _to_polars
                first_date_str = df.get_column("Date")[0].strftime("%Y-%m-%d")
                ew_pd = fdr.DataReader("252650", first_date_str)
                if not ew_pd.empty:
                    ew_df = _to_polars(ew_pd)
                    if not ew_df.is_empty() and "Close" in ew_df.columns:
                        ew_df = ew_df.select([pl.col("Date"), pl.col("Close").alias("EqualWeight")])
                        df = df.join(ew_df, on="Date", how="left")
            except Exception as e:
                # kept broad: FinanceDataReader raises arbitrary third-party exception types.
                logger.warning("Error fetching KODEX 200 EW: %s", e, exc_info=True)
        # ----------------------------------------

        # The index of the stock's market, for the chart's "시장지수" toggle.
        if self.market in MARKET_INDEX_MARKETS and df is not None:
            df = attach_market_index(df, self.market)

        investor_data = []
        if self.market in ("KOSPI", "KOSDAQ") or (self.market == "Index" and self.ticker in ("KS11", "KQ11", "^KS11", "^KQ11")):
            from data_fetcher import fetch_investor_trend
            investor_data = fetch_investor_trend(self.ticker, days=60)
        elif self.ticker == "CL=F":
            from data_fetcher import fetch_wti_futures_curve
            investor_data = fetch_wti_futures_curve()
        overlays = stock_chart_overlays(self.ticker, self.market, df, self.variant)
        self.finished.emit(self.ticker, self.name, df, err or "", investor_data, self.market, self.change_mode,
                           overlays)


# ---------------------------------------------------------------------------
# Single stock fetch thread
# ---------------------------------------------------------------------------
class SingleStockFetchThread(QThread):
    """Background thread to fetch a single stock without blocking the UI.

    finished(result | None, error, ticker): the requested ticker is echoed so
    callers that fan out several lookups can tell the results apart in a bound
    method slot."""
    finished = pyqtSignal(object, str, str)

    def __init__(self, market, ticker):
        super().__init__()
        self.market = market
        self.ticker = ticker

    def run(self):
        try:
            result, error = fetch_single_stock(self.market, self.ticker)
        except Exception as e:
            # boundary: QThread.run; always emit so the Add button is re-enabled (review_agy H7).
            logger.error("SingleStockFetchThread failed for %s", self.ticker, exc_info=True)
            result, error = None, str(e) or type(e).__name__
        self.finished.emit(result, error or "", self.ticker)


# ---------------------------------------------------------------------------
# Ticker validation thread (TradeEntryDialog)
# ---------------------------------------------------------------------------
class TickerValidateThread(QThread):
    """Resolve a ticker's display name for TradeEntryDialog without blocking the UI.

    Tries fetch_single_stock first, then falls back to yahooquery, then (for
    KOSPI/KOSDAQ) Naver — the same three-source chain TradeEntryDialog.on_save()
    used to run synchronously on the UI thread.
    """
    finished = pyqtSignal(bool, str, str)  # is_valid, company, error

    def __init__(self, market, ticker):
        super().__init__()
        self.market = market
        self.ticker = ticker

    def run(self):
        market, ticker = self.market, self.ticker
        is_valid = False
        company = ticker
        err = ""
        try:
            res, err = fetch_single_stock(market, ticker)
            if res is not None:
                is_valid = True
                company = res.get("name", ticker)

            if not is_valid or company == ticker or company.upper() == ticker.upper():
                try:
                    from yahooquery import Ticker as YQTicker
                    yf_sym = ticker
                    if market == "KOSPI":
                        yf_sym = f"{ticker}.KS"
                    elif market == "KOSDAQ":
                        yf_sym = f"{ticker}.KQ"
                    elif "." in ticker:
                        yf_sym = ticker.replace(".", "-")

                    qt = YQTicker(yf_sym).quote_type
                    if qt and isinstance(qt, dict) and yf_sym in qt and isinstance(qt[yf_sym], dict):
                        fetched = qt[yf_sym].get('longName') or qt[yf_sym].get('shortName')
                        if fetched:
                            company = fetched
                            is_valid = True
                except Exception:
                    # kept broad: yahooquery raises arbitrary third-party exception types.
                    logger.debug("yahooquery company-name lookup failed for ticker=%s", ticker, exc_info=True)

            if (not is_valid or company == ticker or company.upper() == ticker.upper()) and market in ("KOSPI", "KOSDAQ"):
                try:
                    n_nv, _ = _fetch_naver_info(ticker)
                    if n_nv:
                        company = n_nv
                        is_valid = True
                except Exception:
                    # boundary: last fallback of the name chain; _fetch_naver_info handles its own fetch errors, so only bugs land here.
                    logger.warning("Naver company-name lookup failed for ticker=%s", ticker, exc_info=True)
        except Exception as e:
            # boundary: the thread must always emit finished so the dialog is released.
            logger.warning("TickerValidateThread failed for ticker=%s", ticker, exc_info=True)
            err = str(e)

        self.finished.emit(is_valid, company, err or "")


# ---------------------------------------------------------------------------
# Asset-tab metrics preload thread (USD/KRW + KOSPI history warm-up)
# ---------------------------------------------------------------------------
class AssetMetricsPreloadThread(QThread):
    """Builds TradingRecordTab's USD/KRW-rate and KOSPI-close snapshot off the UI thread.

    get_usd_krw_rate_for_date()/get_index_close_for_date() trigger a full
    network fetch (fdr.DataReader('USD/KRW') / get_historical_data("KS11", ...))
    on their first call per session and again whenever their cache goes stale,
    so the tab never calls them itself: this thread looks up every requested
    date plus the current rate and emits the result, and the tab renders from
    that snapshot only (review_codex.md #9). A failed lookup yields 0.0, which
    the tab shows as "-".
    """
    finished = pyqtSignal(dict, float)  # {date: (usd_krw, kospi_close)}, current usd_krw

    def __init__(self, dates=()):
        super().__init__()
        self._dates = list(dict.fromkeys(dates))

    def run(self):
        try:
            rate = float(get_usd_krw_rate())
        except Exception:
            # boundary: get_usd_krw_rate already falls back on fetch errors; a bug must not kill the preload.
            rate = 0.0
            logger.warning("AssetMetricsPreloadThread: USD/KRW fetch failed", exc_info=True)
        try:
            get_historical_data("KS11", start_date())
        except Exception:
            # boundary: get_historical_data returns an empty frame on fetch errors; a bug must not kill the preload.
            logger.warning("AssetMetricsPreloadThread: KOSPI warm-up failed", exc_info=True)
        snapshot = {}
        for d in self._dates:
            try:
                usd = float(get_usd_krw_rate_for_date(d))
            except Exception:
                # boundary: a failed lookup is shown as "-" (0.0); see the class docstring.
                logger.warning("AssetMetricsPreloadThread: USD/KRW lookup failed for %s", d, exc_info=True)
                usd = 0.0
            try:
                kospi = float(get_index_close_for_date("KS11", d))
            except Exception:
                # boundary: as above.
                logger.warning("AssetMetricsPreloadThread: KOSPI lookup failed for %s", d, exc_info=True)
                kospi = 0.0
            snapshot[d] = (usd, kospi)
        self.finished.emit(snapshot, rate)


class AssetReportThread(QThread):
    """Fetches what the Total Assets "Report" needs (ui/assets_report.py):
    the trade log, KOSPI closes from `start`, each traded ticker's daily
    closes and the USD/KRW rate per KOSPI trading day. The tab does the
    (pure, cheap) report maths on the UI thread from what this emits.
    A ticker whose history fails is left out of `closes` (valued at cost).
    """
    # trades, {ticker: [(date, close)]}, [(date, kospi_close)], {date: usd_krw}, error
    finished = pyqtSignal(list, dict, list, dict, str)

    def __init__(self, start: str):
        super().__init__()
        self._start = start

    @staticmethod
    def _series(df) -> list:
        if df is None or df.is_empty():
            return []
        return [(str(d)[:10], float(c)) for d, c in zip(df.get_column("Date"), df.get_column("Close"))
                if c is not None]

    def run(self):
        import trade_db
        try:
            trades = trade_db.load_all_trades()
            kospi = self._series(get_historical_data("KS11", self._start))
        except Exception as e:
            # boundary: the thread must always emit finished; covers trade_db (sqlite3) and the KOSPI fetch.
            logger.warning("AssetReportThread: trades/KOSPI load failed", exc_info=True)
            self.finished.emit([], {}, [], {}, str(e))
            return
        # Only tickers held at some point on or after start.
        tickers = {t["ticker"] for t in trades
                   if t.get("ticker") and not (t.get("sell_date") and str(t["sell_date"])[:10] < self._start)}
        closes = {}
        for tk in sorted(tickers):
            try:
                series = self._series(get_historical_data(tk, self._start))
            except Exception:
                # boundary: a ticker whose history fails is left out (valued at cost), see the class docstring.
                logger.warning("AssetReportThread: history failed for %s", tk, exc_info=True)
                continue
            if series:
                closes[tk] = series
        usd_krw = {}
        if any(is_us_market(t.get("market")) for t in trades if t.get("ticker") in closes):
            for d, _ in kospi:
                try:
                    usd_krw[d] = float(get_usd_krw_rate_for_date(d))
                except Exception:
                    # boundary: a failed rate is 0.0, as for the other lookups of this thread.
                    logger.warning("AssetReportThread: USD/KRW lookup failed for %s", d, exc_info=True)
                    usd_krw[d] = 0.0
        self.finished.emit(trades, closes, kospi, usd_krw, "")


# ---------------------------------------------------------------------------
# All-market data fetch thread (full load)
# ---------------------------------------------------------------------------
# The Trading Universe's markets and how many names each brings, in display
# order (docs/ui.md 2.1). Only the markets switched on in the toolbar are
# fetched; NASDAQ 150 and S&P500 are off by default.
UNIVERSE_MARKET_SIZES = {"KOSPI": 500, "KOSDAQ": 700, "NASDAQ 150": 150, "S&P500": 510}
DEFAULT_UNIVERSE_MARKETS = ("KOSPI", "KOSDAQ")


class AllDataFetchThread(QThread):
    market_loaded = pyqtSignal(str, list)
    market_progress = pyqtSignal(str, int, int)
    finished_all = pyqtSignal(list)
    # (reason, failed parts) just before finished_all when any part --
    # "Indices" or one of the markets -- raised or came back empty, so the
    # tab can keep its previous rows for those parts (docs/ui.md 8.8).
    failed = pyqtSignal(str, list)

    def __init__(self, markets=DEFAULT_UNIVERSE_MARKETS, include_indices: bool = True):
        """`markets`: which of UNIVERSE_MARKET_SIZES to fetch; `include_indices`
        False for a market switched on after a refresh (only its rows)."""
        super().__init__()
        self.markets = tuple(m for m in UNIVERSE_MARKET_SIZES if m in set(markets))
        self.include_indices = include_indices

    def run(self):
        from data_fetcher import fetch_major_indices_as_stocks

        all_data = []
        failed_parts = []
        try:
            # Indices and the selected markets all at once (2026-11-16: the
            # index step used to finish first, 1.3-2.7 s, before any market
            # started). The rows are still assembled indices first.
            markets_config = [(m, UNIVERSE_MARKET_SIZES[m]) for m in self.markets]

            if self.include_indices:
                self.market_progress.emit("Indices", 0, len(INDEX_TICKERS))
            for market, top_n in markets_config:
                self.market_progress.emit(market, 0, top_n)

            def fetch_market_task(market, top_n):
                def progress_cb(current, total, m=market):
                    self.market_progress.emit(m, current, total)
                return fetch_market_data(market, top_n, progress_cb)

            indices_data = []
            market_results = {}
            n_tasks = len(markets_config) + (1 if self.include_indices else 0)
            with ThreadPoolExecutor(max_workers=max(1, n_tasks)) as executor:
                futures = {executor.submit(fetch_market_task, m, n): m for m, n in markets_config}
                if self.include_indices:
                    futures[executor.submit(fetch_major_indices_as_stocks)] = "Indices"
                for future in as_completed(futures):
                    part = futures[future]
                    try:
                        data = future.result()
                    except Exception as e:
                        # boundary: one part's failure must not lose the others.
                        logger.warning("Error fetching %s: %s", part, e, exc_info=True)
                        if part == "Indices":
                            failed_parts.append("Indices")
                        else:
                            market_results[part] = []
                        continue
                    self.market_loaded.emit(part, data)
                    if part == "Indices":
                        indices_data = data
                        if not data:
                            failed_parts.append("Indices")
                    else:
                        market_results[part] = data
            all_data.extend(indices_data)

            # A name in both NASDAQ 150 and S&P500 (AAPL, MSFT, ...) is one
            # row, under the first market in display order; `also_in` lists
            # its other markets, so switching the first one off keeps the row
            # under the other (UniverseTab._rows_for_markets).
            first = {x.get("ticker"): x for x in all_data}
            for market, _ in markets_config:
                rows = market_results.get(market, [])
                for x in rows:
                    kept = first.get(x.get("ticker"))
                    if kept is None:
                        first[x.get("ticker")] = x
                        all_data.append(x)
                    elif kept.get("market") != market and market not in kept.get("also_in", []):
                        kept.setdefault("also_in", []).append(market)
                if not rows:
                    failed_parts.append(market)
            del market_results  # release the intermediate dict to free memory
        except Exception as e:
            # boundary: the thread must reach finally and emit finished.
            logger.warning("Error inside AllDataFetchThread.run: %s", e, exc_info=True)
            failed_parts = (["Indices"] if self.include_indices else []) + list(self.markets)
        finally:
            if failed_parts:
                parts = list(dict.fromkeys(failed_parts))
                self.failed.emit(tr(f"데이터 없음: {', '.join(parts)}", f"no data for {', '.join(parts)}"), parts)
            self.finished_all.emit(all_data)


# ---------------------------------------------------------------------------
# Lightweight universe refresh thread (periodic 60-s tick)
# ---------------------------------------------------------------------------
class UniverseLightweightFetchThread(QThread):
    finished_all = pyqtSignal(list)
    status_message = pyqtSignal(str)  # short progress text for MainWindow's status bar
    # Emitted just before finished_all when the tick fetched nothing usable:
    # an exception, or no quote at all for the tickers it asked for (the
    # collectors return {} on a network failure instead of raising). A tick
    # that gets some quotes is not a failure. (docs/ui.md 1.10 / 8.7)
    failed = pyqtSignal(str)

    def __init__(self, current_data: list):
        super().__init__()
        # Shallow per-item copy is enough: run() only reassigns top-level keys
        # (price/changes/usd_price) on each item, never mutates nested values
        # in place — a full deepcopy of ~hundreds of nested "changes" dicts
        # every 60s tick is unnecessary work.
        self.current_data = [dict(item) for item in current_data]

    def run(self):
        from data_fetcher import (
            fetch_naver_realtime_prices,
            fetch_naver_realtime_index_prices,
            fetch_us_realtime_prices,
            fetch_historical_changes,
            refresh_row_per,
        )

        error = ""
        try:
            kr_tickers = []
            us_yf_tickers = []
            kr_index_tickers = []
            for item in self.current_data:
                ticker = item.get("ticker", "")
                if not ticker:
                    continue

                if item.get("is_index", False):
                    if ticker in ("^KS11", "KS11", "KOSPI", "^KQ11", "KQ11", "KOSDAQ"):
                        kr_index_tickers.append(ticker)
                    elif ticker.startswith("^") or ticker == "CL=F":
                        us_yf_tickers.append(ticker)
                elif item.get("market") in ("KOSPI", "KOSDAQ"):
                    kr_tickers.append(ticker)
                else:
                    us_yf_tickers.append(ticker)

            prices_dict = {}
            if kr_tickers:
                self.status_message.emit(tr(f"국내 시세 받는 중... (Naver, {len(kr_tickers)}종목)",
                                             f"Fetching KR quotes... (Naver, {len(kr_tickers)} tickers)"))
                prices_dict.update(fetch_naver_realtime_prices(kr_tickers))
            if kr_index_tickers:
                prices_dict.update(fetch_naver_realtime_index_prices(kr_index_tickers))
            if us_yf_tickers:
                self.status_message.emit(tr("Yahoo Finance 응답 기다리는 중...", "Waiting for Yahoo Finance response..."))
                prices_dict.update(fetch_us_realtime_prices(us_yf_tickers))
            if (kr_tickers or kr_index_tickers or us_yf_tickers) and not prices_dict:
                error = tr("시세를 받지 못함 (Naver/Yahoo)", "no quotes received (Naver/Yahoo)")

            updated_count = 0
            for item in self.current_data:
                ticker = item.get("ticker", "")
                if not ticker or ticker not in prices_dict:
                    continue

                new_price = prices_dict[ticker]
                # A US equity row keeps its quote in usd_price (what the Price
                # cell shows) and price in KRW; Yahoo's quote is in USD.
                usd_row = item.get("currency") == "$" and not item.get("is_index", False)
                if (item.get("usd_price") if usd_row else item.get("price", 0.0)) != new_price:
                    if usd_row:
                        old_price, old_usd = item.get("price") or 0.0, item.get("usd_price") or 0.0
                        # Same KRW rate as the full refresh used (the row's own ratio).
                        item["price"] = new_price * (old_price / old_usd) if old_price > 0 and old_usd > 0 else new_price
                        item["usd_price"] = new_price
                    else:
                        item["price"] = new_price
                    try:
                        chg_mode = item.get("change_mode", "pct")
                        chg = fetch_historical_changes(ticker, new_price, mode=chg_mode)
                        item["changes"] = chg
                        if item.get("is_index", False):
                            item["usd_price"] = new_price
                        # PER follows the price (2026-11-20): price / the stored EPS.
                        refresh_row_per(item)
                        updated_count += 1
                    except Exception:
                        # boundary: one ticker's change-rate failure must not stop the price update of the rest.
                        logger.warning(
                            "UniverseLightweightFetchThread: change-rate fetch failed for ticker=%s",
                            ticker, exc_info=True,
                        )
        except Exception as e:
            # boundary: the thread must reach finally and report the error.
            logger.warning("Error in UniverseLightweightFetchThread: %s", e, exc_info=True)
            error = str(e) or type(e).__name__
        finally:
            if error:
                self.failed.emit(error)
            self.finished_all.emit(self.current_data)


# ---------------------------------------------------------------------------
# Position price fetch thread (Trading History tab)
# ---------------------------------------------------------------------------
class PositionPriceFetchThread(QThread):
    """
    Background thread: resolves company names to KRX codes,
    fetches real-time prices from Naver Finance, and emits P/L results.

    Signal prices_ready(results, request_id): list of dicts, one per input
    position (``index`` = its position in the input lists), and the
    ``request_id`` the caller passed in, echoed back so the slot can drop a
    stale run's results (review_codex.md C10).
    """
    prices_ready = pyqtSignal(list, int)
    status_message = pyqtSignal(str)  # short progress text, forwarded to MainWindow's status bar

    def __init__(self, names: list, tickers: list, markets: list, buy_prices: list, qtys: list, buy_amts: list, is_open: list = None, skip_fetch: list = None,
                 request_id: int = 0):
        super().__init__()
        self.request_id = request_id
        self._names      = names
        self._tickers    = tickers
        self._markets    = markets
        self._buy_prices = buy_prices
        self._qtys       = qtys
        self._buy_amts   = buy_amts
        self._is_open    = is_open if is_open is not None else [True] * len(names)
        # skip_fetch[i]=True - skip price/history fetch (name-resolve only, e.g. stale closed positions)
        self._skip_fetch = skip_fetch if skip_fetch is not None else [False] * len(names)

    def run(self):
        try:
            results = self._run()
        except Exception:
            # boundary: QThread.run; an empty result still clears the caller's
            # "loading prices" label (review_agy H7).
            logger.error("PositionPriceFetchThread failed", exc_info=True)
            results = []
        self.prices_ready.emit(results, self.request_id)

    def _run(self) -> list:
        name_to_info = self._resolve_kr_codes()
        price_map = self._fetch_kr_prices(name_to_info)
        us_price_map, us_price_map_usd = self._fetch_us_prices(name_to_info)

        def _compute_pl(i, name):
            return self._compute_pl(i, name, name_to_info, price_map, us_price_map, us_price_map_usd)

        max_workers = min(10, max(1, len(self._names)))
        with ThreadPoolExecutor(max_workers=max_workers) as exe:
            return list(exe.map(lambda arg: _compute_pl(*arg), enumerate(self._names)))

    @staticmethod
    def _match_listing(name: str, df_krx, df_etf):
        """KRX then ETF listing row for `name` ({"code", "market"}), exact NameNorm first; None if absent."""
        search_term = re.sub(r'[\s_]+', '', name.upper())

        row = df_krx[df_krx['NameNorm'] == search_term]
        if row.empty:
            row = df_krx[df_krx['NameNorm'].str.contains(search_term, regex=False, na=False)]

        if row.empty:
            row = df_etf[df_etf['NameNorm'] == search_term]
        if row.empty:
            row = df_etf[df_etf['NameNorm'].str.contains(search_term, regex=False, na=False)]

        if row.empty:
            return None
        code_col = 'Symbol' if 'Symbol' in row.columns else 'Code'
        code = str(row.iloc[0][code_col])
        if code.isdigit():
            code = code.zfill(6)
        market = row.iloc[0].get('Market', 'KRX') if 'Market' in row.columns else 'ETF'
        if "KOSDAQ GLOBAL" in market:
            market = "KOSDAQ"
        return {"code": code, "market": market}

    def _resolve_kr_codes(self) -> dict:
        """Step 1: name -> {"code", "market"[, "name"]} for the KR positions (given ticker or listing match)."""
        from data_fetcher import _get_listing_with_norm, is_us_market

        # ---Build name - KRX code mapping ---
        name_to_info: dict = {}
        self.status_message.emit(tr("종목 코드 찾는 중...", "Looking up ticker codes..."))
        try:
            # Use cached listings with pre-computed NameNorm (no extra .copy() or regex per call)
            df_krx = _get_listing_with_norm('KRX-DESC')
            df_etf = _get_listing_with_norm('ETF/KR')

            for i, name in enumerate(self._names):
                if not name:
                    continue
                if name in name_to_info:
                    continue

                ticker = self._tickers[i]
                market = self._markets[i]

                if ticker and market and not is_us_market(market):
                    name_to_info[name] = {"code": ticker, "market": market, "name": name}
                    continue

                if is_us_market(market):
                    continue

                info = self._match_listing(name, df_krx, df_etf)
                if info is not None:
                    name_to_info[name] = info
        except Exception as e:
            # boundary: each step of this thread runs even when an earlier one failed.
            logger.warning("[PositionPriceFetch] KRX/ETF listing error: %s", e, exc_info=True)
        return name_to_info

    def _fetch_kr_prices(self, name_to_info: dict) -> dict:
        """Step 2: real-time KRX prices via Naver, keyed by code ({} on failure)."""
        from data_fetcher import fetch_naver_realtime_prices

        # ---Fetch real-time prices via Naver (KRX) ---
        unique_codes = list({v["code"] for v in name_to_info.values()})
        price_map: dict = {}
        if unique_codes:
            self.status_message.emit(tr(f"국내 시세 받는 중... (Naver, {len(unique_codes)}종목)",
                                         f"Fetching KR quotes... (Naver, {len(unique_codes)} tickers)"))
            try:
                price_map = fetch_naver_realtime_prices(unique_codes)
            except Exception as e:
                # boundary: each step of this thread runs even when an earlier one failed.
                logger.warning("[PositionPriceFetch] Naver price error: %s", e, exc_info=True)
        return price_map

    def _us_quote_targets(self, name_to_info: dict) -> tuple[list, list]:
        """Names left unresolved by step 1 (once each) and the Yahoo symbols to quote them by."""
        us_names = []
        yq_safe  = []
        seen_us: set = set()
        for i, name in enumerate(self._names):
            if not name:
                continue
            if name not in name_to_info and name not in seen_us:
                seen_us.add(name)
                us_names.append(name)
                t = self._tickers[i]
                yq_safe.append(t.upper() if t else name.upper())
        return us_names, yq_safe

    def _fetch_us_prices(self, name_to_info: dict) -> tuple[dict, dict]:
        """Step 3: Yahoo quotes for the rest; fills name_to_info and returns (KRW, USD) price maps by name."""
        from data_fetcher import get_usd_krw_rate

        # ---Fetch real-time prices via YahooQuery (US/Other) ---
        us_names, yq_safe = self._us_quote_targets(name_to_info)

        us_price_map: dict = {}
        us_price_map_usd: dict = {}
        if us_names:
            self.status_message.emit(tr("Yahoo Finance 응답 기다리는 중...", "Waiting for Yahoo Finance response..."))
            try:
                from data_fetcher import yf_quote_batch
                fx_rate = get_usd_krw_rate()
                prices = yf_quote_batch(yq_safe, timeout=15, chunk_size=30)

                # We always resolve the name to info, even if closed!
                for name, safe_name in zip(us_names, yq_safe):
                    data = prices.get(safe_name)
                    if isinstance(data, dict):
                        # fallback: just assign US market
                        resolved_name = data.get("longName") or data.get("shortName")
                        name_to_info[name] = {"code": safe_name, "market": "US", "name": resolved_name}
                        if 'regularMarketPrice' in data:
                            raw_usd_price = data['regularMarketPrice']
                            us_price_map_usd[name] = raw_usd_price
                            us_price_map[name] = raw_usd_price * fx_rate
            except Exception as e:
                # boundary: each step of this thread runs even when an earlier one failed.
                logger.warning("[PositionPriceFetch] US price error: %s", e, exc_info=True)
        return us_price_map, us_price_map_usd

    @staticmethod
    def _empty_row(i: int) -> dict:
        """The result row for a position without a name."""
        return {
            "index":       i,
            "ticker":      "",
            "market":      "",
            "name":        "",
            "curr_price":  0.0,
            "curr_pl":     0.0,
            "curr_pl_pct": 0.0,
            "wk1":         0.0,
            "wk2":         0.0,
            "mth1":        0.0,
        }

    def _compute_pl(self, i, name, name_to_info, price_map, us_price_map, us_price_map_usd) -> dict:
        """Step 4: one position's result row (price, P/L, 1w/2w/1m changes)."""
        from data_fetcher import is_us_market

        # ---Calculate P/L per position ---
        if not name:
            return self._empty_row(i)

        info = name_to_info.get(name)
        if info:
            code = info["code"]
            market = info["market"]
            resolved_name = info.get("name")
        else:
            code = ""
            market = ""
            resolved_name = None

        curr_price = 0.0
        curr_pl = 0.0
        curr_pl_pct = 0.0

        is_us = is_us_market(market)

        # ---Current price (KRW for display) ---
        if info and not is_us:
            curr_price = price_map.get(code, 0.0)
        elif info and is_us:
            curr_price = us_price_map.get(name, 0.0)

        # ---Raw USD price for historical comparison ---
        # fetch_historical_changes uses the ticker's historical Close (USD)
        # so we must pass the USD price, not the KRW-converted one.
        if is_us:
            curr_price_for_hist = us_price_map_usd.get(name, 0.0)
        else:
            curr_price_for_hist = curr_price

        # ---P/L (open positions only) ---
        if self._is_open[i]:
            qty     = self._qtys[i]
            buy_amt = self._buy_amts[i]

            if curr_price > 0 and qty > 0:
                eval_val    = curr_price * qty
                curr_pl     = eval_val - buy_amt
                curr_pl_pct = (curr_pl / buy_amt * 100) if buy_amt else 0.0

        wk1, wk2, mth1 = self._trend_changes(i, name, code, curr_price_for_hist)

        return {
            "index":       i,
            "ticker":      code,
            "market":      market,
            "name":        resolved_name,
            "curr_price":  curr_price,
            "curr_pl":     curr_pl,
            "curr_pl_pct": curr_pl_pct,
            "wk1":         wk1,
            "wk2":         wk2,
            "mth1":        mth1,
        }

    def _trend_changes(self, i: int, name: str, code: str, curr_price_for_hist: float) -> tuple:
        """(wk1, wk2, mth1) % changes for position i; zeros when skipped, priceless or failed."""
        from data_fetcher import fetch_historical_changes

        wk1 = 0.0
        wk2 = 0.0
        mth1 = 0.0
        # ---Past % changes (open AND closed, USD-safe) ---
        if curr_price_for_hist > 0 and not self._skip_fetch[i]:
            try:
                fetch_code = code if code else name
                changes = fetch_historical_changes(fetch_code, curr_price_for_hist)
                wk1  = changes.get("5d",  0.0)
                wk2  = changes.get("10d", 0.0)
                mth1 = changes.get("20d", 0.0)
            except Exception as exc:
                # boundary: one position's trend failure must not drop the other rows.
                logger.warning("Error fetching trend for %s: %s", name, exc, exc_info=True)
        return wk1, wk2, mth1


# ---------------------------------------------------------------------------
# Account deposit fetch thread (Trading History tab "Fetch" button)
# ---------------------------------------------------------------------------
class KisAccountThread(QThread):
    """Background thread for Trading History's KIS 조회 (docs/ui.md 8.28): the
    account balance (deposit + holdings) and the executed orders since `start`
    (a date), plus the market of every traded ticker from the KRX listing.

    finished(result, error): error is "" when the balance came back; result is
    {"deposit", "holdings", "holdings_complete", "executions", "costs", "markets", "exec_error", "auto"}
    -- a failed execution or trade-cost query leaves the deposit usable and says
    why in exec_error; `costs` are the fees and taxes KIS charged
    (data.collectors.kis.fetch_kis_trade_costs); `auto` echoes the constructor
    flag (the tab's unattended sync, docs/ui.md 8.28).
    (Replaces AccountDepositThread, which fetched the deposit alone; that REST call
    used to run on the UI thread, roadmap 6-1c.)"""
    finished = pyqtSignal(object, str)

    def __init__(self, start, parent=None, auto: bool = False):
        super().__init__(parent)
        self._start = start
        self._auto = auto

    def run(self):
        from data_fetcher import fetch_account_balance, fetch_kis_executions, fetch_kis_trade_costs
        try:
            bal = fetch_account_balance()
        except Exception as e:
            # boundary: the thread must always emit finished; the error text is shown to the user.
            logger.warning("KIS account balance fetch failed", exc_info=True)
            self.finished.emit({"auto": self._auto}, str(e))
            return
        result = {"deposit": float(bal["deposit"]), "holdings": bal["holdings"],
                  "holdings_complete": bal.get("holdings_complete", True), "executions": [],
                  "costs": [], "markets": {}, "exec_error": "", "auto": self._auto}
        try:
            result["executions"] = fetch_kis_executions(self._start)
            if result["executions"]:
                result["costs"] = fetch_kis_trade_costs(self._start)
        except Exception as e:
            # boundary: the deposit is still worth showing when the execution query fails.
            logger.warning("KIS executions fetch failed", exc_info=True)
            result["exec_error"] = str(e)
        tickers = {e["ticker"] for e in result["executions"]}
        if tickers:
            result["markets"] = self._markets_of(tickers)
        self.finished.emit(result, "")

    @staticmethod
    def _markets_of(tickers: set) -> dict:
        """ticker -> KOSPI / KOSDAQ / ETF from the cached listings ({} for unknown ones)."""
        from data_fetcher import _get_listing_with_norm
        out: dict = {}
        try:
            for listing, default in (("KRX-DESC", "KRX"), ("ETF/KR", "ETF")):
                df = _get_listing_with_norm(listing)
                code_col = "Symbol" if "Symbol" in df.columns else "Code"
                codes = df[code_col].astype(str).str.zfill(6)
                for t in tickers - set(out):
                    row = df[codes == t]
                    if row.empty:
                        continue
                    market = str(row.iloc[0].get("Market", default)) if "Market" in row.columns else default
                    out[t] = "KOSDAQ" if "KOSDAQ" in market else market
        except Exception:
            # kept broad: a listing failure only leaves the market blank (the price fetch resolves it later).
            logger.warning("KIS executions: market lookup failed", exc_info=True)
        return out


# ---------------------------------------------------------------------------
# Auto-backup thread (runs on app start)
# ---------------------------------------------------------------------------
class AutoBackupThread(QThread):
    """Copies portfolio.db (trades + asset snapshots) and custom_settings.json
    into archive/auto_<timestamp>/ on every app start, then prunes old
    automatic backups beyond _AUTO_BACKUP_MAX_KEEP.

    Runs off the UI thread so startup is never blocked by disk I/O. This does not
    replace the manual archive/backup_<timestamp>/ convention used before editing
    main.py/data_fetcher.py/trade_db.py — it's a safety net for normal runtime use.
    """
    backup_done = pyqtSignal(str)  # short status message for the status bar

    def run(self):
        try:
            timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest_dir = os.path.join(ARCHIVE_DIR, f"auto_{timestamp}")

            existing = [f for f in _AUTO_BACKUP_FILES if os.path.exists(f)]
            if not existing:
                return  # nothing to back up yet (e.g. very first run)

            os.makedirs(dest_dir, exist_ok=True)
            for fpath in existing:
                dest = os.path.join(dest_dir, os.path.basename(fpath))
                if fpath == DB_FILE:
                    # SQLite's own backup API rather than checkpoint + file
                    # copy: it includes committed WAL frames and holds the
                    # locks itself, so a write landing mid-copy cannot leave a
                    # torn or stale snapshot.
                    import trade_db
                    trade_db.backup_to(dest)
                else:
                    shutil.copy2(fpath, dest)

            self._prune_old_backups()
            rel_dest = os.path.relpath(dest_dir, BASE_DIR)
            logger.info("Auto-backup completed: %s (%s)", rel_dest,
                        ", ".join(os.path.basename(f) for f in existing))
            self.backup_done.emit(tr(f"자동 백업 완료: {rel_dest}", f"Auto-backup complete: {rel_dest}"))
        except Exception:
            # boundary: a failed backup must never affect startup; logged with traceback.
            logger.warning("Auto-backup failed", exc_info=True)

    @staticmethod
    def _prune_old_backups():
        """Keep only the _AUTO_BACKUP_MAX_KEEP most recent archive/auto_* folders."""
        archive_dir = ARCHIVE_DIR
        if not os.path.isdir(archive_dir):
            return
        auto_dirs = sorted(
            d for d in os.listdir(archive_dir)
            if d.startswith("auto_") and os.path.isdir(os.path.join(archive_dir, d))
        )
        excess = len(auto_dirs) - _AUTO_BACKUP_MAX_KEEP
        for old_dir in auto_dirs[:max(excess, 0)]:
            try:
                shutil.rmtree(os.path.join(archive_dir, old_dir))
            except OSError:
                logger.warning("Failed to remove old auto-backup dir=%s", old_dir, exc_info=True)


# ---------------------------------------------------------------------------
# Gemini AI Background Threads
# ---------------------------------------------------------------------------
class GeminiStockReportThread(QThread):
    """Background thread for the per-stock AI report (roadmap 2-1, review.md 2-1)."""
    finished = pyqtSignal(str, str, str, str)  # ticker, name, result_text, error_message ("" on success)

    def __init__(self, item: dict):
        super().__init__()
        self.item = item

    def run(self):
        ticker = self.item.get("ticker", "")
        name = self.item.get("name", ticker)
        try:
            import gemini_helper
            result_text = gemini_helper.stock_report_summary(self.item)
            self.finished.emit(ticker, name, result_text, "")
        except Exception as e:
            # boundary: the thread must always emit finished; the Gemini SDK raises arbitrary types.
            logger.warning("Gemini stock report failed for ticker=%s", ticker, exc_info=True)
            self.finished.emit(ticker, name, "", str(e))
