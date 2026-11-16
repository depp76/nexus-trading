"""strategy/trend_following/dataset.py — Aligned date x ticker arrays for the
backtest (trend_following.md 5 "데이터와 look-ahead", 6-3 benchmark data).

``build_dataset`` is pure (frames in, arrays out) so tests can feed synthetic
histories; ``load_dataset`` is the network path that pulls everything through
the ``data`` layer: KOSPI index and per-stock OHLCV from ``data.history``, the
current KOSPI top-N listing from the Naver collector (with the FDR fallback),
investor flows from ``data.flows`` and the CD 91-day rate from ``data.rates``.

Look-ahead notes (spec 5): every signal built from a day's close/volume/flow is
only acted on at the next day's open by the engine; the universe is today's
constituents (survivorship bias -- flagged in ``Dataset.info``); the flow
amount is net quantity x that day's close from the same Naver page.
"""
from __future__ import annotations

from i18n import tr

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl

from data.history import get_historical_data
from data.collectors.naver import _fetch_kr_listing_naver
from data.listing import _fetch_kr_listing_fdr_fallback
from data.flows import get_investor_flows, FLOW_FETCH_CONCURRENCY
from data.rates import get_cd91_series
from data.fx import get_usd_krw_history
from data.collectors.yahoo import get_us_listing_ranked
from strategy.trend_following.config import (
    StrategyParams, BM_TR_TICKER, BM_PRICE_FALLBACK_TICKER, DEV_END,
    MARKET_INDEX, MARKET_BM_ETF, BM_ETF_COST_MARKET, BM_ETF_NAMES, US_MARKETS, normalize_markets,
)

logger = logging.getLogger(__name__)


class HoldoutGuardError(ValueError):
    """A date after the development window end (config.DEV_END) was requested
    or found without the explicit `allow_holdout` flag."""


def check_dev_window(end, allow_holdout: bool = False, what: str = "end date") -> None:
    """Raise HoldoutGuardError when `end` lies after DEV_END and the holdout
    flag is off. Only the offending date is named, never any data."""
    if end is None or allow_holdout:
        return
    if _to_date(end) > DEV_END:
        raise HoldoutGuardError(
            f"{what} {_to_date(end)} is after the development window end {DEV_END}; "
            "the holdout is blocked unless allow_holdout=True is passed explicitly")

# Spec 2-0 limits the universe to Korean *stocks*. The Naver / FDR listing rows carry only
# Code / Name / Marcap, so ETFs and ETNs are told apart by name: the issuer brands below
# (always followed by a space) and the "ETN" token. Infrastructure funds / REITs (e.g.
# 맥쿼리인프라) are listed like stocks and are kept.
ETF_BRANDS = (
    "KODEX", "TIGER", "KBSTAR", "KOSEF", "ARIRANG", "HANARO", "SOL", "ACE", "RISE", "PLUS",
    "TIME", "TIMEFOLIO", "KoAct", "KIWOOM", "WON", "UNICORN", "1Q", "TREX", "FOCUS", "MASTER",
    "마이티", "히어로즈", "파워", "에셋플러스",
)
_ETF_NAME_RE = re.compile(r"^(?:{})(?:\s|$)|(?<![A-Za-z])ETN(?![A-Za-z])".format("|".join(map(re.escape, ETF_BRANDS))),
                          re.IGNORECASE)
# Extra listing rows requested so that dropping ETFs still leaves `universe_size` stocks.
LISTING_POOL_FACTOR = 2


def is_etf_name(name: str) -> bool:
    """True when a listing name is an ETF / ETN (issuer brand prefix or an ETN token)."""
    return bool(_ETF_NAME_RE.search(str(name or "").strip()))


def select_stock_universe(listing: list[dict], size: int) -> tuple[list[dict], list[dict]]:
    """(top `size` non-ETF rows in listing order, the ETF/ETN rows that ranked above the cut)."""
    kept: list[dict] = []
    excluded: list[dict] = []
    for r in listing:
        if is_etf_name(r.get("Name", "")):
            if len(kept) < size:
                excluded.append(r)
        elif len(kept) < size:
            kept.append(r)
    return kept, excluded


def universe_impact(stocks: PriceBook, start_idx: int, ended_min_missing: int = 10) -> dict:
    """Measurable size of the survivorship / look-ahead exposure of the universe (checklist B1):
    names whose data ends inside the evaluation window (last bar followed by at least
    `ended_min_missing` empty sessions) and names whose first bar lies after the evaluation start."""
    ended, new = [], []
    T = stocks.T
    for j, tk in enumerate(stocks.tickers):
        idx = np.flatnonzero(np.isfinite(stocks.close[:, j]))
        if idx.size == 0:
            continue
        first, last = int(idx[0]), int(idx[-1])
        if last >= start_idx and T - 1 - last >= ended_min_missing:
            ended.append(tk)
        if first > start_idx:
            new.append(tk)
    return {"ended_in_window": ended, "listed_after_start": new}

