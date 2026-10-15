"""Naver mobile ``/api/stock/<code>/trend`` parsing and bizdate paging (NEX-23),
offline: the fixture is a trimmed real response (005930, rows before 2023-01-03)."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import data.collectors.naver as naver

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "naver_trend_005930.json")


def _items():
    with open(_FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _response(items):
    res = MagicMock()
    res.json.return_value = items
    res.raise_for_status.return_value = None
    return res


class TestParseTrendRows(unittest.TestCase):
    def test_signed_comma_numbers_and_date_format(self):
        rows = naver._parse_trend_rows(_items())
        self.assertEqual(len(rows), 5)
        first = rows[0]
        self.assertEqual(first["Date"], "2023.01.02")
        self.assertEqual(first["Close"], 55500)
        self.assertEqual(first["Foreigner"], -609433)
        self.assertEqual(first["Institution"], -478123)
        self.assertEqual(first["Retail"], 1062403)   # the API's own individual figure

    def test_retail_is_not_derived_from_the_other_two(self):
        first = naver._parse_trend_rows(_items())[0]
        self.assertNotEqual(first["Retail"], -(first["Foreigner"] + first["Institution"]))

    def test_zero_volume_and_malformed_items_are_dropped(self):
        items = _items()
        items[1]["accumulatedTradingVolume"] = "0"
        del items[2]["bizdate"]
        rows = naver._parse_trend_rows(items)
        self.assertEqual([r["Date"] for r in rows], ["2023.01.02", "2022.12.27", "2022.12.26"])

    def test_empty_quantity_counts_as_zero(self):
        items = _items()[:1]
        items[0]["organPureBuyQuant"] = ""
        self.assertEqual(naver._parse_trend_rows(items)[0]["Institution"], 0)


class TestFetchInvestorTrend(unittest.TestCase):
    def test_pages_backwards_by_bizdate_and_trims_to_days(self):
        items = _items()
        pages = [_response(items[:3]), _response(items[3:])]
        with patch.object(naver, "_TREND_PAGE_SIZE", 3), \
                patch.object(naver._NAVER_SESSION, "get", side_effect=pages) as get:
            rows = naver._fetch_investor_trend_naver("5930", days=4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args_list[0].args[0].endswith("/005930/trend"))
        self.assertNotIn("bizdate", get.call_args_list[0].kwargs["params"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["bizdate"], "20221228")

    def test_short_page_ends_the_walk(self):
        with patch.object(naver._NAVER_SESSION, "get", return_value=_response(_items())) as get:
            rows = naver._fetch_investor_trend_naver("005930", days=500)
        self.assertEqual(len(rows), 5)
        self.assertEqual(get.call_count, 1)

    def test_http_error_returns_what_was_collected(self):
        with patch.object(naver._NAVER_SESSION, "get", side_effect=RuntimeError("down")):
            self.assertEqual(naver._fetch_investor_trend_naver("005930", days=10), [])


if __name__ == "__main__":
    unittest.main()
