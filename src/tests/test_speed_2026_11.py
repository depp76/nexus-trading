"""tests/test_speed_2026_11.py — the 2026-11-16 speed pass (roadmap.md):
one shared TLS context per session, the lazy FinanceDataReader import, the
JP10Y incremental download and re-fetch window, indices fetched alongside the
markets and investor-flow concurrency (the Total Assets inline chart this pass
first deferred was removed the same day). No network."""
import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

from data import cache
from data.collectors import krx


class TestSharedTlsAdapter(unittest.TestCase):
    def _attrs(self, adapter, url, verify=True):
        req = requests.Request("GET", url).prepare()
        return adapter.build_connection_pool_key_attributes(req, verify)

    def test_every_https_connection_gets_the_one_context_and_no_bundle_path(self):
        adapter = cache.SharedTlsAdapter()
        _, kw1 = self._attrs(adapter, "https://a.example/x")
        _, kw2 = self._attrs(cache.SharedTlsAdapter(), "https://b.example/y")
        self.assertIs(kw1["ssl_context"], kw2["ssl_context"])
        self.assertNotIn("ca_certs", kw1)
        self.assertEqual(kw1["cert_reqs"], "CERT_REQUIRED")
        ctx = kw1["ssl_context"]
        self.assertTrue(ctx.check_hostname)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)       # the CA bundle is loaded

    def test_connections_are_not_handed_the_bundle_path(self):
        """requests' own adapter gives every connection the certifi path, which
        makes urllib3 read the bundle again per connection; this one does not."""
        class Conn:
            ca_certs = ca_cert_dir = cert_reqs = None
        plain, shared = Conn(), Conn()
        HTTPAdapter().cert_verify(plain, "https://a.example/x", True, None)
        cache.SharedTlsAdapter().cert_verify(shared, "https://a.example/x", True, None)
        self.assertTrue(plain.ca_certs)
        self.assertIsNone(shared.ca_certs)
        self.assertEqual(shared.cert_reqs, "CERT_REQUIRED")
        custom = Conn()
        cache.SharedTlsAdapter().cert_verify(custom, "https://a.example/x", plain.ca_certs, None)
        self.assertEqual(custom.ca_certs, plain.ca_certs)        # an explicit bundle path is kept

    def test_custom_verify_is_left_to_requests(self):
        adapter = cache.SharedTlsAdapter()
        _, off = self._attrs(adapter, "https://a.example/x", verify=False)
        self.assertNotIn("ssl_context", off)
        self.assertEqual(off["cert_reqs"], "CERT_NONE")
        _, http = self._attrs(adapter, "http://a.example/x")
        self.assertNotIn("ssl_context", http)

    def test_yahoo_has_its_own_context(self):
        self.assertIsNot(cache.YFTlsAdapter.shared_context(), cache.SharedTlsAdapter.shared_context())
        self.assertIs(cache.YFTlsAdapter.shared_context(), cache.YFTlsAdapter.shared_context())

    def test_sessions_use_it(self):
        for session in (cache._NAVER_SESSION, cache._MOF_SESSION, cache._KIS_SESSION, cache._YF_SESSION):
            self.assertIsInstance(session.get_adapter("https://x.example/"), cache.SharedTlsAdapter)
        retry = cache._MOF_SESSION.get_adapter("https://x.example/").max_retries
        self.assertEqual((retry.connect, retry.read), (3, 0))


class TestLazyModule(unittest.TestCase):
    def test_imports_on_first_attribute_only(self):
        lazy = cache.LazyModule("json")
        self.assertIsNone(lazy.__dict__["_lazy_mod"])
        self.assertEqual(lazy.dumps([1]), "[1]")
        import json
        self.assertIs(lazy.__dict__["_lazy_mod"], json)

    def test_fdr_is_lazy_in_every_data_module(self):
        from data import history, listing, market
        for mod in (history, listing, market):
            self.assertIs(mod.fdr, cache.fdr)
        self.assertIsInstance(cache.fdr, cache.LazyModule)