# Calendar days of history loaded before the evaluation start so the widest
# windows (252-session 52-week high, BM3's MA200) are warm on day one.
WARMUP_CALENDAR_DAYS = 450
_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _say(progress, msg: str) -> None:
    if progress is not None:
        try:
            progress(msg)
        except Exception:
            logger.debug("progress callback failed", exc_info=True)


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def align_frame(df: pl.DataFrame | None, calendar: list[date], cols: list[str]) -> dict[str, np.ndarray]:
    """Reindex a polars frame with a Date column onto `calendar`; missing
    dates become NaN. Returns one float array per requested column."""
    if df is None or df.is_empty() or "Date" not in df.columns:
        return {c: np.full(len(calendar), np.nan) for c in cols}
    present = [c for c in cols if c in df.columns]
    pdf = df.select(["Date"] + present).to_pandas()
    pdf["Date"] = pd.to_datetime(pdf["Date"]).dt.date
    pdf = pdf.drop_duplicates("Date", keep="last").set_index("Date").reindex(calendar)
    out = {}
    for c in cols:
        if c in present:
            out[c] = pd.to_numeric(pdf[c], errors="coerce").to_numpy(dtype=float)
        else:
            out[c] = np.full(len(calendar), np.nan)
    return out


@dataclass
class PriceBook:
    """OHLCV for a set of tickers on one trading calendar (T x N float arrays,
    NaN where a name has no bar)."""
    dates: list[date]
    tickers: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    markets: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self._col = {t: i for i, t in enumerate(self.tickers)}

    @property
    def T(self) -> int:
        return len(self.dates)

    @property
    def N(self) -> int:
        return len(self.tickers)

    def col(self, ticker: str) -> int:
        return self._col[ticker]

    def market_of(self, ticker: str) -> str:
        return self.markets.get(ticker, "KOSPI")

    @classmethod
    def from_frames(cls, frames: dict[str, pl.DataFrame], calendar: list[date],
                    markets: dict[str, str] | None = None, names: dict[str, str] | None = None) -> PriceBook:
        tickers = list(frames)
        T, N = len(calendar), len(tickers)
        arrays = {c: np.full((T, N), np.nan) for c in _OHLCV}
        for j, tk in enumerate(tickers):
            cols = align_frame(frames[tk], calendar, _OHLCV)
            for c in _OHLCV:
                arrays[c][:, j] = cols[c]
        return cls(
            dates=list(calendar), tickers=tickers,
            open=arrays["Open"], high=arrays["High"], low=arrays["Low"],
            close=arrays["Close"], volume=arrays["Volume"],
            markets=dict(markets or {}), names=dict(names or {}),
        )


@dataclass
class Dataset:
    """Everything one research run needs, on one calendar.

    ``start_idx`` is the first calendar index inside the evaluation window;
    rows before it are warm-up history for the rolling windows. ``rf_period``
    is the simple risk-free return earned over the calendar gap ending at each
    row (annual rate x gap days / 365; 0 on the first row)."""
    stocks: PriceBook
    index_close: np.ndarray
    bm: PriceBook | None
    bm_is_total_return: bool
    rf_annual: np.ndarray
    rf_period: np.ndarray
    rf_source: str
    flow_fi: np.ndarray | None
    flow_retail: np.ndarray | None
    start_idx: int
    info: dict = field(default_factory=dict)
    # Spec 9-25: with several markets, each market's index on the calendar (forward-filled
    # over its own holidays). None = one market, every name judged on `index_close`.
    index_by_market: dict[str, np.ndarray] | None = None

    @property
    def dates(self) -> list[date]:
        return self.stocks.dates

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(self.info.get("markets") or ("KOSPI",))

    def index_cols(self) -> np.ndarray:
        """T x N: the index each name is measured against (its own market's
        index with several markets, `index_close` otherwise)."""
        base = np.asarray(self.index_close, dtype=float)
        n = self.stocks.N
        if not self.index_by_market:
            return np.repeat(base[:, None], n, axis=1)
        out = np.empty((len(base), n))
        for j, tk in enumerate(self.stocks.tickers):
            out[:, j] = self.index_by_market.get(self.stocks.market_of(tk), base)
        return out

    @property
    def eval_dates(self) -> list[date]:
        return self.stocks.dates[self.start_idx:]


def _rf_period_from_annual(dates: list[date], rf_annual: np.ndarray) -> np.ndarray:
    out = np.zeros(len(dates))
    for t in range(1, len(dates)):
        gap = (dates[t] - dates[t - 1]).days
        out[t] = rf_annual[t] * gap / 365.0
    return out


