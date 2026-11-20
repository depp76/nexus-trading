"""tests/test_chart_ma_warmup.py — docs/ma60_unification_2026-11-18.md step
[1a]: the MA chart's MA20 / MA60 / MA200 (MA10 gone; MA50 too since spec v07), strict chart
averages (min_samples=window) with warm-up history loaded ahead of the first
shown day, the divergence / RSI values unchanged, and the major-index window's
60 / 200-day lines. Synthetic prices only; the loader is patched."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import i18n  # noqa: E402
from data import market  # noqa: E402
from data.indicators import _compute_indicators, add_chart_mas  # noqa: E402
from ui.colors import MA_RAMP  # noqa: E402
from ui.dialogs.index_ma import IndexMaDialog, missing_mas  # noqa: E402
from ui.dialogs.stock_ma import StockMaDialog  # noqa: E402
from ui.dialogs.stock_ma_consts import _MA_KEYS, _SERIES  # noqa: E402


def _history(first: date, last: date = None, gap: tuple = None, seed: int = 3) -> pl.DataFrame:
    """Weekday OHLCV bars from `first` to `last` (today); `gap` = (from, to)
    leaves those dates out, like a long trading halt."""
    last = last or datetime.now().date()
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    days = [d for d in days if d.weekday() < 5 and not (gap and gap[0] <= d <= gap[1])]
    n = len(days)
    close = 1000 * np.cumprod(1 + np.random.default_rng(seed).normal(0.0004, 0.015, n))
    return pl.DataFrame({"Date": days, "Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": np.full(n, 1e5)})


class _Loader:
    """Stands in for data.market._load_ohlcv_window over one fixed history."""

    def __init__(self, hist: pl.DataFrame):
        self.hist, self.calls = hist, []

    def __call__(self, ticker, start, end=None):
        self.calls.append(start)
        df = self.hist.filter(pl.col("Date") >= date.fromisoformat(start))
        if end:
            df = df.filter(pl.col("Date") <= date.fromisoformat(end))
        return df


def _frame_equal(a: pl.Series, b: pl.Series) -> bool:
    x, y = a.to_numpy().astype(float), b.to_numpy().astype(float)
    return bool(np.array_equal(np.isnan(x), np.isnan(y)) and np.allclose(x[~np.isnan(x)], y[~np.isnan(y)]))


class TestStrictChartMas(unittest.TestCase):

    def test_first_window_minus_one_bars_are_missing(self):
        df = add_chart_mas(_history(date(2024, 1, 1), date(2025, 6, 30)))
        for w in (20, 60, 200):
            col = df.get_column(f"SMA{w}")
            self.assertEqual(col[: w - 1].null_count(), w - 1, w)
            self.assertEqual(col[w - 1:].null_count(), 0, w)
            self.assertAlmostEqual(col[w - 1], float(df.get_column("Close")[:w].mean()), places=6)

    def test_199_200_201_closes(self):
        hist = _history(date(2020, 1, 1))
        for n, valid in ((199, 0), (200, 1), (201, 2)):
            col = add_chart_mas(hist.head(n), (200,)).get_column("SMA200")
            self.assertEqual(n - col.null_count(), valid, n)

    def test_a_missing_close_is_not_filled(self):
        df = _history(date(2024, 1, 1), date(2024, 6, 30))
        df = df.with_columns(pl.when(pl.int_range(pl.len()) == 70).then(None)
                             .otherwise(pl.col("Close")).alias("Close"))
        col = add_chart_mas(df, (20,)).get_column("SMA20")
        self.assertEqual(col[70:90].null_count(), 20)               # every window holding the gap
        self.assertIsNotNone(col[69])
        self.assertIsNotNone(col[90])


class TestStockFetchWarmup(unittest.TestCase):

    def _fetch(self, hist, days=1825):
        loader = _Loader(hist)
        with patch.object(market, "_load_ohlcv_window", loader):
            df, err = market.fetch_stock_ma_multi("005930", "KOSPI", windows=(20,), days=days)
        self.assertIsNone(err)
        return df, loader

    def test_first_shown_day_has_every_average(self):
        df, _ = self._fetch(_history(date(2015, 1, 1)))
        first = df.row(0, named=True)
        self.assertGreaterEqual(first["Date"], datetime.now().date() - timedelta(days=1826))
        for w in (20, 60, 200):
            self.assertIsNotNone(first[f"SMA{w}"], w)

    def test_divergence_and_rsi_are_unchanged(self):
        """The ratios and RSI are _compute_indicators over the shown range,
        exactly as before the warm-up was added."""
        hist = _history(date(2015, 1, 1))
        df, _ = self._fetch(hist)
        start = df.get_column("Date")[0]
        before = _compute_indicators(hist.filter(pl.col("Date") >= start), (20,))
        self.assertEqual(df.height, before.height)
        for col in ("MA20_Div", "MA60_Div", "RSI14", "MA20", "MA60"):
            self.assertTrue(_frame_equal(df.get_column(col), before.get_column(col)), col)

    def test_a_longer_lookback_does_not_change_a_days_value(self):
        hist = _history(date(2015, 1, 1))
        long_df, _ = self._fetch(hist, days=1825)
        short_df, _ = self._fetch(hist, days=700)
        joined = short_df.select(["Date", "SMA60", "SMA200"]).join(
            long_df.select(["Date", "SMA60", "SMA200"]), on="Date", suffix="_long")
        self.assertEqual(joined.height, short_df.height)
        for w in (60, 200):
            self.assertTrue(_frame_equal(joined.get_column(f"SMA{w}"), joined.get_column(f"SMA{w}_long")), w)

    def test_new_listing_keeps_the_gap_and_greys_out_the_menu(self):
        hist = _history(datetime.now().date() - timedelta(days=150))     # ~107 sessions
        df, loader = self._fetch(hist)
        self.assertEqual(len(loader.calls), 2)                      # a longer request found nothing older
        self.assertEqual(df.get_column("SMA200").null_count(), df.height)
        self.assertEqual(df.get_column("SMA20")[:19].null_count(), 19)
        d = StockMaDialog("NEW", "New", "KOSPI", df)
        cb = d._series_buttons["MA200"]
        self.assertFalse(cb.isEnabled())
        self.assertEqual(cb.text(), "MA200 · 데이터 부족")
        self.assertTrue(d._series_buttons["MA20"].isEnabled())
        d.close()

    def test_a_long_halt_extends_the_lookback(self):
        today = datetime.now().date()
        start = today - timedelta(days=1825)
        hist = _history(date(2015, 1, 1), gap=(start - timedelta(days=400), start - timedelta(days=1)))
        df, loader = self._fetch(hist)
        self.assertGreater(len(loader.calls), 1)
        self.assertIsNotNone(df.row(0, named=True)["SMA200"])

    def test_the_warmup_counts_valid_sessions(self):
        today = datetime.now().date()
        display = today - timedelta(days=365)
        loader = _Loader(_history(date(2015, 1, 1)))
        with patch.object(market, "_load_ohlcv_window", loader):
            full = market._load_with_warmup("X", display)
        self.assertGreaterEqual(full.filter(pl.col("Date") < display).height, 199)


class TestIndexMas(unittest.TestCase):

    def _fetch(self, hist, days=365):
        with patch.object(market, "_load_ohlcv_window", _Loader(hist)):
            return market.fetch_index_mas("^KS11", days)

    def test_lines_are_60_and_200_with_values_from_the_first_shown_day(self):
        hist = _history(date(2015, 1, 1))
        df, err = self._fetch(hist)
        self.assertIsNone(err)
        self.assertEqual(df.columns, ["Date", "Close", "SMA60", "SMA200"])
        self.assertGreaterEqual(df.get_column("Date")[0], datetime.now().date() - timedelta(days=365))
        self.assertEqual(df.get_column("SMA200").null_count(), 0)
        self.assertEqual(missing_mas(df), [])
        # same values as over the whole history: the cut never changes a day
        whole = add_chart_mas(hist, (60, 200)).join(df.select(["Date", "SMA200"]), on="Date", suffix="_shown")
        self.assertTrue(_frame_equal(whole.get_column("SMA200"), whole.get_column("SMA200_shown")))

    def test_short_history_stays_missing_and_is_labelled(self):
        df, _ = self._fetch(_history(datetime.now().date() - timedelta(days=520)))   # ~110 sessions before
        self.assertGreater(df.get_column("SMA200").null_count(), 0)
        self.assertEqual(missing_mas(df), [200])
        dlg = IndexMaDialog({"KOSPI": (df, None)})
        ax = dlg.findChild(type(dlg.layout().itemAt(0).widget())).figure.axes[0]
        labels = [t.get_text() for t in ax.get_legend().get_texts()]
        self.assertEqual(labels, ["종가", "60일 이평", "200일 이평 (데이터 부족)"])
        dlg.close()

    def test_dialog_draws_close_60_200_only(self):
        df, _ = self._fetch(_history(date(2015, 1, 1)))
        dlg = IndexMaDialog({"KOSPI": (df, None)})
        ax = dlg.layout().itemAt(0).widget().figure.axes[0]
        self.assertEqual([t.get_text() for t in ax.get_legend().get_texts()], ["종가", "60일 이평", "200일 이평"])
        self.assertEqual([ln.get_color() for ln in ax.get_lines()[1:]], [MA_RAMP["MA60"], MA_RAMP["MA200"]])
        dlg.close()


class TestStockChartMenu(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        hist = _history(date(2015, 1, 1))
        with patch.object(market, "_load_ohlcv_window", _Loader(hist)):
            cls.df, _ = market.fetch_stock_ma_multi("005930", "KOSPI", windows=(20,))

    def test_menu_end_labels_and_hover_have_ma60_ma200_not_ma10(self):
        self.assertEqual(_MA_KEYS, ("MA20", "MA60", "MA200"))
        self.assertNotIn("MA10", _SERIES)
        d = StockMaDialog("005930", "Samsung", "KOSPI", self.df)
        self.assertEqual([d._series_buttons[k].text() for k in _MA_KEYS], ["MA20", "MA60", "MA200"])
        self.assertNotIn("MA50", d._series_buttons)
        self.assertNotIn("SMA50", self.df.columns)
        self.assertNotIn("MA10", d._lines)
        for k in _MA_KEYS:
            d._set_series(k, True)
        self.assertEqual(set(d._end_labels), {"Close", *_MA_KEYS})
        d._set_cursor(len(d._x) - 10)
        shown = {k for k, (m, a) in d._hover.items() if m.get_visible() and a.get_visible()}
        self.assertTrue({"MA60", "MA200"} <= shown)
        self.assertNotIn("MA10", shown)
        # the lines are the strict SMA columns
        self.assertTrue(np.allclose(d._lines["MA200"].get_ydata(), self.df.get_column("SMA200").to_numpy(),
                                    equal_nan=True))
        d.close()

    def test_divergence_lines_keep_their_colours(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", self.df)
        self.assertEqual(d._lines["Div(20)"].get_color(), "#d1c5ff")
        self.assertEqual(d._lines["Div(60)"].get_color(), "#a993ff")      # the long divergence line: MA60
        d.close()

    def test_period_change_keeps_the_values(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", self.df)
        before = d._lines["MA200"].get_ydata().copy()
        for p in ("1M", "All", "3Y", "5Y", "10Y"):
            d._apply_period(p)
            self.assertTrue(np.allclose(d._lines["MA200"].get_ydata(), before, equal_nan=True), p)
        d.close()

    def test_not_enough_data_in_english(self):
        df = self.df.with_columns(pl.lit(None, dtype=pl.Float64).alias("SMA200"))
        i18n.set_language("en")
        try:
            d = StockMaDialog("NEW", "New", "KOSPI", df)
            self.assertEqual(d._series_buttons["MA200"].text(), "MA200 · not enough data")
            d.close()
        finally:
            i18n.set_language("ko")


if __name__ == "__main__":
    unittest.main()