def _csv(text):
    res = MagicMock()
    res.content = text.encode("shift_jis")
    res.raise_for_status.return_value = None
    return res


_HIST = "Interest Rate\nDate,1Y,10Y\n2024/01/04,0.01,0.60\n2024/01/05,0.02,0.62\n"
_RECENT = "Interest Rate\nDate,1Y,10Y\n2024/02/01,0.02,0.65\n2024/02/02,0.03,0.66\n"


class TestJp10yIncremental(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        p = patch("data.collectors.krx.JP10Y_CACHE_FILE", os.path.join(self._tmp.name, "cache", "jp10y.json"))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        saved = dict(cache._JP10Y_CACHE)
        cache._JP10Y_CACHE.clear()
        cache._JP10Y_CACHE["df"] = None

        def restore():
            cache._JP10Y_CACHE.clear()
            cache._JP10Y_CACHE.update(saved)
        self.addCleanup(restore)
        stale = patch("data.collectors.krx._pdf_is_stale", return_value=True)   # a weekday, no bar for today
        stale.start()
        self.addCleanup(stale.stop)

    @staticmethod
    def _frame(rows):
        df = pd.DataFrame({"Close": [v for _, v in rows]}, index=pd.to_datetime([d for d, _ in rows]))
        df.index.name = "Date"
        return df

    @patch.object(krx._MOF_SESSION, "get")
    def test_cache_inside_the_current_month_downloads_only_the_small_csv(self, get):
        cache._JP10Y_CACHE["df"] = self._frame([("2024-01-05", 0.62), ("2024-02-01", 0.99)])
        get.side_effect = [_csv(_RECENT)]
        df = krx._get_jp10y_df()
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith(krx._JP10Y_CSV_PATHS[1]))
        # earlier rows kept, the current month replaced by the fresh CSV
        self.assertEqual(list(df["Close"]), [0.62, 0.65, 0.66])
        self.assertTrue(df.index.is_monotonic_increasing and df.index.is_unique)

    @patch.object(krx._MOF_SESSION, "get")
    def test_cache_from_an_earlier_month_downloads_the_history_too(self, get):
        cache._JP10Y_CACHE["df"] = self._frame([("2024-01-04", 0.60)])
        get.side_effect = [_csv(_RECENT), _csv(_HIST)]
        df = krx._get_jp10y_df()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(list(df["Close"]), [0.60, 0.62, 0.65, 0.66])

    @patch.object(krx._MOF_SESSION, "get")
    def test_a_fresh_download_is_not_repeated_within_the_window(self, get):
        get.side_effect = [_csv(_HIST), _csv(_RECENT), AssertionError("downloaded again")]
        first = krx._get_jp10y_df()
        self.assertIs(krx._get_jp10y_df(), first)
        self.assertEqual(get.call_count, 2)
        # after the window it looks again (only the small CSV: the cache covers the month)
        frame, when = cache._JP10Y_CACHE["fetched"]
        cache._JP10Y_CACHE["fetched"] = (frame, when - krx._JP10Y_REFETCH_TTL - dt.timedelta(seconds=1))
        get.side_effect = [_csv(_RECENT)]
        again = krx._get_jp10y_df()
        self.assertEqual(get.call_count, 3)
        self.assertEqual(list(again["Close"]), list(first["Close"]))