def build_dataset(calendar: list[date], index_close: np.ndarray, stock_frames: dict[str, pl.DataFrame],
                  start: date, markets: dict[str, str] | None = None, names: dict[str, str] | None = None,
                  bm_frame: pl.DataFrame | None = None, bm_ticker: str = BM_TR_TICKER,
                  bm_is_total_return: bool = True, flow_frames: dict[str, pl.DataFrame] | None = None,
                  rf_frame: pl.DataFrame | None = None, rf_fallback: float = 0.03,
                  rf_source: str = "", info: dict | None = None, cutoff: date | None = None,
                  market_index_frames: dict[str, pl.DataFrame] | None = None,
                  fx_frame: pl.DataFrame | None = None,
                  bm_frames: dict[str, pl.DataFrame] | None = None,
                  bm_markets: dict[str, str] | None = None) -> Dataset:
    """Assemble a Dataset from already-fetched frames (no network). `cutoff`
    drops every calendar row after that date (rows of the frames beyond the
    calendar are ignored by the reindex).

    Several markets (spec 9-25): `market_index_frames` (market -> its index
    history) marks which calendar days each market traded; a name's row on a
    day its market was shut repeats the last close with zero volume, so the
    rolling windows run on and the engine cannot fill there. US prices
    (markets in US_MARKETS) are converted to KRW with `fx_frame` (USD/KRW
    close, forward-filled). `bm_frames` / `bm_markets` hold one ETF per
    market for the BM1/BM3 mix; `index_close` may be None and is then the
    first market's index."""
    calendar, index_close = _apply_cutoff(calendar, index_close, cutoff)
    stocks = PriceBook.from_frames(stock_frames, calendar, markets, names)
    index_close, index_by_market, open_days = _align_market_indexes(stocks, calendar, index_close,
                                                                    market_index_frames)
    bm, bm_ticker = _benchmark_book(calendar, bm_frame, bm_ticker, bm_frames, bm_markets, open_days)
    fx_source = _convert_us_to_krw(calendar, (stocks, bm), fx_frame)
    flow_fi, flow_retail = _flow_arrays(stocks, calendar, flow_frames)
    rate, rf_source = _risk_free_rates(calendar, rf_frame, rf_fallback, rf_source)
    rf_period = _rf_period_from_annual(calendar, rate)
    start_idx = _eval_start_idx(calendar, start)
    bm_tr = bool(bm_is_total_return and bm is not None)
    meta = _dataset_meta(stocks, calendar, start_idx, bm_ticker if bm is not None else "", bm_tr,
                         rf_source, flow_fi is not None, fx_source, info)
    return Dataset(
        stocks=stocks, index_close=index_close, bm=bm, bm_is_total_return=bm_tr,
        rf_annual=rate, rf_period=rf_period, rf_source=rf_source,
        flow_fi=flow_fi, flow_retail=flow_retail, start_idx=start_idx, info=meta, index_by_market=index_by_market,
    )


def _apply_cutoff(calendar, index_close, cutoff: date | None) -> tuple[list[date], np.ndarray | None]:
    """The calendar as dates, without the rows after `cutoff` (spec 5 development-window guard)."""
    calendar = [_to_date(d) for d in calendar]
    if cutoff is not None:
        keep = [i for i, d in enumerate(calendar) if d <= cutoff]
        calendar = [calendar[i] for i in keep]
        if index_close is not None:
            index_close = np.asarray(index_close, dtype=float)[keep]
    return calendar, index_close


def _align_market_indexes(stocks: PriceBook, calendar: list[date], index_close,
                          market_index_frames: dict[str, pl.DataFrame] | None
                          ) -> tuple[np.ndarray, dict[str, np.ndarray] | None, dict[str, np.ndarray]]:
    """(index_close, index_by_market, open_days), spec 9-25: each market's index on the
    calendar (forward/back-filled) and the days it traded; the closed days of `stocks`
    are filled in place. One market (no frames) keeps `index_close` as given."""
    index_by_market = None
    open_days: dict[str, np.ndarray] = {}
    if market_index_frames:
        index_by_market = {}
        for m, f in market_index_frames.items():
            arr = align_frame(f, calendar, ["Close"])["Close"]
            open_days[m] = np.isfinite(arr)
            index_by_market[m] = pd.Series(arr).ffill().bfill().to_numpy(dtype=float)
        if index_close is None:
            index_close = next(iter(index_by_market.values()))
        _fill_closed_days(stocks, open_days)
    return np.asarray(index_close, dtype=float), index_by_market, open_days


def _benchmark_book(calendar: list[date], bm_frame: pl.DataFrame | None, bm_ticker: str,
                    bm_frames: dict[str, pl.DataFrame] | None, bm_markets: dict[str, str] | None,
                    open_days: dict[str, np.ndarray]) -> tuple[PriceBook | None, str]:
    """(BM ETF PriceBook or None, its ticker label): the per-market ETF mix (spec 9-25,
    tickers joined by "+") when `bm_frames` is given, else the single BM1 ETF (spec 6-3)."""
    bm = None
    if bm_frames:
        frames = {tk: f for tk, f in bm_frames.items() if f is not None and not f.is_empty()}
        if frames:
            bm_mk = {tk: (bm_markets or {}).get(tk, "ETF") for tk in frames}
            bm = PriceBook.from_frames(frames, calendar, bm_mk, {tk: BM_ETF_NAMES.get(tk, tk) for tk in frames})
            if open_days:
                _fill_closed_days(bm, open_days)
            bm_ticker = "+".join(frames)
    elif bm_frame is not None and not bm_frame.is_empty():
        bm = PriceBook.from_frames({bm_ticker: bm_frame}, calendar, {bm_ticker: "ETF"}, {bm_ticker: bm_ticker})
    return bm, bm_ticker


