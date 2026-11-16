"""strategy/common/inputs.py — the current-signal inputs any strategy can ask
for (2026-11-16): daily OHLCV of a set of Universe rows on one market
index's calendar, plus investor flows when asked.

A ``SignalProvider`` (strategy.signal_api) loads what its variants need
through ``load_daily_inputs``; a strategy needing something else (financials,
another calendar) adds a loader here so the screens never fetch strategy data
themselves. The lookback is ``data.cache.start_date()`` -- the key the
Universe refresh used for its own history fetch -- so right after a refresh
every frame comes from ``_HIST_CACHE`` without a network call.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import polars as pl

from data.cache import start_date
from data.history import get_historical_data
from i18n import tr
from strategy.common.prices import PriceBook, align_frame, check_stop, flow_arrays, say, to_date

logger = logging.getLogger(__name__)

# Calendar / regime index per Universe market (the market's own index).
MARKET_INDEX = {"KOSPI": "KS11", "KOSDAQ": "KS11", "NASDAQ 100": "^NDX", "S&P 500": "^GSPC"}


@dataclass
class DailyInputs:
    book: PriceBook
    index_close: np.ndarray | None      # aligned closes of `index_ticker` (None: unavailable)
    index_ticker: str
    flow_fi: np.ndarray | None = None   # T x N KRW foreigner + institution net buys (KR names)


def _items(items: list[dict]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    tickers, names, markets = [], {}, {}
    for it in items:
        tk = str(it.get("ticker", "")).strip()
        if not tk or tk in names:
            continue
        tickers.append(tk)
        names[tk] = str(it.get("name", tk) or tk)
        markets[tk] = str(it.get("market", "") or "")
    return tickers, names, markets


def load_daily_inputs(items: list[dict], index_ticker: str = "KS11", need_flows: bool = False,
                      progress=None, should_stop=None, max_workers: int = 8,
                      start: str | None = None) -> DailyInputs:
    """Daily OHLCV for the rows `items` (dicts with ticker / name / market) on
    `index_ticker`'s calendar (the union of the stock dates when the index
    history is unavailable), plus KRW flows when `need_flows`."""
    start = start or start_date()
    tickers, names, markets = _items(items)
    if not tickers:
        raise ValueError("No tickers to score")

    check_stop(should_stop)
    say(progress, tr(f"{index_ticker} 지수 이력 불러오는 중...", f"Loading {index_ticker} index history..."))
    try:
        idx_df = get_historical_data(index_ticker, start)
    except Exception:
        # kept broad: the history router reaches several third-party libraries.
        logger.warning("%s index history failed; using the union calendar", index_ticker, exc_info=True)
        idx_df = None

    say(progress, tr(f"{len(tickers)}종목 일봉 불러오는 중...", f"Loading daily history for {len(tickers)} names..."))
    frames: dict[str, pl.DataFrame] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {exe.submit(get_historical_data, tk, start): tk for tk in tickers}
        for fut in as_completed(futures):
            check_stop(should_stop, futures)
            tk = futures[fut]
            done += 1
            try:
                df = fut.result()
            except Exception:
                # boundary: one name failing leaves it out, not the run.
                logger.warning("history fetch failed for %s", tk, exc_info=True)
                df = None
            if df is not None and not df.is_empty() and "Close" in df.columns:
                frames[tk] = df
            if done % 25 == 0 or done == len(tickers):
                say(progress, tr(f"일봉 {done}/{len(tickers)} (사용 가능 {len(frames)})",
                                 f"Daily history {done}/{len(tickers)} ({len(frames)} usable)"))
    if not frames:
        raise RuntimeError("No usable stock histories were loaded")
    frames = {tk: frames[tk] for tk in tickers if tk in frames}   # keep the Universe order

    if idx_df is not None and not idx_df.is_empty() and "Date" in idx_df.columns:
        calendar = sorted({to_date(d) for d in idx_df.get_column("Date").to_list()})
        index_close = align_frame(idx_df, calendar, ["Close"])["Close"]
    else:
        calendar = sorted({to_date(d) for df in frames.values() for d in df.get_column("Date").to_list()})
        index_close = None
    book = PriceBook.from_frames(frames, calendar, markets, names)
    flow_fi = load_flow_fi(book, progress, should_stop) if need_flows else None
    return DailyInputs(book, index_close, index_ticker, flow_fi)


def load_flow_fi(book: PriceBook, progress=None, should_stop=None) -> np.ndarray | None:
    """KRW foreigner + institution net buys for every name of `book`
    (Naver investor pages; KR codes only, others stay NaN)."""
    from data.cache import is_kr_code
    from data.flows import FLOW_FETCH_CONCURRENCY, get_investor_flows
    tickers = [tk for tk in book.tickers if is_kr_code(tk)]
    frames = {}
    done = 0
    start = start_date()
    with ThreadPoolExecutor(max_workers=FLOW_FETCH_CONCURRENCY) as exe:
        futures = {exe.submit(get_investor_flows, tk, start): tk for tk in tickers}
        for fut in as_completed(futures):
            check_stop(should_stop, futures)
            tk = futures[fut]
            done += 1
            try:
                df = fut.result()
            except Exception:
                # boundary: one name's flows failing leaves it without flows, not the run.
                logger.warning("investor flows failed for %s", tk, exc_info=True)
                df = None
            if df is not None and not df.is_empty():
                frames[tk] = df
            if done % 25 == 0 or done == len(tickers):
                say(progress, tr(f"투자자 수급 {done}/{len(tickers)}", f"Investor flows {done}/{len(tickers)}"))
    flow_fi, _retail = flow_arrays(book, list(book.dates), frames)
    return flow_fi
