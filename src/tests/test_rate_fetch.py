"""tests/test_rate_fetch.py — JP10Y (MOF CSVs), KR3Y (Naver) and USD/KRW (Yahoo chart)
fetches: request timeouts, one lock per cache, and keeping the cached value on
network failures with a one-line warning."""
import threading
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from data import cache, fx
from data.collectors import krx, naver

_LOCKS = {"jp10y": cache._JP10Y_LOCK, "kr3y": cache._KR3Y_LOCK, "usd_krw": cache._USD_KRW_LOCK}

# MOF layout: a title line (skipped), then the header row.
_HIST_CSV = "Interest Rate\nDate,1Y,10Y\n2024/01/04,0.01,0.60\n2024/01/05,0.02,0.62\n"
_RECENT_CSV = "Interest Rate\nDate,1Y,10Y\n2024/01/09,0.02,0.65\n2024/01/10,0.03,-\n"

_CHART_JSON = {"chart": {"result": [{
    "timestamp": [1704412800, 1704672000],  # 2024-01-05, 2024-01-08 (UTC)
    "indicators": {
        "quote": [{"open": [1310.0, 1312.0], "high": [1315.0, 1318.0], "low": [1305.0, 1309.0],
                   "close": [1312.5, 1316.0], "volume": [0, 0]}],
        "adjclose": [{"adjclose": [1312.5, 1316.0]}],
    },
}]}}


def _csv_response(text):
    res = MagicMock()
    res.content = text.encode("shift_jis")
    res.raise_for_status.return_value = None
    return res


def _locks_held_except(own):
    """Names of the other caches' locks that are currently held."""
    held = []
    for name, lock in _LOCKS.items():
        if name == own:
            continue
        if lock.acquire(blocking=False):
            lock.release()
        else:
            held.append(name)
    return held


class TestLocks(unittest.TestCase):
    def test_each_cache_has_its_own_lock(self):
        locks = list(_LOCKS.values())
        self.assertEqual(len({id(lock) for lock in locks}), 3)
        self.assertFalse(hasattr(cache, "_MISC_CACHE_LOCK"))

    def test_slow_jp10y_fetch_does_not_block_usd_krw(self):
        started, release = threading.Event(), threading.Event()

        def slow_get(*_args, **_kwargs):
            started.set()
            release.wait(5)
            return _csv_response(_HIST_CSV)

        saved_jp, saved_usd = cache._JP10Y_CACHE["df"], dict(cache._USD_KRW_CACHE)
        cache._JP10Y_CACHE["df"] = None
        cache._USD_KRW_CACHE.update(rate=1400.0, df=None, fetched_at=None)
        try:
            with patch.object(krx._MOF_SESSION, "get", side_effect=slow_get):
                worker = threading.Thread(target=krx._get_jp10y_df)
                worker.start()
                self.assertTrue(started.wait(5))
                self.assertEqual(fx.get_usd_krw_rate(), 1400.0)  # cached, returns at once
                release.set()
                worker.join(5)
        finally:
            release.set()
            cache._JP10Y_CACHE["df"] = saved_jp
            cache._USD_KRW_CACHE.update(saved_usd)


class TestJp10yFetch(unittest.TestCase):
    def test_session_retries_connects_but_not_reads(self):
        retry = cache._MOF_SESSION.get_adapter(krx._JP10Y_BASE_URL).max_retries
        self.assertEqual((retry.connect, retry.read, retry.status), (3, 0, 0))

    def setUp(self):
        self._saved = cache._JP10Y_CACHE["df"]
        cache._JP10Y_CACHE["df"] = None

    def tearDown(self):
        cache._JP10Y_CACHE["df"] = self._saved

    @patch.object(krx._MOF_SESSION, "get")
    def test_merges_both_csvs_with_timeout(self, mock_get):
        mock_get.side_effect = [_csv_response(_HIST_CSV), _csv_response(_RECENT_CSV)]
        df = krx._get_jp10y_df()
        self.assertEqual(list(df["Close"]), [0.60, 0.62, 0.65])  # "-" row dropped
        self.assertEqual(df.index[-1], pd.Timestamp("2024-01-09"))
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], krx._JP10Y_TIMEOUT)
        self.assertIs(cache._JP10Y_CACHE["df"], df)

    @patch.object(krx._MOF_SESSION, "get")
    def test_network_failure_keeps_cache_and_logs_one_line(self, mock_get):
        cached = pd.DataFrame({"Close": [0.5]}, index=pd.to_datetime(["2024-01-01"]))
        cache._JP10Y_CACHE["df"] = cached
        mock_get.side_effect = requests.ConnectTimeout("timed out")
        with patch("data.collectors.krx._pdf_is_stale", return_value=True), \
                self.assertLogs("data.collectors.krx", level="WARNING") as logs:
            result = krx._get_jp10y_df()
        self.assertIs(result, cached)
        self.assertEqual([(r.levelname, r.exc_info) for r in logs.records], [("WARNING", None)])

    @patch.object(krx._MOF_SESSION, "get")
    def test_fetch_holds_only_its_own_lock(self, mock_get):
        held = []

        def fake_get(*_args, **_kwargs):
            held.append(_locks_held_except("jp10y"))
            return _csv_response(_HIST_CSV)

        mock_get.side_effect = fake_get
        krx._get_jp10y_df()
        self.assertEqual(held, [[], []])