def _convert_us_to_krw(calendar: list[date], books, fx_frame: pl.DataFrame | None) -> str:
    """Convert the US names of `books` to KRW in place (spec 9-25); returns the fx
    source label ("" when no book holds a US name)."""
    us_books = [bk for bk in books if bk is not None and any(m in US_MARKETS for m in bk.markets.values())]
    if not us_books:
        return ""
    fx = align_frame(fx_frame, calendar, ["Close"])["Close"]
    fx = pd.Series(fx).ffill().bfill().to_numpy(dtype=float)
    if not np.isfinite(fx).any():
        raise RuntimeError("USD/KRW history unavailable: US prices cannot be converted to KRW")
    for bk in us_books:
        _to_krw(bk, fx)
    return "USD/KRW daily close (data.fx), forward-filled"


def _flow_arrays(stocks: PriceBook, calendar: list[date], flow_frames: dict[str, pl.DataFrame] | None
                 ) -> tuple[np.ndarray | None, np.ndarray | None]:
    """(flow_fi, flow_retail): T x N KRW flow amounts for the spec 2-3 F filter, or (None, None)."""
    if not flow_frames:
        return None, None
    T, N = stocks.T, stocks.N
    flow_fi = np.full((T, N), np.nan)
    flow_retail = np.full((T, N), np.nan)
    for tk, fdf in flow_frames.items():
        if tk not in stocks._col or fdf is None or fdf.is_empty():
            continue
        j = stocks.col(tk)
        cols = align_frame(fdf, calendar, ["Foreigner", "Institution", "Retail", "Close"])
        px = cols["Close"]
        # Amount proxy: net quantity x the same page's close (spec 2-3 normalises
        # by trading value, so both sides are in KRW).
        flow_fi[:, j] = (cols["Foreigner"] + cols["Institution"]) * px
        # Since NEX-23 (Naver mobile trend API) "Retail" is the API's own
        # individual net purchase, not -(Foreigner + Institution): other
        # corporations/the state make the three not sum to zero. Snapshots
        # built before that (<= dev_2021_2024_nex21) had flow_retail == -flow_fi.
        flow_retail[:, j] = cols["Retail"] * px
    return flow_fi, flow_retail


def _risk_free_rates(calendar: list[date], rf_frame: pl.DataFrame | None, rf_fallback: float,
                     rf_source: str) -> tuple[np.ndarray, str]:
    """(annual risk-free rate per row, source label) for BM4 (spec 6-3): the rate series
    filled with `rf_fallback` where missing, or that constant when there is no series."""
    if rf_frame is not None and not rf_frame.is_empty() and "Rate" in rf_frame.columns:
        rate = align_frame(rf_frame, calendar, ["Rate"])["Rate"]
        rate = pd.Series(rate).ffill().bfill().to_numpy(dtype=float) / 100.0
        rate = np.where(np.isfinite(rate), rate, rf_fallback)
        return rate, rf_source or "series"
    return np.full(len(calendar), float(rf_fallback)), rf_source or f"constant {rf_fallback:.2%}"


def _eval_start_idx(calendar: list[date], start) -> int:
    """First calendar index on or after `start`; ValueError when there is none."""
    start = _to_date(start)
    start_idx = next((i for i, d in enumerate(calendar) if d >= start), len(calendar))
    if start_idx >= len(calendar):
        raise ValueError(f"No trading days on or after {start} in the loaded calendar")
    return start_idx


def _dataset_meta(stocks: PriceBook, calendar: list[date], start_idx: int, bm_ticker: str, bm_tr: bool,
                  rf_source: str, has_flows: bool, fx_source: str, info: dict | None) -> dict:
    """`Dataset.info`: provenance, the survivorship note (spec 5) and the universe
    impact (checklist B1), overridden by the caller's `info`."""
    selected = tuple((info or {}).get("markets") or ("KOSPI",))
    meta = {
        "n_tickers": stocks.N,
        "calendar_start": calendar[0].isoformat() if calendar else "",
        "eval_start": calendar[start_idx].isoformat(),
        "eval_end": calendar[-1].isoformat() if calendar else "",
        "bm_ticker": bm_ticker,
        "bm_is_total_return": bm_tr,
        "rf_source": rf_source,
        "has_flows": has_flows,
        "universe": f"current {'+'.join(selected)} stocks by market cap"
                    f"{' (KRW-converted, ranked together)' if len(selected) > 1 else ''}, ETF/ETN excluded "
                    "(survivorship bias: no point-in-time list)",
        "markets": list(selected),
        "fx_source": fx_source,
        **universe_impact(stocks, start_idx),
    }
    meta.update(info or {})
    return meta


def _kr_days(open_days: dict[str, np.ndarray]) -> np.ndarray | None:
    for m in ("KOSPI", "KOSDAQ"):
        if m in open_days:
            return open_days[m]
    return None