class TestIndicesRunAlongsideMarkets(unittest.TestCase):
    def test_indices_do_not_hold_the_markets_back_and_come_first_in_the_rows(self):
        from threads.fetch_threads import AllDataFetchThread
        started = {}
        market_started = threading.Event()

        def slow_indices():
            started["Indices"] = time.perf_counter()
            # the old code finished this step before any market began
            self.assertTrue(market_started.wait(5), "markets did not start while the index step ran")
            return [{"ticker": "^KS11", "market": "Index"}]

        def market(m, n, cb):
            started[m] = time.perf_counter()
            market_started.set()
            return [{"ticker": m, "market": m}]

        t = AllDataFetchThread()
        done, loaded, failed = [], [], []
        t.finished_all.connect(done.append)
        t.market_loaded.connect(lambda part, rows: loaded.append(part))
        t.failed.connect(lambda reason, parts: failed.append(parts))
        with patch("data_fetcher.fetch_major_indices_as_stocks", side_effect=slow_indices), \
             patch("threads.fetch_threads.fetch_market_data", side_effect=market):
            t.run()
        self.assertEqual([x["ticker"] for x in done[0]], ["^KS11", "KOSPI", "KOSDAQ"])   # indices first
        self.assertEqual(done[0][0]["market"], "Index")
        self.assertEqual(sorted(loaded), ["Indices", "KOSDAQ", "KOSPI"])
        self.assertEqual(failed, [])

    def test_a_failed_index_step_keeps_the_markets(self):
        from threads.fetch_threads import AllDataFetchThread
        t = AllDataFetchThread()
        done, failed = [], []
        t.finished_all.connect(done.append)
        t.failed.connect(lambda reason, parts: failed.append(parts))
        with patch("data_fetcher.fetch_major_indices_as_stocks", side_effect=RuntimeError("down")), \
             patch("threads.fetch_threads.fetch_market_data",
                   side_effect=lambda m, n, cb: [{"ticker": m, "market": m}]), \
             self.assertLogs("threads.fetch_threads", level="WARNING"):
            t.run()
        self.assertEqual([x["ticker"] for x in done[0]], ["KOSPI", "KOSDAQ"])
        self.assertEqual(failed, [["Indices"]])


class TestFlowConcurrency(unittest.TestCase):
    def test_dataset_loads_flows_at_the_fetch_concurrency(self):
        from data import flows
        from strategy.trend_following import dataset
        self.assertEqual(flows.FLOW_FETCH_CONCURRENCY, 8)
        peak, live, lock = [0], [0], threading.Lock()
        gate = threading.Barrier(flows.FLOW_FETCH_CONCURRENCY, timeout=5)

        def fake(code, ws):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                gate.wait()                      # passes only when 8 calls are in flight together
            except threading.BrokenBarrierError:
                pass
            with lock:
                live[0] -= 1
            return None

        with patch("strategy.trend_following.dataset.get_investor_flows", side_effect=fake):
            out = dataset._load_flows([f"{i:06d}" for i in range(16)], "2021-01-01", dt.date(2024, 12, 30), 8,
                                      None, None)
        self.assertEqual(out, {})
        self.assertEqual(peak[0], flows.FLOW_FETCH_CONCURRENCY)


class TestAssetsTabHasNoInlineChart(unittest.TestCase):
    """The inline trend chart (and its deferred draw from this pass) went the
    same day: the 그래프 button opens the graph, the table takes the height."""

    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        records = [{"date": "2026-01-02", "total": 1000.0, "manual": True},
                   {"date": "2026-01-09", "total": 1100.0, "manual": True}]
        for target, kw in (("ui.assets_tab.trade_db.load_asset_records", {"return_value": records}),
                           ("ui.assets_tab.trade_db.save_asset_records", {}),
                           ("ui.assets_tab.TradingRecordTab._start_metrics_preload", {})):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_no_canvas_and_the_table_fills_the_tab(self):
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from PyQt6.QtWidgets import QSplitter
        from ui.assets_tab import TradingRecordTab
        tab = TradingRecordTab()
        self.assertEqual(tab.findChildren(FigureCanvasQTAgg), [])
        self.assertEqual(tab.findChildren(QSplitter), [])
        self.assertFalse(hasattr(tab, "_chart_canvas"))
        tab.resize(1200, 800)
        tab.show()
        self.addCleanup(tab.close)
        self.app.processEvents()
        self.assertGreater(tab._table.height(), 600)
        self.assertEqual(tab._table.rowCount(), 2)
        with patch("ui.assets_tab.TotalAssetsGraphDialog") as dlg:
            tab._metrics_ready = True
            tab._show_graph()
        dlg.assert_called_once()


if __name__ == "__main__":
    unittest.main()
