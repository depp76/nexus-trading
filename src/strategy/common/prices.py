"""strategy/common/prices.py — strategy-agnostic price structures shared by
every strategy package and by strategy.signal_api (moved out of
strategy.trend_following.dataset on 2026-11-16, which re-exports them).

``PriceBook`` is OHLCV for a set of tickers on one calendar (T x N arrays);
``align_frame`` reindexes a polars frame onto a calendar; ``flow_arrays``
turns per-ticker investor-flow frames into KRW amount arrays;
``ResearchCancelled`` / ``check_stop`` / ``say`` are the cancel and progress
plumbing the worker threads use.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
import polars as pl

logger = logging.getLogger(__name__)

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


class ResearchCancelled(RuntimeError):
    """Raised by load_dataset / run_research when the caller's `should_stop`
    callable returns True (the Stop button of the Trend Following tab)."""


def _check_stop(should_stop, futures=None) -> None:
    if should_stop is not None and should_stop():
        for f in futures or ():
            f.cancel()      # queued fetches never start; in-flight ones finish
        raise ResearchCancelled("cancelled")


# Public names (the underscore ones are kept for the old import sites).
say = _say
check_stop = _check_stop
to_date = _to_date
flow_arrays = _flow_arrays