def _fill_closed_days(book: PriceBook, open_days: dict[str, np.ndarray]) -> None:
    """On a calendar day the name's own market was shut, repeat its last close
    as O/H/L/C with zero volume (in place). Korean ETFs follow the KRX days."""
    for j, tk in enumerate(book.tickers):
        m = book.market_of(tk)
        days = open_days.get(m) if m in open_days else (_kr_days(open_days) if m not in US_MARKETS else None)
        if days is None:
            continue
        closed = ~days
        if not closed.any():
            continue
        last = pd.Series(np.where(closed, np.nan, book.close[:, j])).ffill().to_numpy(dtype=float)
        rows = closed & np.isfinite(last)
        for arr in (book.open, book.high, book.low, book.close):
            arr[rows, j] = last[rows]
        book.volume[rows, j] = 0.0


def _to_krw(book: PriceBook, fx: np.ndarray) -> None:
    """Multiply the OHLC of every US name by the day's USD/KRW (in place)."""
    for j, tk in enumerate(book.tickers):
        if book.market_of(tk) in US_MARKETS:
            for arr in (book.open, book.high, book.low, book.close):
                arr[:, j] = arr[:, j] * fx


def _cut_frame(df: pl.DataFrame | None, end: date) -> pl.DataFrame | None:
    """Drop rows dated after `end` (the development-window guard at load time)."""
    if df is None or df.is_empty() or "Date" not in df.columns:
        return df
    return df.filter(pl.col("Date").cast(pl.Date) <= end)


def _load_listing(universe_size: int) -> list[dict]:
    try:
        rows = _fetch_kr_listing_naver("KOSPI", universe_size)
    except Exception:
        logger.warning("Naver KOSPI listing failed, trying FDR fallback", exc_info=True)
        rows = []
    if not rows:
        rows = _fetch_kr_listing_fdr_fallback("KOSPI", universe_size)
    return rows or []


class ResearchCancelled(RuntimeError):
    """Raised by load_dataset / run_research when the caller's `should_stop`
    callable returns True (the Stop button of the Trend Following tab)."""


def _check_stop(should_stop, futures=None) -> None:
    if should_stop is not None and should_stop():
        for f in futures or ():
            f.cancel()      # queued fetches never start; in-flight ones finish
        raise ResearchCancelled("cancelled")


def load_dataset(start, end=None, universe_size: int = 200, include_flows: bool = False,
                 params: StrategyParams | None = None, progress=None, max_workers: int = 8,
                 min_rows: int = 60, should_stop=None, allow_holdout: bool = False,
                 tickers: list[str] | None = None, ticker_names: dict[str, str] | None = None,
                 fixed_source: str = "", excluded_etf: list[dict] | None = None,
                 markets=("KOSPI",)) -> Dataset:
    """Network path: KOSPI calendar + top-N KOSPI names + optional flows + BM
    ETF + CD91, warmed up WARMUP_CALENDAR_DAYS before `start`. `should_stop`
    (no-arg callable) is polled between fetches; True raises ResearchCancelled.

    Development-window guard: `end` defaults to DEV_END (not today) and a later
    one raises HoldoutGuardError unless `allow_holdout`. Every fetched frame is
    cut at `end` right after the fetch, so rows after it never reach the Dataset.

    Universe: ETFs / ETNs are dropped from the listing (spec 2-0, stocks only) and the top
    `universe_size` stocks are kept. `tickers` pins the list instead (no listing call, no ETF
    filter -- the list is taken as given; `ticker_names` / `excluded_etf` / `fixed_source` are the
    provenance of the manifest it came from) so a snapshot can be rebuilt for the same names."""
    p = params or StrategyParams()
    start = _to_date(start)
    check_dev_window(start, allow_holdout, "start date")
    check_dev_window(end, allow_holdout, "end date")
    end = _to_date(end) if end else (date.today() if allow_holdout else DEV_END)
    warm_start = start - timedelta(days=WARMUP_CALENDAR_DAYS)
    ws = warm_start.isoformat()
    markets = normalize_markets(markets)
    if markets != ("KOSPI",) and not tickers:
        return _load_multi_market(start, end, warm_start, markets, universe_size, include_flows, p, progress,
                                  max_workers, min_rows, should_stop, allow_holdout)

    calendar, index_close = _load_kospi_calendar(warm_start, end, progress, should_stop)
    if tickers:
        codes, names, excluded = _pinned_universe(tickers, ticker_names, excluded_etf, fixed_source, progress)
    else:
        codes, names, excluded = _kospi_universe(universe_size, progress)
    frames = _load_histories(codes, ws, end, max_workers, min_rows, progress, should_stop)
    markets = {c: "KOSPI" for c in frames}
    flow_frames = (_load_flows(list(frames), ws, end, max_workers, progress, should_stop)
                   if include_flows else None)
    bm_ticker, bm_tr, bm_df = _load_kospi_benchmark(ws, end, progress, should_stop)
    rf_df, rf_source = _load_cd91(ws, end, progress)

    return build_dataset(
        calendar, index_close, frames, start, markets, names,
        bm_frame=bm_df, bm_ticker=bm_ticker, bm_is_total_return=bm_tr,
        flow_frames=flow_frames, rf_frame=rf_df, rf_fallback=p.risk_free_fallback, rf_source=rf_source,
        info={"universe_size_requested": universe_size, "flows_requested": include_flows,
              "cutoff": end.isoformat(), "holdout_allowed": bool(allow_holdout),
              "excluded_etf": excluded, "universe_fixed_from": fixed_source,
              "universe_built_on": date.today().isoformat()},
        cutoff=end,
    )


