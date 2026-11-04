"""data/krx_calendar.py — KRX open days from the KIS holiday API, cached on disk.

A day that was already over when it was fetched never changes, so it is served from
``cache/krx_calendar.json`` forever. Today and later days can still change (a temporary holiday
is declared at short notice), so they are refreshed at most once per calendar day, which also
keeps to the once-a-day use KIS asks for on this TR. If that refresh fails the cached answer is
served with a warning. A day nobody has an answer for raises: guessing "weekday = open" here
would quietly schedule a weekly check on a holiday.
"""
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from data.collectors.kis import _KST, fetch_kis_holiday_page
from paths import KRX_CALENDAR_CACHE_FILE

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PAGE_PAUSE_SEC = 0.3


def _today() -> date:
    return datetime.now(_KST).date()


def parse_holiday_rows(items: list[dict]) -> dict[date, bool]:
    """KIS chk-holiday rows -> {day: is_open}. Rows without a readable date or an explicit Y/N
    open flag are dropped, so a malformed row can never read as a closed (or open) day."""
    out: dict[date, bool] = {}
    for item in items or []:
        flag = str(item.get("opnd_yn", "")).strip().upper()
        if flag not in ("Y", "N"):
            continue
        try:
            out[datetime.strptime(str(item["bass_dt"]).strip(), "%Y%m%d").date()] = flag == "Y"
        except (KeyError, ValueError):
            continue
    return out


def _load_cache() -> tuple[dict[date, bool], date | None]:
    try:
        with open(KRX_CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        days = {date.fromisoformat(k): bool(v) for k, v in raw["days"].items()}
        return days, date.fromisoformat(raw["fetched_on"])
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.warning("KRX calendar cache unreadable, rebuilding it", exc_info=True)
        return {}, None


def _save_cache(days: dict[date, bool], fetched_on: date) -> None:
    payload = {"fetched_on": fetched_on.isoformat(), "days": {d.isoformat(): days[d] for d in sorted(days)}}
    tmp = KRX_CALENDAR_CACHE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(KRX_CALENDAR_CACHE_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, KRX_CALENDAR_CACHE_FILE)
    except OSError:
        logger.warning("KRX calendar cache save failed", exc_info=True)


def _fetch_range(start: date, end: date) -> dict[date, bool]:
    """Every page covering start..end. A page that does not advance ends the walk, and the
    caller's coverage check turns any day still missing into an error."""
    out: dict[date, bool] = {}
    cursor = start
    while cursor <= end:
        page = parse_holiday_rows(fetch_kis_holiday_page(cursor.strftime("%Y%m%d")))
        if not page or max(page) < cursor:
            break
        out.update(page)
        cursor = max(page) + timedelta(days=1)
        if cursor <= end:
            time.sleep(_PAGE_PAUSE_SEC)
    return out


def get_krx_sessions(start: date, end: date) -> list[date]:
    """Ascending KRX open days in start..end. See the module docstring for the cache rules.
    Raises RuntimeError when a day in the range has no answer from KIS or from the cache."""
    if end < start:
        return []
    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    with _LOCK:
        today = _today()
        days, fetched_on = _load_cache()
        refresh_live = fetched_on != today
        needed = [d for d in all_days if d not in days or (d >= today and refresh_live)]
        if needed:
            try:
                fresh = _fetch_range(needed[0], needed[-1])
            except Exception as e:
                fresh = {}
                missing = [d for d in all_days if d not in days]
                if missing:
                    raise RuntimeError(f"KRX calendar unavailable for {missing[0]}..{missing[-1]}: {e}") from e
                logger.warning("KRX calendar refresh failed, serving cached days: %s", e)
            if fresh:
                days.update(fresh)
                # Claim today's refresh only if it reached every live day asked for; a partial
                # walk must not suppress the retry for the days it did not reach.
                live_done = all(d in fresh for d in needed if d >= today)
                _save_cache(days, today if live_done else (fetched_on or today - timedelta(days=1)))
        missing = [d for d in all_days if d not in days]
        if missing:
            raise RuntimeError(f"KIS holiday API returned no answer for {missing[0]}..{missing[-1]}")
        return [d for d in all_days if days[d]]
