"""data/raw_prices.py — KRX daily prices from KIS, unadjusted (as-traded) or split-adjusted.

``get_historical_data`` and the Naver chart API return split-adjusted prices, which hides the
price level the market actually traded at (tick grid, whole-share sizing, daily limits).
``get_raw_daily_prices`` is what the A5 check of the Nexus validation needs: daily OHLCV with
the KIS "original price" switch (``FID_ORG_ADJ_PRC=1``). ``get_adjusted_daily_prices`` is the
same fetch with the switch off, the independent source ``data.price_crosscheck`` compares
against Naver. Neither is used by the app's own data paths, they touch no cache, and they never
return a row after ``max_date`` (default: the end of the development window, 2024-12-31), so
the holdout year cannot enter through them.
"""
import logging
import time
from datetime import date, datetime, timedelta

import polars as pl

from data.cache import _KIS_SESSION
from data.collectors.kis import _KIS_BASE_URL, _kis_headers

logger = logging.getLogger(__name__)

RAW_DEV_END = date(2024, 12, 31)
_KIS_DAILY_PAGE = 100          # rows per inquire-daily-itemchartprice response
_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume", "Value"]


def parse_kis_daily_rows(items: list[dict], max_date: date = RAW_DEV_END) -> list[dict]:
    """KIS ``output2`` items -> ascending, de-duplicated rows with int prices, dropping rows
    after ``max_date`` and rows without a usable price (blank / zero OHLC)."""
    out: dict[date, dict] = {}
    for item in items or []:
        try:
            d = datetime.strptime(str(item["stck_bsop_date"]), "%Y%m%d").date()
            o, h, lo, c = (int(item.get(k) or 0) for k in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr"))
            vol = int(item.get("acml_vol") or 0)
            val = int(float(item.get("acml_tr_pbmn") or 0))
        except (KeyError, ValueError, TypeError):
            continue
        if d > max_date or min(o, h, lo, c) <= 0:
            continue
        out[d] = {"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": vol, "Value": val}
    return [out[d] for d in sorted(out)]


def rows_to_frame(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame({c: [] for c in _COLUMNS}, schema={
            "Date": pl.Date, "Open": pl.Int64, "High": pl.Int64, "Low": pl.Int64, "Close": pl.Int64,
            "Volume": pl.Int64, "Value": pl.Int64})
    return pl.DataFrame(rows).select(_COLUMNS)


def _get_page(ticker: str, start: date, cursor: date, attempts: int = 4, adjusted: bool = False) -> dict:
    """One KIS page; HTTP errors (KIS answers 500 or a rt_cd error under bursts) are retried with a growing pause."""
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
              "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
              "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0" if adjusted else "1"}
    for k in range(attempts):
        try:
            r = _KIS_SESSION.get(
                f"{_KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                headers=_kis_headers("FHKST03010100"), params=params, timeout=10)
            r.raise_for_status()
            body = r.json()
            if body.get("rt_cd") == "0":
                return body
            err = RuntimeError(f"KIS daily prices for {ticker} failed: {body.get('msg1')}")
        except Exception as e:
            err = e
        if k == attempts - 1:
            raise err
        time.sleep(1.5 * (k + 1))      # rate-limit answers come back as rt_cd != 0 with a misleading message
    raise RuntimeError("unreachable")


def get_raw_daily_prices(ticker: str, start: date, end: date | None = None, max_date: date = RAW_DEV_END,
                         pause: float = 0.3) -> pl.DataFrame:
    """Unadjusted daily OHLCV of a KRX code for ``start..min(end, max_date)`` (Date, Open, High,
    Low, Close, Volume, Value in KRW)."""
    return _get_daily_prices(ticker, start, end, max_date, pause, adjusted=False)


def get_adjusted_daily_prices(ticker: str, start: date, end: date | None = None, max_date: date = RAW_DEV_END,
                              pause: float = 0.3) -> pl.DataFrame:
    """Split-adjusted daily OHLCV from KIS, same shape and holdout guard as ``get_raw_daily_prices``."""
    return _get_daily_prices(ticker, start, end, max_date, pause, adjusted=True)


def _get_daily_prices(ticker: str, start: date, end: date | None, max_date: date, pause: float,
                      adjusted: bool) -> pl.DataFrame:
    """Pages backwards through KIS, 100 sessions per call."""
    stop = min(end or max_date, max_date)
    cursor = stop
    rows: dict[date, dict] = {}
    while cursor >= start:
        body = _get_page(ticker, start, cursor, adjusted=adjusted)
        page = parse_kis_daily_rows(body.get("output2", []), max_date)
        if not page:
            break
        for row in page:
            if row["Date"] >= start:
                rows[row["Date"]] = row
        oldest = page[0]["Date"]
        if oldest <= start or oldest >= cursor + timedelta(days=1):
            break
        cursor = oldest - timedelta(days=1)
        time.sleep(pause)
    logger.info("%s daily prices %s: %d rows", "adjusted" if adjusted else "raw", ticker, len(rows))
    return rows_to_frame([rows[d] for d in sorted(rows)])