def _load_kospi_calendar(warm_start: date, end: date, progress, should_stop) -> tuple[list[date], np.ndarray]:
    """(calendar, KOSPI close on it): the KOSPI trading days from `warm_start` to `end` (spec 5)."""
    _check_stop(should_stop)
    _say(progress, tr("KOSPI 지수 이력 불러오는 중...", "Loading KOSPI index history..."))
    idx_df = _cut_frame(get_historical_data("KS11", warm_start.isoformat()), end)
    if idx_df is None or idx_df.is_empty():
        raise RuntimeError("KOSPI index history unavailable (data.history returned nothing)")
    idx_dates = [_to_date(d) for d in idx_df.get_column("Date").to_list()]
    calendar = [d for d in idx_dates if warm_start <= d <= end]
    if not calendar:
        raise RuntimeError(f"No KOSPI trading days between {warm_start} and {end}")
    return calendar, align_frame(idx_df, calendar, ["Close"])["Close"]


def _pinned_universe(tickers, ticker_names, excluded_etf, fixed_source: str,
                     progress) -> tuple[list[str], dict[str, str], list[dict]]:
    """(codes, names, excluded ETF rows) of a caller-pinned ticker list, taken as given."""
    codes = [str(c).zfill(6) for c in tickers]
    names = {c: str((ticker_names or {}).get(c, "")) for c in codes}
    excluded = list(excluded_etf or [])
    _say(progress, tr(f"고정 목록 {len(codes)}종목 사용 ({fixed_source or '호출자'})", f"Using the fixed list of {len(codes)} names from {fixed_source or 'the caller'}"))
    return codes, names, excluded


def _kospi_universe(universe_size: int, progress) -> tuple[list[str], dict[str, str], list[dict]]:
    """(codes, names, excluded ETF rows): the current KOSPI top `universe_size` stocks by
    market cap, ETFs / ETNs dropped (spec 2-0)."""
    _say(progress, tr(f"KOSPI 상위 {universe_size} 목록 불러오는 중 (ETF/ETN 제외)...", f"Loading KOSPI top {universe_size} listing (ETF/ETN excluded)..."))
    listing = _load_listing(universe_size * LISTING_POOL_FACTOR)
    if not listing:
        raise RuntimeError("KOSPI listing unavailable (Naver and FDR both failed)")
    kept, excluded_rows = select_stock_universe(listing, universe_size)
    excluded = [{"Code": str(r["Code"]).zfill(6), "Name": str(r.get("Name", ""))} for r in excluded_rows]
    codes = [str(r["Code"]).zfill(6) for r in kept]
    names = {str(r["Code"]).zfill(6): str(r.get("Name", "")) for r in kept}
    if len(kept) < universe_size:
        logger.warning("only %d stocks left after the ETF/ETN filter (requested %d)", len(kept), universe_size)
    return codes, names, excluded


def _load_histories(codes: list[str], ws: str, end: date, max_workers: int, min_rows: int,
                    progress, should_stop) -> dict[str, pl.DataFrame]:
    """Daily OHLCV of `codes` cut at `end`, in `codes` order; names with fewer than
    `min_rows` bars or a failed fetch are dropped. RuntimeError when none is left."""
    _check_stop(should_stop)
    _say(progress, tr(f"{len(codes)}종목 일봉 불러오는 중...", f"Loading daily history for {len(codes)} names..."))
    frames: dict[str, pl.DataFrame] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {exe.submit(get_historical_data, c, ws): c for c in codes}
        for fut in as_completed(futures):
            _check_stop(should_stop, futures)
            c = futures[fut]
            done += 1
            try:
                df = _cut_frame(fut.result(), end)
            except Exception:
                logger.warning("history fetch failed for %s", c, exc_info=True)
                df = None
            if df is not None and not df.is_empty() and df.height >= min_rows:
                frames[c] = df
            if done % 25 == 0 or done == len(codes):
                _say(progress, tr(f"일봉 {done}/{len(codes)} (사용 가능 {len(frames)})", f"Daily history {done}/{len(codes)} ({len(frames)} usable)"))
    if not frames:
        raise RuntimeError("No usable stock histories were loaded")
    return {c: frames[c] for c in codes if c in frames}   # keep market-cap order


