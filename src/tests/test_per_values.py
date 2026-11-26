"""tPER / fPER sources keep a loss-making company's negative PER (2026-11-20):
Naver's 추정PER sign, Naver's "N/A" PER filled from the negative EPS, Yahoo's
missing trailingPE filled from epsTrailingTwelveMonths."""
import unittest
from unittest.mock import MagicMock, patch


def _naver_reply(infos):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"totalInfos": infos}
    return r


class TestPerFromEps(unittest.TestCase):
    def test_signed_and_rounded(self):
        from data.cache import per_from_eps
        self.assertEqual(per_from_eps(11.29, -2.16), -5.2)
        self.assertEqual(per_from_eps(183200, 31809), 5.8)

    def test_missing_or_zero_is_none(self):
        from data.cache import per_from_eps
        for price, eps in ((None, -1.0), (10.0, None), (10.0, 0), (0, -1.0), (float("nan"), -1.0)):
            self.assertIsNone(per_from_eps(price, eps), (price, eps))


class TestNaverPer(unittest.TestCase):
    def test_signed_number_parsing(self):
        from data.collectors.naver import _naver_signed_number
        self.assertEqual(_naver_signed_number("-9.82배"), -9.82)
        self.assertEqual(_naver_signed_number("-1,469원"), -1469.0)
        self.assertEqual(_naver_signed_number("31,809원"), 31809.0)
        self.assertEqual(_naver_signed_number("5.76배"), 5.76)
        for empty in ("N/A", "-", "", None):
            self.assertIsNone(_naver_signed_number(empty))

    def test_loss_maker_keeps_negative_forward_per_and_eps(self):
        from data.collectors import naver
        infos = [
            {"code": "per", "key": "PER", "value": "N/A"},
            {"code": "eps", "key": "EPS", "value": "-1,469원"},
            {"code": "cnsPer", "key": "추정PER", "value": "-9.82배"},
            {"code": "cnsEps", "key": "추정EPS", "value": "-1,308원"},
        ]
        with patch.object(naver._NAVER_SESSION, "get", return_value=_naver_reply(infos)):
            self.assertEqual(naver._fetch_naver_per_single("293490"),
                             ("293490", {"tper": None, "fper": -9.8, "eps": -1469.0, "feps": -1308.0}))

    def test_key_labels_are_the_fallback(self):
        from data.collectors import naver
        infos = [{"key": "PER", "value": "5.76배"}, {"key": "추정PER", "value": "4.02배"}]
        with patch.object(naver._NAVER_SESSION, "get", return_value=_naver_reply(infos)):
            self.assertEqual(naver._fetch_naver_per_single("005930"),
                             ("005930", {"tper": 5.8, "fper": 4.0, "eps": None, "feps": None}))

    def test_assembled_row_fills_trailing_per_from_negative_eps(self):
        from data.market import _assemble_kr_results
        listing = [{"Code": "293490"}, {"Code": "005930"}]
        hist = {"293490": ("A", 1, 12000.0, {}), "005930": ("B", 1, 183200.0, {})}
        per = {"293490": {"tper": None, "fper": -9.8, "eps": -1469.0, "feps": -1308.0},
               "005930": {"tper": 5.8, "fper": 4.0, "eps": 31809.0, "feps": 45565.0}}
        rows = _assemble_kr_results(listing, hist, "KOSPI", per)
        by = {r["ticker"]: r for r in rows}
        self.assertEqual(by["293490"]["trailing_per"], -8.2)
        self.assertEqual(by["293490"]["forward_per"], -9.8)
        self.assertEqual(by["293490"]["eps_forward"], -1308.0)
        self.assertEqual(by["005930"]["trailing_per"], 5.8)   # Naver's own value wins
        self.assertEqual(by["005930"]["eps_trailing"], 31809.0)


