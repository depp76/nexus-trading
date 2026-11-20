"""data/rates.py — CD 91-day rate series (the Trend Following BM4 / Sharpe
risk-free rate, trend_following.md 6-3).

Sources, in order: the Bank of Korea ECOS API when ``ECOS_API_KEY`` is set in
.env (statistic 817Y002 "시장금리(일별)", item 010502000 = CD(91일); verify the
item code on ecos.bok.or.kr if the response is empty), else a hand-made
``cd91.csv`` at the repo root (columns Date, Rate in percent), else nothing --
the strategy then falls back to its constant ``risk_free_fallback``. Fetched
rows are cached in cache/cd91.json.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

import polars as pl
import requests

from paths import CD91_CACHE_FILE, CD91_CSV_FILE
from data.cache import PAYLOAD_ERRORS

logger = logging.getLogger(__name__)

ECOS_STAT_CODE = "817Y002"
ECOS_CD91_ITEM = "010502000"
_ECOS_URL = "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100000/{stat}/D/{start}/{end}/{item}"
# The cache counts as covering [start, end] when its first row is at most this many
# days after `start` and its last at most this many days before `end`: room for
# holidays (Chuseok/Seollal run up to five days) and ECOS publishing a day or two late.
_COVERAGE_SLACK = timedelta(days=7)
# Serializes the cache file's read-merge-write (and its fixed .tmp path) across threads.
_CACHE_LOCK = threading.Lock()


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={"Date": pl.Date, "Rate": pl.Float64})


def _frame(rows: dict[str, float]) -> pl.DataFrame:
    if not rows:
        return _empty()
    keys = sorted(rows)
    return pl.DataFrame({"Date": [datetime.strptime(k, "%Y-%m-%d").date() for k in keys],
                         "Rate": [float(rows[k]) for k in keys]})


def _load_cache() -> dict[str, float]:
    if not os.path.exists(CD91_CACHE_FILE):
        return {}
    try:
        with open(CD91_CACHE_FILE, encoding="utf-8") as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        # TypeError/AttributeError: a cache file whose JSON is not a {date: rate} dict.
        logger.debug("CD91 cache unreadable, ignoring", exc_info=True)
        return {}


def _save_cache(rows: dict[str, float]) -> None:
    try:
        os.makedirs(os.path.dirname(CD91_CACHE_FILE), exist_ok=True)
        tmp = CD91_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, CD91_CACHE_FILE)
    except OSError:
        logger.warning("CD91 cache save failed", exc_info=True)


def fetch_ecos_cd91(start: str, end: str, api_key: str, timeout: int = 15) -> dict[str, float]:
    """{YYYY-MM-DD: rate %} from ECOS for [start, end] (YYYY-MM-DD)."""
    url = _ECOS_URL.format(key=api_key, stat=ECOS_STAT_CODE, start=start.replace("-", ""),
                           end=end.replace("-", ""), item=ECOS_CD91_ITEM)
    res = requests.get(url, timeout=timeout)
    res.raise_for_status()
    payload = res.json()
    block = payload.get("StatisticSearch") or {}
    rows = block.get("row") or []
    out: dict[str, float] = {}
    for r in rows:
        t = str(r.get("TIME", "")).strip()
        v = r.get("DATA_VALUE")
        if len(t) != 8 or v in (None, ""):
            continue
        try:
            out[f"{t[:4]}-{t[4:6]}-{t[6:8]}"] = float(v)
        except (TypeError, ValueError):
            continue
    if not out and payload.get("RESULT"):
        logger.warning("ECOS CD91 query returned no rows: %s", payload.get("RESULT"))
    return out


def read_cd91_csv(path: str | None = None) -> dict[str, float]:
    """{YYYY-MM-DD: rate %} from the hand-made CSV (default: paths.CD91_CSV_FILE,
    resolved at call time so tests can point it elsewhere)."""
    path = path or CD91_CSV_FILE
    out: dict[str, float] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            d = str(row.get("Date", "")).strip().replace(".", "-").replace("/", "-")[:10]
            try:
                datetime.strptime(d, "%Y-%m-%d")
                out[d] = float(str(row.get("Rate", "")).replace("%", "").strip())
            except (TypeError, ValueError):
                continue
    return out


def _cache_covers(cached: dict[str, float], start: str, end: str) -> bool:
    """True when the cached rows span [start, end] up to _COVERAGE_SLACK at each end
    (date arithmetic; the old string test treated any row in end's month as covering
    it and could never be met for a start after the 7th of a month)."""
    have = sorted(k for k in cached if start <= k <= end)
    if not have:
        return False
    try:
        start_d = datetime.strptime(start[:10], "%Y-%m-%d").date()
        end_d = datetime.strptime(end[:10], "%Y-%m-%d").date()
        first = datetime.strptime(have[0], "%Y-%m-%d").date()
        last = datetime.strptime(have[-1], "%Y-%m-%d").date()
    except ValueError:
        return False
    return first <= start_d + _COVERAGE_SLACK and last >= end_d - _COVERAGE_SLACK


def get_cd91_series(start: str, end: str | None = None) -> pl.DataFrame:
    """(Date, Rate %) rows covering [start, end]; empty when no source is available."""
    end = end or date.today().isoformat()
    cached = _load_cache()
    api_key = os.getenv("ECOS_API_KEY", "").strip()
    if api_key and not _cache_covers(cached, start, end):
        try:
            fetched = fetch_ecos_cd91(start, end, api_key)
            if fetched:
                with _CACHE_LOCK:
                    cached = _load_cache()
                    cached.update(fetched)
                    _save_cache(cached)
        except PAYLOAD_ERRORS as e:
            # The key is part of the request URL, which request errors quote; never log it
            # (and no traceback, whose messages would carry the URL too).
            logger.warning("ECOS CD91 fetch failed: %s: %s", type(e).__name__, str(e).replace(api_key, "***"))
    rows = {k: v for k, v in cached.items() if start <= k <= end}
    if not rows:
        rows = {k: v for k, v in read_cd91_csv().items() if start <= k <= end}
    return _frame(rows)
