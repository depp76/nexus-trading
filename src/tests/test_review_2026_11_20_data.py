"""Regression tests for the data-layer findings of the 2026-11-20 code review
(review_agy.md C1, H1-H5, M3, M5, L2). No network: sessions and collectors are mocked."""
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import polars as pl
import requests

import data.cache as dcache
import data.flows as flows
import data.history as history
import data.rates as rates
from data import fx, krx_calendar, listing, market
from data.collectors import krx, naver, yahoo
from data.indicators import _compute_indicators, fetch_historical_changes


def _usd_frame(closes):
    days = [date(2026, 11, 2) + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame({"Date": days, "Close": closes})


# ── C1 / M5: USD/KRW ─────────────────────────────────────────────────────────

class TestUsdKrwColdStart(unittest.TestCase):
    def setUp(self):
        self._saved = dict(dcache._USD_KRW_CACHE)
        dcache._USD_KRW_CACHE.update(rate=None, df=None, fetched_at=None, failed_at=None)

    def tearDown(self):
        dcache._USD_KRW_CACHE.clear()
        dcache._USD_KRW_CACHE.update(self._saved)

    def test_cold_start_failure_is_retried_after_the_interval(self):
        good = pd.DataFrame({"Close": [1400.0, 1410.0]},
                            index=pd.to_datetime(["2026-11-18", "2026-11-19"]))
        with patch("data.fx._fetch_usd_krw_history", side_effect=requests.ConnectionError("down")) as fetch, \
                self.assertLogs("data.fx", level="WARNING"):
            self.assertEqual(fx.get_usd_krw_rate(), fx._USD_KRW_FALLBACK)
            # Within the retry interval the fallback is served without a new request.
            self.assertEqual(fx.get_usd_krw_rate(), fx._USD_KRW_FALLBACK)
            self.assertEqual(fetch.call_count, 1)
        dcache._USD_KRW_CACHE["failed_at"] -= fx._USD_KRW_RETRY_AFTER
        with patch("data.fx._fetch_usd_krw_history", return_value=good) as fetch, \
                patch("data.fx._hist_df_is_stale", return_value=False):
            self.assertEqual(fx.get_usd_krw_rate(), 1410.0)
            self.assertEqual(fetch.call_count, 1)
            self.assertIsNone(dcache._USD_KRW_CACHE["failed_at"])
            self.assertEqual(fx.get_usd_krw_rate(), 1410.0)     # now a plain cache hit
            self.assertEqual(fetch.call_count, 1)

    def test_rate_for_date_skips_null_closes(self):
        dcache._USD_KRW_CACHE.update(rate=1300.0, df=_usd_frame([1300.0, 1310.0, None]),
                                     fetched_at=datetime.now())
        with patch("data.fx._hist_df_is_stale", return_value=False):
            self.assertEqual(fx.get_usd_krw_rate_for_date("2026-11-04"), 1310.0)


class TestIndexCloseForDate(unittest.TestCase):
    def test_null_close_is_skipped(self):
        df = _usd_frame([2500.0, None])
        with patch.object(market, "get_historical_data", return_value=df):
            self.assertEqual(market.get_index_close_for_date("KS11", "2026-11-03"), 2500.0)


# ── H1: investor flows ───────────────────────────────────────────────────────

def _flow_rows(days):
    return [{"Date": d.strftime("%Y.%m.%d"), "Close": 1000, "Foreigner": 1, "Institution": 2, "Retail": -3}
            for d in sorted(days, reverse=True)]


def _weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class TestInvestorFlowCoverage(unittest.TestCase):
    TODAY = date(2026, 9, 28)   # Monday

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(flows, "FLOWS_CACHE_DIR", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def _saved(self, ticker):
        with open(os.path.join(self.tmp.name, f"{ticker}.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_partial_walk_does_not_claim_coverage(self):
        recent = _flow_rows(_weekdays(date(2026, 7, 1), date(2026, 9, 25)))
        with patch.object(flows, "fetch_investor_trend_paged", return_value=(recent, False)) as fetch:
            df = flows.get_investor_flows("005930", "2022-01-01", today=self.TODAY)
            self.assertGreater(df.height, 0)
            self.assertIsNone(self._saved("005930")["covers_from"])
            flows.get_investor_flows("005930", "2022-01-01", today=self.TODAY)
            self.assertEqual(fetch.call_count, 2)       # history still missing -> asked again

    def test_complete_walk_claims_coverage(self):
        rows = _flow_rows(_weekdays(date(2026, 8, 3), date(2026, 9, 25)))
        with patch.object(flows, "fetch_investor_trend_paged", return_value=(rows, True)):
            flows.get_investor_flows("005930", "2026-08-01", today=self.TODAY)
        self.assertEqual(self._saved("005930")["covers_from"], "2026-08-01")

    def test_incomplete_recent_walk_with_a_gap_is_not_cached(self):
        old = _flow_rows(_weekdays(date(2026, 8, 3), date(2026, 8, 28)))
        with patch.object(flows, "fetch_investor_trend_paged", return_value=(old, True)):
            flows.get_investor_flows("000660", "2026-08-01", today=date(2026, 8, 31))
        newer = _flow_rows(_weekdays(date(2026, 9, 21), date(2026, 9, 25)))   # stops short of 08-28
        with patch.object(flows, "fetch_investor_trend_paged", return_value=(newer, False)), \
                self.assertLogs("data.flows", level="WARNING"):
            flows.get_investor_flows("000660", "2026-08-01", today=self.TODAY)
        self.assertEqual(max(r["Date"] for r in self._saved("000660")["rows"]), "2026-08-28")

    def test_save_merges_rows_another_thread_saved_meanwhile(self):
        mine = _flow_rows(_weekdays(date(2026, 9, 1), date(2026, 9, 25)))
        theirs = {"covers_from": None, "rows": [
            {"Date": "2026-01-05", "Close": 1.0, "Foreigner": 0.0, "Institution": 0.0, "Retail": 0.0}]}

        def fetch(ticker, days):
            # Another thread saves the same ticker while this one is on the network.
            with open(os.path.join(self.tmp.name, f"{ticker}.json"), "w", encoding="utf-8") as f:
                json.dump(theirs, f)
            return mine, False

        with patch.object(flows, "fetch_investor_trend_paged", side_effect=fetch):
            flows.get_investor_flows("035420", "2026-09-01", today=self.TODAY)
        dates = [r["Date"] for r in self._saved("035420")["rows"]]
        self.assertIn("2026-01-05", dates)
        self.assertIn("2026-09-25", dates)


def _trend_items(first: date, n: int):
    days = [first - timedelta(days=i) for i in range(n)]
    return [{"bizdate": d.strftime("%Y%m%d"), "accumulatedTradingVolume": "100", "closePrice": "1,000",
             "foreignerPureBuyQuant": "+1", "organPureBuyQuant": "-1", "individualPureBuyQuant": "0"}
            for d in days]


class TestTrendPagingCompleteness(unittest.TestCase):
    def _res(self, items):
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = items
        return r

    def test_failed_page_reports_incomplete(self):
        pages = [self._res(_trend_items(date(2026, 9, 25), 3)), requests.ConnectionError("down")]
        with patch.object(naver, "_TREND_PAGE_SIZE", 3), \
                patch.object(naver._NAVER_SESSION, "get", side_effect=pages), \
                self.assertLogs("data.collectors.naver", level="WARNING"):
            rows, complete = naver.fetch_investor_trend_paged("005930", days=10)
        self.assertEqual((len(rows), complete), (3, False))

    def test_end_of_series_is_complete(self):
        with patch.object(naver._NAVER_SESSION, "get", return_value=self._res(_trend_items(date(2026, 9, 25), 4))):
            rows, complete = naver.fetch_investor_trend_paged("005930", days=500)
        self.assertEqual((len(rows), complete), (4, True))
        # The list-only wrapper kis.py uses is unchanged.
        with patch.object(naver._NAVER_SESSION, "get", return_value=self._res(_trend_items(date(2026, 9, 25), 4))):
            self.assertEqual(len(naver._fetch_investor_trend_naver("005930", days=500)), 4)

    def test_reply_of_the_wrong_shape_is_incomplete_not_raised(self):
        with patch.object(naver._NAVER_SESSION, "get", return_value=self._res({"error": "x"})), \
                self.assertLogs("data.collectors.naver", level="WARNING"):
            self.assertEqual(naver.fetch_investor_trend_paged("005930", days=10), ([], False))


# ── H2: yfinance bulk staging cache ──────────────────────────────────────────

class TestYfBulkStaging(unittest.TestCase):
    def tearDown(self):
        dcache._YF_BULK_CACHE.clear()
        dcache._YF_BULK_REFS.clear()

    def test_overlapping_runs_keep_a_shared_key_until_both_release(self):
        nasdaq, sp500 = set(), set()
        df = pl.DataFrame({"Close": [1.0]})
        dcache.yf_bulk_put("AAPL_2025-01-01", df, nasdaq)
        dcache.yf_bulk_put("AAPL_2025-01-01", df, sp500)
        dcache.yf_bulk_put("MSFT_2025-01-01", df, nasdaq)
        dcache.yf_bulk_release(nasdaq)
        self.assertIsNotNone(dcache.yf_bulk_get("AAPL_2025-01-01"))
        self.assertIsNone(dcache.yf_bulk_get("MSFT_2025-01-01"))
        dcache.yf_bulk_release(sp500)
        self.assertEqual((dcache._YF_BULK_CACHE, dcache._YF_BULK_REFS), ({}, {}))

    def test_history_reads_the_staged_frame(self):
        staged = pl.DataFrame({"Date": [date(2026, 11, 19)], "Close": [10.0]})
        dcache.yf_bulk_put("ZZZZ_2025-10-06", staged, set())
        self.assertIs(history._fetch_historical_uncached("ZZZZ", "2025-10-06"), staged)

    def test_staged_frames_are_released_when_a_chunk_raises(self):
        def boom(chunk, run):
            dcache.yf_bulk_put(f"{chunk[0][0]}_{run.start}", pl.DataFrame({"Close": [1.0]}), run.yf_bulk_keys_added)
            raise RuntimeError("chunk failed")

        with patch.object(yahoo, "_prefetch_naver_us_market_caps", return_value={}), \
                patch.object(yahoo, "_process_us_chunk", side_effect=boom):
            with self.assertRaises(RuntimeError):
                yahoo.fetch_us_stock_data_bulk([("AAPL", "Apple")], "NASDAQ 100", 1400.0)
        self.assertEqual(dcache._YF_BULK_CACHE, {})

    def test_run_uses_one_start_for_download_key_and_lookup(self):
        run = yahoo._UsBulkRun(market_name="NASDAQ 100", fx_rate=1.0, total=1, progress_callback=None,
                               naver_mcaps={}, lock=threading.Lock(), yf_semaphore=threading.Semaphore(1),
                               start="2025-10-06")
        frame = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.to_datetime(["2026-11-18", "2026-11-19"]))
        yf_mod = MagicMock()
        yf_mod.download.return_value = frame
        with patch.dict("sys.modules", {"yfinance": yf_mod}), \
                patch.object(yahoo, "start_date", return_value="2099-01-01"):
            yahoo._prefetch_chunk_history(["AAPL"], run)
        self.assertEqual(yf_mod.download.call_args.kwargs["start"], "2025-10-06")
        self.assertEqual(run.yf_bulk_keys_added, {"AAPL_2025-10-06"})
        with patch.object(yahoo, "get_historical_data", return_value=pl.DataFrame()) as ghd:
            yahoo._process_us_symbol("AAPL", "Apple", yahoo._UsChunkMeta(), run)
        self.assertEqual(ghd.call_args.args, ("AAPL", "2025-10-06"))


# ── H3: Yahoo crumb ──────────────────────────────────────────────────────────

class TestYahooCrumb(unittest.TestCase):
    def setUp(self):
        self._saved = dcache._YF_CRUMB
        dcache._YF_CRUMB = None

    def tearDown(self):
        dcache._YF_CRUMB = self._saved

    @staticmethod
    def _reply(text="", status=200):
        r = MagicMock()
        r.text = text
        r.status_code = status
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(f"{status}")
        else:
            r.raise_for_status.return_value = None
        return r

    def test_error_reply_is_not_cached(self):
        replies = [self._reply(), self._reply("Too Many Requests", 429)]
        with patch.object(dcache._YF_SESSION, "get", side_effect=replies), \
                self.assertLogs("data.cache", level="WARNING"):
            self.assertEqual(dcache._get_yf_crumb(), "")
        self.assertIsNone(dcache._YF_CRUMB)            # the next call tries again
        with patch.object(dcache._YF_SESSION, "get", side_effect=[self._reply(), self._reply("abcDEF12.x")]):
            self.assertEqual(dcache._get_yf_crumb(), "abcDEF12.x")

    def test_html_body_is_not_taken_for_a_crumb(self):
        with patch.object(dcache._YF_SESSION, "get", side_effect=[self._reply(), self._reply("<html>oops</html>")]), \
                self.assertLogs("data.cache", level="WARNING"):
            self.assertEqual(dcache._get_yf_crumb(), "")
        self.assertIsNone(dcache._YF_CRUMB)

    def test_refresh_is_skipped_when_another_thread_already_refreshed(self):
        dcache._YF_CRUMB = "new-crumb"
        with patch.object(dcache._YF_SESSION, "get") as get:
            self.assertEqual(dcache._get_yf_crumb(force_refresh=True, stale_crumb="old-crumb"), "new-crumb")
            get.assert_not_called()
        with patch.object(dcache._YF_SESSION, "get", side_effect=[self._reply(), self._reply("fresh")]) as get:
            self.assertEqual(dcache._get_yf_crumb(force_refresh=True, stale_crumb="new-crumb"), "fresh")
            self.assertEqual(get.call_count, 2)

    def test_quote_batch_passes_its_rejected_crumb(self):
        unauthorized = MagicMock(status_code=401)
        ok = MagicMock(status_code=200)
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"quoteResponse": {"result": [{"symbol": "AAPL"}]}}
        with patch.object(yahoo, "_get_yf_crumb", side_effect=["c1", "c2"]) as crumb, \
                patch.object(yahoo._YF_SESSION, "get", side_effect=[unauthorized, ok]):
            self.assertIn("AAPL", yahoo.yf_quote_batch(["AAPL"]))
        self.assertEqual(crumb.call_args.kwargs, {"force_refresh": True, "stale_crumb": "c1"})

    def test_yahoo_pool_fits_the_bulk_fan_out(self):
        adapter = dcache._YF_SESSION.get_adapter("https://query2.finance.yahoo.com")
        self.assertGreaterEqual(adapter._pool_maxsize, 30)


# ── H4: CD91 ─────────────────────────────────────────────────────────────────

class TestCd91(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(rates, "CD91_CACHE_FILE", os.path.join(self.tmp.name, "cd91.json")),
                  patch.object(rates, "CD91_CSV_FILE", os.path.join(self.tmp.name, "cd91.csv"))):
            p.start()
            self.addCleanup(p.stop)

    def test_coverage_uses_date_arithmetic(self):
        rows = {"2026-01-16": 3.0, "2026-11-02": 3.1}
        self.assertFalse(rates._cache_covers(rows, "2026-01-15", "2026-11-20"))   # 18 days short at the end
        rows["2026-11-19"] = 3.2
        self.assertTrue(rates._cache_covers(rows, "2026-01-15", "2026-11-20"))    # a start past the 7th can be met
        self.assertFalse(rates._cache_covers({"2026-02-01": 3.0, "2026-11-19": 3.0}, "2026-01-15", "2026-11-20"))
        self.assertFalse(rates._cache_covers({}, "2026-01-15", "2026-11-20"))

    def test_covered_range_is_not_fetched_again(self):
        with open(rates.CD91_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"2026-01-16": 3.0, "2026-11-19": 3.2}, f)
        with patch.dict(os.environ, {"ECOS_API_KEY": "k"}), patch.object(rates.requests, "get") as get:
            df = rates.get_cd91_series("2026-01-15", "2026-11-20")
        get.assert_not_called()
        self.assertEqual(df.height, 2)

    def test_api_key_is_masked_in_the_log(self):
        secret = "SECRET123KEY"
        err = requests.ConnectionError(f"Max retries exceeded with url: /api/StatisticSearch/{secret}/json/kr")
        with patch.dict(os.environ, {"ECOS_API_KEY": secret}), \
                patch.object(rates.requests, "get", side_effect=err), \
                self.assertLogs("data.rates", level="WARNING") as logs:
            rates.get_cd91_series("2024-01-01", "2024-01-31")
        text = "\n".join(logs.output) + "".join(str(r.exc_info) for r in logs.records)
        self.assertNotIn(secret, text)
        self.assertIn("***", text)


# ── H5: single-flight locks and the stale fallback ───────────────────────────

class TestHistoryStaleFallback(unittest.TestCase):
    KEY = ("STALE1", "2025-01-01")

    def tearDown(self):
        with dcache._HIST_CACHE_LOCK:
            dcache._HIST_CACHE.pop(self.KEY, None)
            dcache._HIST_CACHE_FETCHED_AT.pop(self.KEY, None)

    def test_failed_refresh_returns_the_stale_frame(self):
        old = pl.DataFrame({"Date": [date(2026, 11, 18)], "Close": [5.0]})
        with dcache._HIST_CACHE_LOCK:
            dcache._HIST_CACHE[self.KEY] = old
        with patch.object(history, "_hist_df_is_stale", return_value=True), \
                patch.object(history, "_fetch_historical_uncached", return_value=pl.DataFrame()) as fetch:
            self.assertIs(history.get_historical_data(*self.KEY), old)
        fetch.assert_called_once()
        self.assertEqual(len(history._HIST_KEY_LOCKS), 0)


class TestKeyedLocks(unittest.TestCase):
    def test_lock_is_kept_while_a_waiter_is_queued(self):
        locks = dcache.KeyedLocks()
        inside, release, order = threading.Event(), threading.Event(), []

        def first():
            with locks.hold("k"):
                inside.set()
                release.wait(5)
                order.append("first")

        def second():
            with locks.hold("k"):
                order.append("second")

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(inside.wait(5))
        t2 = threading.Thread(target=second)
        t2.start()
        time.sleep(0.05)
        self.assertEqual(locks._locks["k"][1], 2)       # holder + waiter share one entry
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(len(locks), 0)

    def test_entry_is_dropped_when_the_body_raises(self):
        locks = dcache.KeyedLocks()
        with self.assertRaises(ValueError):
            with locks.hold("k"):
                raise ValueError("x")
        self.assertEqual(len(locks), 0)


class TestListingSingleFlight(unittest.TestCase):
    def test_exception_releases_the_key_lock(self):
        calls = []

        @listing._singleflight_cache(maxsize=4)
        def fn(key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("down")
            return pd.DataFrame({"Code": ["005930"]})

        with self.assertRaises(RuntimeError):
            fn("KRX")
        done = []
        t = threading.Thread(target=lambda: done.append(fn("KRX")))
        t.start()
        t.join(5)
        self.assertEqual(len(done), 1)                  # not blocked on a leaked lock
        self.assertEqual(len(calls), 2)


# ── M3: KRX calendar ─────────────────────────────────────────────────────────

class TestKrxCalendarLiveClaim(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.today = date(2026, 11, 4)
        self.calls = []
        for p in (patch.object(krx_calendar, "KRX_CALENDAR_CACHE_FILE", os.path.join(tmp.name, "cal.json")),
                  patch.object(krx_calendar, "_PAGE_PAUSE_SEC", 0),
                  patch.object(krx_calendar, "_today", side_effect=lambda: self.today),
                  patch.object(krx_calendar, "fetch_kis_holiday_page", side_effect=self._fetch)):
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, base):
        self.calls.append(base)
        first = datetime.strptime(base, "%Y%m%d").date()
        return [{"bass_dt": (first + timedelta(days=i)).strftime("%Y%m%d"),
                 "opnd_yn": "Y" if (first + timedelta(days=i)).weekday() < 5 else "N"} for i in range(24)]

    def test_past_only_fetch_does_not_mark_today_refreshed(self):
        krx_calendar.get_krx_sessions(date(2026, 10, 1), date(2026, 10, 10))
        _days, fetched_on = krx_calendar._load_cache()
        self.assertNotEqual(fetched_on, self.today)
        n = len(self.calls)
        krx_calendar.get_krx_sessions(date(2026, 11, 4), date(2026, 11, 10))
        self.assertGreater(len(self.calls), n)          # today's live refresh still happens


# ── M5: name search, Naver parsing, indicators ───────────────────────────────

class TestNameSearchIsLiteral(unittest.TestCase):
    def test_regex_metacharacters_in_a_name(self):
        krx_df = pd.DataFrame({"Code": ["000001", "000002", "000003"],
                               "Name": ["PLUSS 기타", "PLUS+ 고배당", "알파(H)"]})
        norm = krx_df.assign(NameNorm=["PLUSS기타", "PLUS+고배당", "알파(H)"])
        with patch.object(market, "get_stock_listing", return_value=krx_df), \
                patch.object(market, "_get_listing_with_norm", return_value=norm):
            self.assertEqual(list(market._match_krx_rows("PLUS+", "PLUS+")["Code"]), ["000002"])
            self.assertEqual(list(market._match_krx_rows("알파(", "알파(")["Code"]), ["000003"])   # re.error as a regex


class TestNaverListingParse(unittest.TestCase):
    def test_bad_rows_are_skipped_one_by_one(self):
        res = MagicMock(status_code=200)
        res.json.return_value = {"stocks": [
            {"itemCode": "", "stockName": "빈코드", "marketValueRaw": "1"},
            {"itemCode": "5930", "stockName": "삼성전자", "marketValueRaw": "not-a-number", "marketValue": "1,000"},
            {"itemCode": "000660", "stockName": "SK하이닉스", "marketValueRaw": 200000000000},
        ]}
        with patch.object(naver._NAVER_SESSION, "get", return_value=res):
            rows = naver._fetch_kr_listing_naver("KOSPI", 100)
        self.assertEqual(rows, [
            {"Code": "005930", "Name": "삼성전자", "Marcap": 1000 * 100_000_000},
            {"Code": "000660", "Name": "SK하이닉스", "Marcap": 200000000000},
        ])

    def test_index_trend_day_with_a_bad_close_is_skipped(self):
        price = MagicMock()
        price.raise_for_status.return_value = None
        price.json.return_value = [{"localTradedAt": "2026-11-19", "closePrice": None},
                                   {"localTradedAt": "2026-11-18", "closePrice": "abc"},
                                   {"localTradedAt": "2026-11-17", "closePrice": "2,500.5"}]
        trend = MagicMock()
        trend.raise_for_status.return_value = None
        trend.json.return_value = {"foreignValue": "+10", "institutionalValue": "-5", "personalValue": "-5"}

        def get(url, timeout):
            return price if "/price" in url else trend

        with patch.object(naver._NAVER_SESSION, "get", side_effect=get):
            rows = naver._fetch_index_investor_trend("KOSPI", days=3)
        self.assertEqual([(r["Date"], r["Close"]) for r in rows], [("2026.11.19", 0), ("2026.11.17", 2500)])


class TestIndicatorEdges(unittest.TestCase):
    def _frame(self, closes):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(len(closes))]
        return pl.DataFrame({"Date": days, "Close": [float(c) for c in closes]})

    def test_flat_series_rsi_is_neutral(self):
        rsi = _compute_indicators(self._frame([100] * 30)).get_column("RSI14")
        self.assertEqual(rsi[-1], 50.0)

    def test_rising_and_falling_rsi_are_unchanged(self):
        self.assertEqual(_compute_indicators(self._frame(range(100, 130))).get_column("RSI14")[-1], 100.0)
        self.assertEqual(_compute_indicators(self._frame(range(130, 100, -1))).get_column("RSI14")[-1], 0.0)

    def test_nan_close_does_not_zero_the_ma_ratios(self):
        closes = [100.0] * 70
        closes[-3] = float("nan")
        df = self._frame(closes)
        with patch("data.indicators.datetime") as dt:
            dt.now.return_value = datetime(2026, 3, 11, 10, 0)    # the day after the last bar
            changes = fetch_historical_changes("X", 100.0, df)   # polars keeps NaN (pandas input would turn it into null)
        self.assertAlmostEqual(changes["ma20_div"], 100.0)
        self.assertAlmostEqual(changes["ma60_div"], 100.0)


# ── L2: VKOSPI wrapper and stats counters ────────────────────────────────────

class TestVkospiWrapper(unittest.TestCase):
    def setUp(self):
        self._saved = dict(krx._VKOSPI_PDF_CACHE)
        krx._VKOSPI_PDF_CACHE.update(df=None, checked_at=None)

    def tearDown(self):
        krx._VKOSPI_PDF_CACHE.update(self._saved)

    def test_stale_series_is_checked_once_per_ttl(self):
        df = pl.DataFrame({"Date": [date(2026, 11, 18)], "Close": [20.0]})
        with patch.object(krx, "fetch_vkospi_history", return_value=df) as fetch, \
                patch.object(krx, "_pdf_is_stale", return_value=True):
            first = krx._get_vkospi_pdf()
            second = krx._get_vkospi_pdf()
        self.assertEqual(fetch.call_count, 1)
        self.assertIs(first, second)


class TestHistCacheStats(unittest.TestCase):
    def test_counts_are_not_lost_under_threads(self):
        saved = dict(dcache._HIST_CACHE_STATS)
        dcache._HIST_CACHE_STATS.update(hits=0, misses=0)
        try:
            with patch.object(dcache, "_HIST_CACHE_LOG_INTERVAL", 10 ** 9):
                threads = [threading.Thread(target=lambda: [dcache._record_hist_cache_lookup(True) for _ in range(500)])
                           for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
            self.assertEqual(dcache._HIST_CACHE_STATS["hits"], 4000)
        finally:
            dcache._HIST_CACHE_STATS.update(saved)


if __name__ == "__main__":
    unittest.main()