class TestYahooPer(unittest.TestCase):
    def test_v7_missing_trailing_pe_comes_from_negative_eps(self):
        from data.collectors import yahoo
        quotes = {
            "RIVN": {"marketCap": 1, "regularMarketPrice": 11.29, "trailingPE": None,
                     "forwardPE": -6.4057, "epsTrailingTwelveMonths": -2.16},
            "AAPL": {"marketCap": 1, "regularMarketPrice": 349.21, "trailingPE": 39.237,
                     "forwardPE": 33.03, "epsTrailingTwelveMonths": 8.9},
        }
        meta = yahoo._UsChunkMeta()
        with patch.object(yahoo, "yf_quote_batch", return_value=quotes):
            yahoo._apply_v7_quotes(list(quotes), meta)
        self.assertEqual(meta.trailing_per, {"RIVN": -5.2, "AAPL": 39.2})
        self.assertEqual(meta.forward_per, {"RIVN": -6.4, "AAPL": 33.0})
        self.assertEqual(meta.eps_trailing, {"RIVN": -2.16, "AAPL": 8.9})

    def test_yfinance_info_fallback(self):
        from data.market import _per_from_info
        self.assertEqual(_per_from_info({"currentPrice": 55.72, "trailingEps": -2.4, "forwardPE": 31.12}, "INTC"),
                         (-23.2, 31.1))


class TestPerFollowsPrice(unittest.TestCase):
    """The 60 s price update recomputes PER as price / the stored EPS (2026-11-20)."""

    def test_refresh_row_per(self):
        from data.cache import refresh_row_per
        kr = {"price": 190000.0, "trailing_per": 5.8, "forward_per": 4.0,
              "eps_trailing": 31809.0, "eps_forward": 45565.0}
        refresh_row_per(kr)
        self.assertEqual((kr["trailing_per"], kr["forward_per"]), (6.0, 4.2))
        us = {"currency": "$", "price": 500000.0, "usd_price": 360.0, "trailing_per": 39.2,
              "eps_trailing": 8.9, "eps_forward": None, "forward_per": 33.0}
        refresh_row_per(us)
        self.assertEqual((us["trailing_per"], us["forward_per"]), (40.4, 33.0))   # USD basis; no fwd EPS kept
        old = {"price": 100.0, "trailing_per": 10.0}                               # cached before EPS was stored
        refresh_row_per(old)
        self.assertEqual(old["trailing_per"], 10.0)

    def _run(self, rows, kr=None, us=None):
        from threads.fetch_threads import UniverseLightweightFetchThread
        done = []
        thread = UniverseLightweightFetchThread(rows)
        thread.finished_all.connect(done.append)
        with patch("data_fetcher.fetch_naver_realtime_prices", return_value=kr or {}),              patch("data_fetcher.fetch_us_realtime_prices", return_value=us or {}),              patch("data_fetcher.fetch_historical_changes", return_value={}):
            thread.run()
        return {r["ticker"]: r for r in done[0]}

    def test_lightweight_tick_moves_per_with_the_price(self):
        rows = [{"ticker": "005930", "market": "KOSPI", "price": 183200.0, "currency": "₩",
                 "trailing_per": 5.8, "forward_per": 4.0, "eps_trailing": 31809.0, "eps_forward": 45565.0,
                 "changes": {}}]
        out = self._run(rows, kr={"005930": 200000.0})
        self.assertEqual((out["005930"]["trailing_per"], out["005930"]["forward_per"]), (6.3, 4.4))

    def test_lightweight_tick_updates_the_us_dollar_price(self):
        """The USD quote goes to usd_price (shown in the Price cell) and price stays KRW."""
        rows = [{"ticker": "AAPL", "market": "NASDAQ 150", "price": 349.21 * 1400, "usd_price": 349.21,
                 "currency": "$", "trailing_per": 39.2, "forward_per": 33.0,
                 "eps_trailing": 8.9, "eps_forward": 10.57273, "changes": {}}]
        out = self._run(rows, us={"AAPL": 356.0})["AAPL"]
        self.assertEqual(out["usd_price"], 356.0)
        self.assertAlmostEqual(out["price"], 356.0 * 1400)
        self.assertEqual((out["trailing_per"], out["forward_per"]), (40.0, 33.7))


class TestPerCellColor(unittest.TestCase):
    def test_negative_tper_is_warning_not_cheap(self):
        from PyQt6.QtGui import QColor
        from ui.colors import OK, WARN
        from ui.universe_columns import COL_TPER
        from ui.universe_table import StockTable
        table = StockTable()
        table.setRowCount(3)
        for row, val in enumerate((-8.2, 5.0, 80.0)):
            table._set_per_cells(row, {"trailing_per": val, "forward_per": None})
        colors = [table.item(r, COL_TPER).foreground().color() for r in range(3)]
        self.assertEqual(colors, [QColor(WARN), QColor(OK), QColor(WARN)])
        self.assertEqual(table.item(0, COL_TPER).text(), "-8.2")


if __name__ == "__main__":
    unittest.main()