def _load_flows(codes: list[str], ws: str, end: date, max_workers: int, progress,
                should_stop) -> dict[str, pl.DataFrame]:
    """Investor-flow frames (spec 2-3) of `codes` cut at `end`; failed or empty ones are left out."""
    flow_frames: dict[str, pl.DataFrame] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, FLOW_FETCH_CONCURRENCY))) as exe:
        futures = {exe.submit(get_investor_flows, c, ws): c for c in codes}
        for fut in as_completed(futures):
            _check_stop(should_stop, futures)
            c = futures[fut]
            done += 1
            try:
                fdf = _cut_frame(fut.result(), end)
            except Exception:
                logger.warning("investor flow fetch failed for %s", c, exc_info=True)
                fdf = None
            if fdf is not None and not fdf.is_empty():
                flow_frames[c] = fdf
            if done % 10 == 0 or done == len(codes):
                _say(progress, tr(f"투자자 수급 {done}/{len(codes)} (자료 있음 {len(flow_frames)})", f"Investor flows {done}/{len(codes)} ({len(flow_frames)} with data)"))
    return flow_frames


def _load_kospi_benchmark(ws: str, end: date, progress, should_stop) -> tuple[str, bool, pl.DataFrame | None]:
    """(ticker, is total return, frame) of the BM1 ETF (spec 6-3): KODEX 200TR, else the
    price-return KODEX 200."""
    _check_stop(should_stop)
    _say(progress, tr("벤치마크 ETF 불러오는 중...", "Loading benchmark ETF..."))
    bm_ticker, bm_tr = BM_TR_TICKER, True
    bm_df = _cut_frame(get_historical_data(bm_ticker, ws), end)
    if bm_df is None or bm_df.is_empty():
        bm_ticker, bm_tr = BM_PRICE_FALLBACK_TICKER, False
        bm_df = _cut_frame(get_historical_data(bm_ticker, ws), end)
    return bm_ticker, bm_tr, bm_df


def _load_cd91(ws: str, end: date, progress) -> tuple[pl.DataFrame | None, str]:
    """(CD91 rate frame or None, source label) for BM4 / cash interest (spec 6-3)."""
    _say(progress, tr("CD 91일 금리 불러오는 중...", "Loading CD 91-day rate..."))
    rf_source = ""
    try:
        rf_df = _cut_frame(get_cd91_series(ws, end.isoformat()), end)
        if rf_df is not None and not rf_df.is_empty():
            rf_source = "CD91 (data.rates)"
    except Exception:
        logger.warning("CD91 rate load failed, using the constant fallback", exc_info=True)
        rf_df = None
    return rf_df, rf_source


# ---------------------------------------------------------------------------
# Several markets (spec 9-25)
# ---------------------------------------------------------------------------
def _ranked_listing(market: str, pool: int, usd_krw: float) -> tuple[list[dict], list[dict]]:
    """(stock rows with a KRW `MarcapKRW` and `Market`, excluded ETF rows) of one market, largest first."""
    if market in US_MARKETS:
        rows = get_us_listing_ranked(market, pool)
        kept = [dict(r, Market=market, MarcapKRW=float(r["Marcap"]) * usd_krw) for r in rows]
        return kept, []
    try:
        rows = _fetch_kr_listing_naver(market, pool)
    except Exception:
        logger.warning("Naver %s listing failed, trying FDR fallback", market, exc_info=True)
        rows = []
    if not rows:
        rows = _fetch_kr_listing_fdr_fallback(market, pool) or []
    kept, excluded = select_stock_universe(rows, pool)
    out = [dict(r, Code=str(r["Code"]).zfill(6), Market=market, MarcapKRW=float(r.get("Marcap") or 0)) for r in kept]
    return out, [{"Code": str(r["Code"]).zfill(6), "Name": str(r.get("Name", ""))} for r in excluded]


def _load_multi_market(start, end, warm_start, markets, universe_size, include_flows, p, progress, max_workers,
                       min_rows, should_stop, allow_holdout) -> Dataset:
    ws = warm_start.isoformat()
    index_frames, calendar = _load_market_indexes(markets, warm_start, end, progress, should_stop)
    fx_frame, usd_krw = _load_fx(markets, end, progress)
    codes, names, market_of, excluded = _multi_market_universe(markets, universe_size, usd_krw, progress,
                                                               should_stop)
    frames = _load_histories(codes, ws, end, max_workers, min_rows, progress, should_stop)
    kr_codes = [c for c in frames if market_of[c] not in US_MARKETS]
    flow_frames = (_load_flows(kr_codes, ws, end, max_workers, progress, should_stop)
                   if include_flows and kr_codes else None)
    bm_frames, bm_tr, bm_missing = _load_market_benchmarks(markets, ws, end, progress, should_stop)
    bm_markets = {tk: BM_ETF_COST_MARKET.get(tk, "ETF") for tk in bm_frames}
    rf_df, rf_source = _load_cd91(ws, end, progress)

    return build_dataset(
        calendar, None, frames, start, market_of, names,
        bm_frames=bm_frames, bm_markets=bm_markets, bm_is_total_return=bm_tr,
        flow_frames=flow_frames, rf_frame=rf_df, rf_fallback=p.risk_free_fallback, rf_source=rf_source,
        market_index_frames=index_frames, fx_frame=fx_frame,
        info={"universe_size_requested": universe_size, "flows_requested": include_flows,
              "cutoff": end.isoformat(), "holdout_allowed": bool(allow_holdout),
              "excluded_etf": excluded, "universe_fixed_from": "",
              "universe_built_on": date.today().isoformat(), "markets": list(markets),
              "bm_requested": [MARKET_BM_ETF[m] for m in markets], "bm_missing": bm_missing,
              "names_by_market": {m: sum(1 for c in frames if market_of[c] == m) for m in markets}},
        cutoff=end,
    )