class TestKr3yFetch(unittest.TestCase):
    """The Naver endpoint is disabled (_KR3Y_API_DISABLED); these run the fetch
    path with the flag patched off."""

    def setUp(self):
        self._saved = cache._KR3Y_CACHE["df"]
        cache._KR3Y_CACHE["df"] = None
        patcher = patch("data.collectors.naver._KR3Y_API_DISABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        cache._KR3Y_CACHE["df"] = self._saved

    def test_http_error_keeps_cache_and_logs_one_line(self):
        cached = pd.DataFrame({"Close": [3.1]}, index=pd.to_datetime(["2024-01-01"]))
        cache._KR3Y_CACHE["df"] = cached
        gone = MagicMock()
        gone.raise_for_status.side_effect = requests.HTTPError("410 Client Error: Gone")
        with patch.object(naver._NAVER_SESSION, "get", return_value=gone), \
                patch("data.collectors.naver._pdf_is_stale", return_value=True), \
                self.assertLogs("data.collectors.naver", level="WARNING") as logs:
            result = naver._get_kr3y_df()
        self.assertIs(result, cached)
        self.assertEqual([(r.levelname, r.exc_info) for r in logs.records], [("WARNING", None)])

    def test_fetch_holds_only_its_own_lock(self):
        held = []
        page = MagicMock()
        page.raise_for_status.return_value = None
        page.text = ("<table><tr><th>Date</th><th>Close</th></tr>"
                     "<tr><td>2024.01.05</td><td>3.25</td></tr></table>")

        def fake_get(*_args, **_kwargs):
            held.append(_locks_held_except("kr3y"))
            return page

        with patch.object(naver._NAVER_SESSION, "get", side_effect=fake_get):
            df = naver._get_kr3y_df()
        self.assertEqual(df["Close"].iloc[-1], 3.25)
        self.assertTrue(held)
        self.assertTrue(all(h == [] for h in held))


class TestUsdKrwFetch(unittest.TestCase):
    def setUp(self):
        self._saved = dict(cache._USD_KRW_CACHE)
        cache._USD_KRW_CACHE.update(rate=None, df=None, fetched_at=None)

    def tearDown(self):
        cache._USD_KRW_CACHE.update(self._saved)

    @patch("data.fx.requests.get")
    def test_parses_chart_with_timeout(self, mock_get):
        held = []
        res = MagicMock()
        res.raise_for_status.return_value = None
        res.json.return_value = _CHART_JSON

        def fake_get(*_args, **_kwargs):
            held.append(_locks_held_except("usd_krw"))
            return res

        mock_get.side_effect = fake_get
        self.assertEqual(fx.get_usd_krw_rate(), 1316.0)
        self.assertEqual(mock_get.call_args.kwargs["timeout"], fx._USD_KRW_TIMEOUT)
        self.assertEqual(held, [[]])
        self.assertEqual(fx.get_usd_krw_rate_for_date("2024-01-06"), 1312.5)

    @patch("data.fx.requests.get")
    def test_network_failure_uses_cached_rate_and_logs_one_line(self, mock_get):
        cache._USD_KRW_CACHE["rate"] = 1390.0
        mock_get.side_effect = requests.ConnectTimeout("timed out")
        with patch("data.fx._hist_df_is_stale", return_value=True), \
                self.assertLogs("data.fx", level="WARNING") as logs:
            rate = fx.get_usd_krw_rate()
        self.assertEqual(rate, 1390.0)
        self.assertEqual([(r.levelname, r.exc_info) for r in logs.records], [("WARNING", None)])


if __name__ == "__main__":
    unittest.main()
