"""tests/test_listing_cache.py — data.listing._singleflight_cache: day-scoped
memoisation, concurrent de-duplication, LRU bound and cache_clear()."""
import threading
import time
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd

import data.listing as listing


def _is_partial(result):
    return result == "partial"


def _cached(maxsize=16):
    calls = []

    @listing._singleflight_cache(maxsize=maxsize)
    def fn(key):
        calls.append(key)
        return f"result-{key}"

    return fn, calls


class TestSingleflightCache(unittest.TestCase):

    def test_second_call_hits_cache(self):
        fn, calls = _cached()
        self.assertEqual(fn("A"), "result-A")
        self.assertEqual(fn("A"), "result-A")
        self.assertEqual(calls, ["A"])

    def test_entries_expire_at_day_boundary(self):
        fn, calls = _cached()
        with patch("data.listing.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 18, 23, 0)
            fn("A")
            fn("A")
            mock_dt.now.return_value = datetime(2026, 9, 19, 0, 1)
            fn("A")
        self.assertEqual(calls, ["A", "A"])

    def test_lru_bound_evicts_oldest(self):
        fn, calls = _cached(maxsize=2)
        fn("A"); fn("B")
        fn("A")            # A is now most recent
        fn("C")            # evicts B
        fn("A")            # still cached
        fn("B")            # recomputed
        self.assertEqual(calls, ["A", "B", "C", "B"])

    def test_cache_clear_forces_recompute(self):
        fn, calls = _cached()
        fn("A")
        fn.cache_clear()
        fn("A")
        self.assertEqual(calls, ["A", "A"])

    def test_concurrent_callers_share_one_computation(self):
        calls = []
        release = threading.Event()

        @listing._singleflight_cache(maxsize=4)
        def slow(key):
            calls.append(key)
            release.wait(2.0)
            return f"result-{key}"

        results = []

        def worker():
            results.append(slow("X"))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        # Let every thread reach either the computation or the wait-on-lock.
        time.sleep(0.2)
        release.set()
        for t in threads:
            t.join(3.0)

        self.assertEqual(results, ["result-X"] * 5)
        self.assertEqual(calls, ["X"], "only one thread should have computed the key")

    def test_distinct_keys_do_not_block_each_other(self):
        started = {}
        release = threading.Event()

        @listing._singleflight_cache(maxsize=4)
        def slow(key):
            started[key] = True
            release.wait(2.0)
            return key

        threads = [threading.Thread(target=slow, args=(k,)) for k in ("A", "B")]
        for t in threads:
            t.start()
        time.sleep(0.2)
        self.assertEqual(set(started), {"A", "B"}, "second key must not wait on the first key's lock")
        release.set()
        for t in threads:
            t.join(3.0)


class TestProvisionalEntries(unittest.TestCase):

    def _run(self, result, seconds_later):
        calls = []

        @listing._singleflight_cache(maxsize=4, provisional=_is_partial)
        def fn(key):
            calls.append(key)
            return result

        with patch("data.listing.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            fn("A")
            mock_time.monotonic.return_value = 1000.0 + seconds_later
            fn("A")
        return len(calls)

    def test_provisional_entry_stays_cached_within_ttl(self):
        self.assertEqual(self._run("partial", listing._PROVISIONAL_TTL - 1), 1)

    def test_provisional_entry_expires_after_ttl(self):
        self.assertEqual(self._run("partial", listing._PROVISIONAL_TTL), 2)

    def test_complete_entry_ignores_ttl(self):
        self.assertEqual(self._run("complete", listing._PROVISIONAL_TTL * 10), 1)

    def test_listing_predicate(self):
        nan = float("nan")
        self.assertTrue(listing._listing_is_provisional(pd.DataFrame(columns=["Code", "Name"])))
        self.assertTrue(listing._listing_is_provisional(pd.DataFrame({"Code": ["A"], "Marcap": [nan]})))
        self.assertFalse(listing._listing_is_provisional(pd.DataFrame({"Code": ["A", "B"], "Marcap": [1.0, nan]})))
        # KRX-DESC and ETF/KR have no Marcap column and are complete without one.
        self.assertFalse(listing._listing_is_provisional(pd.DataFrame({"Code": ["A"], "Name": ["a"]})))


def _response(status=200, text=""):
    r = MagicMock(status_code=status, text=text)
    if status >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return r


class TestFetchRecentMarcaps(unittest.TestCase):

    TODAY = date(2026, 11, 9)  # a Monday

    def _fetch(self, by_date):
        def get(url, timeout):
            return by_date.get(url.rsplit("/", 1)[1][:-4], _response(404))

        with patch("data.listing.requests.get", side_effect=get) as mock_get:
            return listing._fetch_recent_marcaps(self.TODAY), mock_get

    def test_skips_empty_and_missing_files(self):
        result, mock_get = self._fetch({
            "2026-11-08": _response(text="Code,Marcap,Name\n5930,,x\n"),  # weekend file
            "2026-11-06": _response(text="Code,Marcap,Name\n5930,100,x\n660,,y\n"),
        })
        self.assertEqual(result, ("2026-11-06", {"005930": 100.0}))
        self.assertEqual(mock_get.call_count, 3)

    def test_nothing_within_lookback(self):
        result, mock_get = self._fetch({})
        self.assertEqual(result, (None, {}))
        self.assertEqual(mock_get.call_count, listing._MARCAP_LOOKBACK_DAYS)

    def test_download_failure_stops_the_walk(self):
        result, mock_get = self._fetch({"2026-11-08": _response(503)})
        self.assertEqual(result, (None, {}))
        self.assertEqual(mock_get.call_count, 1)


class TestFdrFallbackRanking(unittest.TestCase):

    NAN = float("nan")

    def _rank(self, tables, recent=(None, {}), top_n=2):
        with patch.object(listing, "get_stock_listing", side_effect=lambda m: tables[m]), \
                patch.object(listing, "_fetch_recent_marcaps", return_value=recent) as mock_recent:
            return listing._fetch_kr_listing_fdr_fallback("KOSPI", top_n), mock_recent

    def _frame(self, marcaps):
        return pd.DataFrame({
            "Code": ["000001", "000002", "000003"], "Name": ["a", "b", "c"],
            "Market": ["KOSPI"] * 3, "Marcap": marcaps,
        })

    def test_todays_market_caps_need_no_lookup(self):
        rows, mock_recent = self._rank({"KOSPI": self._frame([1.0, 3.0, 2.0])})
        self.assertEqual([r["Code"] for r in rows], ["000002", "000003"])
        mock_recent.assert_not_called()

    def test_empty_market_caps_are_ranked_from_earlier_file(self):
        recent = ("2026-11-05", {"000001": 5.0, "000003": 9.0})
        rows, _ = self._rank({"KOSPI": self._frame([self.NAN] * 3)}, recent)
        self.assertEqual([(r["Code"], r["Marcap"]) for r in rows], [("000003", 9.0), ("000001", 5.0)])

    def test_desc_listing_is_ranked_from_earlier_file(self):
        desc = pd.DataFrame({"Code": ["000001", "000002"], "Name": ["a", "b"], "Market": ["KOSPI"] * 2})
        empty = pd.DataFrame(columns=["Symbol", "Code", "Name", "Market"])
        rows, _ = self._rank({"KOSPI": empty, "KRX-DESC": desc}, ("2026-11-05", {"000002": 7.0}))
        self.assertEqual([r["Code"] for r in rows], ["000002", "000001"])

    def test_no_market_caps_anywhere_returns_nothing(self):
        rows, _ = self._rank({"KOSPI": self._frame([self.NAN] * 3)})
        self.assertEqual(rows, [])

    def test_earlier_file_without_these_codes_returns_nothing(self):
        rows, _ = self._rank({"KOSPI": self._frame([self.NAN] * 3)}, ("2026-11-05", {"999999": 1.0}))
        self.assertEqual(rows, [])


class TestGetStockListingFallback(unittest.TestCase):

    def setUp(self):
        listing.get_stock_listing.cache_clear()
        listing._get_listing_with_norm.cache_clear()

    @patch("data.listing.fdr")
    def test_fdr_failure_returns_empty_frame_with_expected_columns(self, mock_fdr):
        mock_fdr.StockListing.side_effect = Exception("offline")
        df = listing.get_stock_listing("ETF/KR")
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), ["Symbol", "Code", "Name", "Market"])

    @patch("data.listing.fdr")
    def test_krx_markets_use_fdr_stock_listing_only(self, mock_fdr):
        for market in ("KRX-DESC", "KRX", "KOSPI", "KOSDAQ"):
            expected = pd.DataFrame({"Code": ["005930"], "Name": [market]})
            mock_fdr.StockListing.return_value = expected
            self.assertIs(listing.get_stock_listing(market), expected)
            mock_fdr.StockListing.assert_called_with(market)

    @patch("data.listing.fdr")
    def test_listing_with_norm_strips_spaces_and_uppercases(self, mock_fdr):
        mock_fdr.StockListing.return_value = pd.DataFrame(
            {"Symbol": ["069500"], "Name": ["kodex 200_tr"], "Market": ["ETF"]}
        )
        df = listing._get_listing_with_norm("ETF/KR")
        self.assertEqual(df["NameNorm"].iloc[0], "KODEX200TR")
        # Cached: the underlying listing is fetched once even though two caches sit above it.
        listing._get_listing_with_norm("ETF/KR")
        self.assertEqual(mock_fdr.StockListing.call_count, 1)

    @patch("data.listing.fdr")
    def test_listing_without_market_caps_is_refetched_after_ttl(self, mock_fdr):
        mock_fdr.StockListing.return_value = pd.DataFrame(
            {"Code": ["005930"], "Name": ["a"], "Marcap": [float("nan")]}
        )
        with patch("data.listing.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            listing.get_stock_listing("KOSPI")
            listing.get_stock_listing("KOSPI")
            self.assertEqual(mock_fdr.StockListing.call_count, 1)
            mock_time.monotonic.return_value = 1000.0 + listing._PROVISIONAL_TTL
            listing.get_stock_listing("KOSPI")
        self.assertEqual(mock_fdr.StockListing.call_count, 2)


if __name__ == "__main__":
    unittest.main()