def _load_market_indexes(markets, warm_start: date, end: date, progress,
                         should_stop) -> tuple[dict[str, pl.DataFrame], list[date]]:
    """(market -> index frame, union calendar from `warm_start` to `end`), spec 9-25."""
    ws = warm_start.isoformat()
    index_frames: dict[str, pl.DataFrame] = {}
    days: set = set()
    for m in markets:
        _check_stop(should_stop)
        _say(progress, tr(f"{m} 지수 이력 불러오는 중...", f"Loading {m} index history..."))
        f = _cut_frame(get_historical_data(MARKET_INDEX[m], ws), end)
        if f is None or f.is_empty():
            raise RuntimeError(f"{m} index history unavailable (data.history returned nothing)")
        index_frames[m] = f
        days |= {_to_date(d) for d in f.get_column("Date").to_list()}
    calendar = sorted(d for d in days if warm_start <= d <= end)
    if not calendar:
        raise RuntimeError(f"No trading days between {warm_start} and {end}")
    return index_frames, calendar


def _load_fx(markets, end: date, progress) -> tuple[pl.DataFrame | None, float]:
    """(USD/KRW history, latest rate) when a US market is selected, else (None, 0.0)."""
    if not any(m in US_MARKETS for m in markets):
        return None, 0.0
    _say(progress, tr("USD/KRW 환율 이력 불러오는 중...", "Loading USD/KRW history..."))
    fx_frame = _cut_frame(get_usd_krw_history(), end)
    if fx_frame is None or fx_frame.is_empty():
        raise RuntimeError("USD/KRW history unavailable: US prices cannot be converted to KRW")
    return fx_frame, float(fx_frame.get_column("Close").drop_nulls()[-1])


def _multi_market_universe(markets, universe_size: int, usd_krw: float, progress, should_stop
                           ) -> tuple[list[str], dict[str, str], dict[str, str], list[dict]]:
    """(codes, names, code -> market, excluded ETF rows): the top `universe_size` stocks of
    every selected market ranked together by KRW market cap (spec 9-25)."""
    pool = universe_size * LISTING_POOL_FACTOR
    rows: list[dict] = []
    excluded: list[dict] = []
    for m in markets:
        _check_stop(should_stop)
        _say(progress, tr(f"{m} 목록 불러오는 중 (상위 {pool}, ETF/ETN 제외)...", f"Loading {m} listing (top {pool}, ETF/ETN excluded)..."))
        kept, ex = _ranked_listing(m, pool, usd_krw)
        if not kept:
            raise RuntimeError(f"{m} listing unavailable")
        rows += kept
        excluded += ex
    rows.sort(key=lambda r: -r["MarcapKRW"])
    rows = rows[:universe_size]
    codes = [r["Code"] for r in rows]
    names = {r["Code"]: str(r.get("Name", "")) for r in rows}
    market_of = {r["Code"]: r["Market"] for r in rows}
    return codes, names, market_of, excluded


def _load_market_benchmarks(markets, ws: str, end: date, progress, should_stop
                            ) -> tuple[dict[str, pl.DataFrame], bool, list[str]]:
    """(ETF ticker -> frame, is total return, missing "market: ticker" labels): one BM ETF
    per selected market for the BM1/BM3 mix (spec 9-25); empty when any is missing."""
    _check_stop(should_stop)
    _say(progress, tr("벤치마크 ETF 불러오는 중...", "Loading benchmark ETFs..."))
    bm_frames: dict[str, pl.DataFrame] = {}
    bm_missing: list[str] = []
    bm_tr = True
    for m in markets:
        tk = MARKET_BM_ETF[m]
        f = _cut_frame(get_historical_data(tk, ws), end)
        if (f is None or f.is_empty()) and tk == BM_TR_TICKER:
            tk, bm_tr = BM_PRICE_FALLBACK_TICKER, False
            f = _cut_frame(get_historical_data(tk, ws), end)
        if f is not None and not f.is_empty():
            bm_frames[tk] = f
        else:
            bm_missing.append(f"{m}: {MARKET_BM_ETF[m]}")
    if bm_missing:
        # Spec 9-25 defines BM1/BM3 as the equal-weight mix of *every* selected market's
        # ETF. A mix of the ones that happened to load would be a different benchmark
        # under the same name (review_codex.md C3), so none is used: BM1/BM3 and the
        # BM-relative metrics come out N/A and the report names what was missing.
        logger.warning("benchmark ETF history missing for %s; BM1/BM3 are skipped", ", ".join(bm_missing))
        bm_frames = {}
    return bm_frames, bm_tr, bm_missing
