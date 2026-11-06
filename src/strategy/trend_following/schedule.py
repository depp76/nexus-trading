"""strategy/trend_following/schedule.py — real-calendar dates of the weekly check (2-1, 9-20).

The engine derives check days from the sessions in its dataset, which only exist in hindsight.
This gives the same days ahead of time from the KRX calendar, with the same rule
(``signals.weekly_check_days``). Nothing in the engine or the UI calls it yet.

  python -m strategy.trend_following.schedule --start 2026-11-01 --end 2026-12-31
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from data.krx_calendar import get_krx_sessions
from strategy.trend_following.config import StrategyParams
from strategy.trend_following.signals import weekly_check_days


def weekly_check_dates(start: date, end: date, weekday: int | None = None) -> list[date]:
    """Weekly check dates in start..end. Sessions are read for whole ISO weeks: the rule looks at
    the week's sessions, so a range cut mid-week would pick the wrong day for that week."""
    if end < start:
        return []
    wd = StrategyParams().check_weekday if weekday is None else weekday
    monday = start - timedelta(days=start.weekday())
    sunday = end + timedelta(days=6 - end.weekday())
    sessions = get_krx_sessions(monday, sunday)
    mask = weekly_check_days(sessions, wd)
    return [d for d, hit in zip(sessions, mask) if hit and start <= d <= end]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m strategy.trend_following.schedule", description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--weekday", type=int, help="0=Mon .. 4=Fri; default: StrategyParams.check_weekday")
    args = ap.parse_args(argv)
    for d in weekly_check_dates(args.start, args.end, args.weekday):
        print(d.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
