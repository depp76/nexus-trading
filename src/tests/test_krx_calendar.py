"""KRX calendar cache rules (data.krx_calendar) and the weekly check dates built on it. No network."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from data import krx_calendar
from strategy.trend_following.schedule import weekly_check_dates

TODAY = date(2026, 11, 4)


def _page(first: date, closed=(), n=24):
    """A KIS-shaped page of n days; weekends and `closed` are not open."""
    rows = []
    for i in range(n):
        d = first + timedelta(days=i)
        is_open = d.weekday() < 5 and d not in closed
        rows.append({"bass_dt": d.strftime("%Y%m%d"), "opnd_yn": "Y" if is_open else "N"})
    return rows


class CalendarCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "sub", "krx_calendar.json")
        self.today = TODAY
        self.closed = set()
        self.calls = []
        for p in (patch.object(krx_calendar, "KRX_CALENDAR_CACHE_FILE", self.path),
                  patch.object(krx_calendar, "_PAGE_PAUSE_SEC", 0),
                  patch.object(krx_calendar, "_today", side_effect=lambda: self.today),
                  patch.object(krx_calendar, "fetch_kis_holiday_page", side_effect=self._fetch)):
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, base):
        self.calls.append(base)
        return _page(datetime.strptime(base, "%Y%m%d").date(), self.closed)


class TestParse(unittest.TestCase):
    def test_malformed_rows_are_dropped_not_read_as_closed(self):
        rows = [{"bass_dt": "20261104", "opnd_yn": "Y"}, {"bass_dt": "20261105", "opnd_yn": "N"},
                {"bass_dt": "20261106", "opnd_yn": ""}, {"bass_dt": "bad", "opnd_yn": "Y"}, {"opnd_yn": "Y"}]
        self.assertEqual(krx_calendar.parse_holiday_rows(rows),
                         {date(2026, 11, 4): True, date(2026, 11, 5): False})


class TestSessions(CalendarCase):
    def test_excludes_weekends_and_holidays_and_pages_past_one_response(self):
        self.closed = {date(2024, 2, 12)}
        got = krx_calendar.get_krx_sessions(date(2024, 1, 1), date(2024, 2, 29))
        self.assertEqual(len(self.calls), 3)                # 24 days per page
        self.assertNotIn(date(2024, 2, 12), got)
        self.assertNotIn(date(2024, 1, 6), got)
        self.assertIn(date(2024, 2, 13), got)
        self.assertEqual(got, sorted(got))

    def test_past_days_never_refetched_even_on_a_later_day(self):
        krx_calendar.get_krx_sessions(date(2024, 1, 1), date(2024, 1, 20))
        self.calls.clear()
        self.today = TODAY + timedelta(days=30)
        krx_calendar.get_krx_sessions(date(2024, 1, 1), date(2024, 1, 20))
        self.assertEqual(self.calls, [])

    def test_live_days_refresh_once_a_day_and_pick_up_a_temporary_holiday(self):
        rng = (TODAY, TODAY + timedelta(days=9))
        self.assertIn(date(2026, 11, 9), krx_calendar.get_krx_sessions(*rng))
        self.calls.clear()
        self.closed = {date(2026, 11, 9)}
        self.assertIn(date(2026, 11, 9), krx_calendar.get_krx_sessions(*rng))   # same day: cache
        self.assertEqual(self.calls, [])
        self.today = TODAY + timedelta(days=1)
        again = krx_calendar.get_krx_sessions(self.today, rng[1])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn(date(2026, 11, 9), again)

    def test_failed_refresh_serves_cache_but_a_day_without_any_answer_raises(self):
        rng = (TODAY, TODAY + timedelta(days=9))
        first = krx_calendar.get_krx_sessions(*rng)
        self.today = TODAY + timedelta(days=1)
        with patch.object(krx_calendar, "fetch_kis_holiday_page", side_effect=OSError("down")):
            self.assertEqual(krx_calendar.get_krx_sessions(*rng), first)
            with self.assertRaises(RuntimeError):
                krx_calendar.get_krx_sessions(date(2027, 3, 1), date(2027, 3, 5))

    def test_failed_refresh_is_retried_rather_than_counted_as_todays(self):
        rng = (TODAY, TODAY + timedelta(days=9))
        krx_calendar.get_krx_sessions(*rng)
        self.today = TODAY + timedelta(days=1)
        with patch.object(krx_calendar, "fetch_kis_holiday_page", side_effect=OSError("down")):
            krx_calendar.get_krx_sessions(*rng)
        self.calls.clear()
        krx_calendar.get_krx_sessions(*rng)
        self.assertEqual(len(self.calls), 1)

    def test_partial_walk_does_not_mark_live_days_refreshed(self):
        rng = (TODAY, TODAY + timedelta(days=30))     # needs two pages
        krx_calendar.get_krx_sessions(*rng)
        self.today = TODAY + timedelta(days=1)
        answers = iter([_page(self.today), []])       # the second page comes back empty
        with patch.object(krx_calendar, "fetch_kis_holiday_page", side_effect=lambda base: next(answers)):
            krx_calendar.get_krx_sessions(*rng)
        self.calls.clear()
        krx_calendar.get_krx_sessions(*rng)
        self.assertGreaterEqual(len(self.calls), 1)

    def test_empty_answer_raises_instead_of_inventing_days(self):
        with patch.object(krx_calendar, "fetch_kis_holiday_page", return_value=[]):
            with self.assertRaises(RuntimeError):
                krx_calendar.get_krx_sessions(date(2024, 1, 1), date(2024, 1, 5))

    def test_corrupt_cache_is_rebuilt(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(len(krx_calendar.get_krx_sessions(date(2024, 1, 1), date(2024, 1, 5))), 5)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["fetched_on"], TODAY.isoformat())


class TestWeeklyCheckDates(CalendarCase):
    def test_monday_holiday_moves_to_tuesday(self):
        self.closed = {date(2024, 2, 12)}
        self.assertEqual(weekly_check_dates(date(2024, 2, 5), date(2024, 2, 18), weekday=0),
                         [date(2024, 2, 5), date(2024, 2, 13)])

    def test_range_cut_mid_week_keeps_the_real_check_day(self):
        # Friday check; a range ending Wednesday must not promote Wednesday to the check day.
        self.assertEqual(weekly_check_dates(date(2024, 2, 5), date(2024, 2, 14), weekday=4), [date(2024, 2, 9)])

    def test_check_day_before_range_start_is_excluded(self):
        self.assertEqual(weekly_check_dates(date(2024, 2, 6), date(2024, 2, 9), weekday=0), [])

    def test_default_weekday_comes_from_params(self):
        self.assertEqual(weekly_check_dates(date(2024, 2, 5), date(2024, 2, 11)), [date(2024, 2, 5)])
