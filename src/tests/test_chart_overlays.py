"""tests/test_chart_overlays.py — docs/ma60_unification_2026-11-18.md step
[1b]: the strategy.signal_api chart overlays (1.7). The breakout channel on
the stock chart and the L1 regime shading on the index window must equal the
signals.py arrays the backtest uses; provisional (intraday / unfinished-week)
values are flagged; providers without the optional methods give nothing.
Synthetic prices only; no network."""
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch

import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import i18n  # noqa: E402
from strategy import signal_api  # noqa: E402
from strategy.signal_api import RegimeSpan  # noqa: E402
from strategy.trend_following.config import StrategyParams  # noqa: E402
from strategy.trend_following.signal_provider import breakout_channel, regime_spans_of  # noqa: E402
from strategy.trend_following.signals import compute_features, regime_state, rolling, shift  # noqa: E402
from tests.strategy.trend_following.helpers import business_days, synthetic_dataset  # noqa: E402
from threads import fetch_threads  # noqa: E402
from threads.fetch_threads import index_regimes, kr_bar_is_provisional, stock_chart_overlays  # noqa: E402
from ui.dialogs.index_ma import IndexMaDialog  # noqa: E402
from ui.dialogs.stock_ma import StockMaDialog  # noqa: E402

P = StrategyParams()


def _ohlc(n=300, seed=5, first=date(2025, 1, 2)):
    days = business_days(first, n)
    rng = np.random.default_rng(seed)
    close = 1000 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    high = close * (1 + rng.random(n) * 0.02)
    low = close * (1 - rng.random(n) * 0.02)
    return pl.DataFrame({"Date": days, "Open": close, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(n, 1e6)})


def _arr(line):
    return np.array([np.nan if v is None else v for v in line.values])


def _compact_v(d, key):
    """The end label's compact value of `key` at the right edge."""
    from ui.dialogs.stock_ma_helpers import _compact
    return _compact(float(d._end_labels[key].xy[1]))


def _expand(spans, days):
    """Per-day (state, provisional) from spans."""
    out = {}
    for sp in spans:
        for d in days:
            if sp.start <= d <= sp.end:
                out[d] = (sp.on, sp.provisional)
    return [out[d] for d in days]


class TestBreakoutChannel(unittest.TestCase):

    def test_equals_the_signals_definition_and_excludes_today(self):
        df = _ohlc()
        hi_line, lo_line = breakout_channel(df["Date"], df["High"], df["Low"], P)
        high, low = df["High"].to_numpy(), df["Low"].to_numpy()
        np.testing.assert_array_equal(_arr(hi_line), shift(rolling(high, P.breakout_window, "max"), 1))
        np.testing.assert_array_equal(_arr(lo_line), shift(rolling(low, P.exit_window, "min"), 1))
        t = 150
        self.assertEqual(_arr(hi_line)[t], high[t - P.breakout_window:t].max())      # sessions t-20 .. t-1
        self.assertEqual(_arr(lo_line)[t], low[t - P.exit_window:t].min())
        self.assertEqual(hi_line.values[:P.breakout_window], [None] * P.breakout_window)
        self.assertEqual((hi_line.key, lo_line.key), ("breakout_high", "exit_low"))
        self.assertEqual((hi_line.label, lo_line.label), ("직전 20일 최고가", "직전 10일 최저가"))
        self.assertIn("당일 제외", hi_line.tooltip)

    def test_matches_the_backtest_entry_and_exit_arrays(self):
        """close > channel high is compute_features' breakout, close < channel
        low its exit_channel, on every session of every name."""
        ds = synthetic_dataset()
        f = compute_features(ds, P)
        b = ds.stocks
        for j, tk in enumerate(b.tickers):
            hi, lo = breakout_channel(b.dates, b.high[:, j], b.low[:, j], P)
            close = b.close[:, j]
            np.testing.assert_array_equal(close > _arr(hi), f.breakout[:, j], tk)
            np.testing.assert_array_equal(close < _arr(lo), f.exit_channel[:, j], tk)

    def test_a_missing_bar_breaks_the_line(self):
        df = _ohlc()
        high = df["High"].to_numpy().copy()
        high[100] = np.nan
        hi, _lo = breakout_channel(df["Date"], high, df["Low"], P)
        self.assertTrue(all(v is None for v in hi.values[101:121]))   # every window holding bar 100
        self.assertIsNotNone(hi.values[100])
        self.assertIsNotNone(hi.values[121])

    def test_provisional_last_bar(self):
        df = _ohlc()
        hi, lo = breakout_channel(df["Date"], df["High"], df["Low"], P, provisional_last=True)
        self.assertEqual(hi.provisional_from, df["Date"][-1])
        self.assertIsNone(breakout_channel(df["Date"], df["High"], df["Low"], P)[0].provisional_from)

    def test_window_lengths_come_from_the_params(self):
        p = replace(P, breakout_window=15, exit_window=7)
        df = _ohlc()
        hi, lo = breakout_channel(df["Date"], df["High"], df["Low"], p)
        self.assertEqual((hi.label, lo.label), ("직전 15일 최고가", "직전 7일 최저가"))
        self.assertEqual(hi.values[:15], [None] * 15)

    def test_signal_api_kr_only(self):
        df = _ohlc()
        self.assertEqual(len(signal_api.chart_overlays("C1", "X", "KOSPI", df)), 2)
        self.assertEqual(len(signal_api.chart_overlays("A0", "X", "KOSDAQ", df)), 2)
        self.assertEqual(signal_api.chart_overlays("C1", "AAPL", "NASDAQ 100", df), [])


class TestRegimeSpans(unittest.TestCase):

    def _index(self, n=400, seed=7, first=date(2025, 1, 2), days=None):
        days = days or business_days(first, n)
        rng = np.random.default_rng(seed)
        close = 2500 * np.cumprod(1 + rng.normal(0.0003, 0.012, len(days)))
        return days, close

    def test_spans_equal_regime_state(self):
        days, close = self._index()
        spans = regime_spans_of(days, close, P)
        state, _check = regime_state(close, days, P)
        self.assertEqual([s for s, _p in _expand(spans, days)], [bool(x) for x in state])
        self.assertFalse(any(p for _s, p in _expand(spans, days)))
        self.assertTrue(any(sp.on for sp in spans) and any(not sp.on for sp in spans))
        for a, b in zip(spans, spans[1:]):                      # contiguous, alternating
            self.assertLess(a.end, b.start)
            self.assertNotEqual((a.on, a.provisional), (b.on, b.provisional))

    def test_holiday_weeks(self):
        """Mondays off in some weeks: the check moves to the next session
        (weekly_check_days) and the spans still equal regime_state."""
        days, _ = self._index()
        days = [d for i, d in enumerate(days) if not (d.weekday() == 0 and i % 3 == 0)]
        _, close = self._index(days=days)
        spans = regime_spans_of(days, close, P)
        state, check = regime_state(close, days, P)
        self.assertEqual([s for s, _p in _expand(spans, days)], [bool(x) for x in state])
        self.assertTrue(any(days[i].weekday() == 1 and check[i] for i in range(len(days))))

    def test_unfinished_week_is_provisional_only_before_its_check_day(self):
        days, close = self._index()
        # Cut so the last week holds Mon-Tue only.
        while days[-1].weekday() != 1:
            days, close = days[:-1], close[:-1]
        default = regime_spans_of(days, close, P)                  # check weekday Mon: Monday is the real check
        self.assertFalse(default[-1].provisional)
        wed = regime_spans_of(days, close, replace(P, check_weekday=2))   # Wednesday not come yet
        self.assertTrue(wed[-1].provisional)
        # Only Tuesday, the fallback check: Monday still holds last week's real check.
        self.assertEqual((wed[-1].start, wed[-1].end), (days[-1], days[-1]))
        self.assertFalse(wed[-2].provisional)

    def test_intraday_last_bar_is_provisional_on_a_check_day(self):
        days, close = self._index()
        while days[-1].weekday() != 0:                         # last bar a Monday (a check day)
            days, close = days[:-1], close[:-1]
        spans = regime_spans_of(days, close, P, provisional_last=True)
        self.assertTrue(spans[-1].provisional)
        self.assertEqual(spans[-1].start, days[-1])
        while days[-1].weekday() != 2:                         # Wednesday: held from Monday, final
            days, close = days[:-1], close[:-1]
        self.assertFalse(regime_spans_of(days, close, P, provisional_last=True)[-1].provisional)

    def test_more_history_does_not_change_a_shown_day(self):
        days, close = self._index(n=500)
        shown = days[-250:]
        long_ = _expand(regime_spans_of(days, close, P), shown)
        k = len(days) - 250 - 140                              # ~140 sessions of warm-up only
        short = _expand(regime_spans_of(days[k:], close[k:], P), shown)
        self.assertEqual(long_, short)

    def test_variant_without_regime_and_other_markets(self):
        days, close = self._index()
        self.assertTrue(signal_api.regime_spans("C1", "KOSPI", days, close))
        self.assertEqual(signal_api.regime_spans("A0", "KOSPI", days, close), [])
        self.assertEqual(signal_api.regime_spans("C1", "NASDAQ 100", days, close), [])
        self.assertTrue(signal_api.uses_regime("A1"))
        self.assertFalse(signal_api.uses_regime("A0"))
        label, tip = signal_api.regime_legend("C1")
        self.assertEqual(label, "L1 Risk-on(종가 판단)")
        self.assertIn("다음 거래일", tip)
        self.assertIn("A0", tip)


class TestProviderWithoutOverlays(unittest.TestCase):

    def test_optional_methods_default_to_nothing(self):
        class Bare:
            def variants(self):
                return ["Z1"]
        with patch.object(signal_api, "_PROVIDERS", {"Z1": Bare()}):
            self.assertEqual(signal_api.chart_overlays("Z1", "X", "KOSPI", _ohlc()), [])
            self.assertEqual(signal_api.regime_spans("Z1", "KOSPI", [], []), [])
            self.assertFalse(signal_api.uses_regime("Z1"))
            self.assertEqual(signal_api.regime_legend("Z1"), ("", ""))


class TestThreads(unittest.TestCase):

    def test_kr_bar_is_provisional(self):
        wed = datetime(2026, 11, 18, 10, 0)
        self.assertTrue(kr_bar_is_provisional(date(2026, 11, 18), wed))
        self.assertFalse(kr_bar_is_provisional(date(2026, 11, 18), wed.replace(hour=16)))
        self.assertFalse(kr_bar_is_provisional(date(2026, 11, 17), wed))
        self.assertFalse(kr_bar_is_provisional(date(2026, 11, 21), datetime(2026, 11, 21, 10)))   # Saturday

    def test_stock_overlays_flag_an_intraday_bar(self):
        df = _ohlc(first=date(2026, 1, 2))
        last = df["Date"][-1]
        intraday = datetime.combine(last, datetime.min.time()).replace(hour=11)
        ov = stock_chart_overlays("X", "KOSPI", df, "A1", now=intraday)
        self.assertEqual([o.provisional_from for o in ov], [last, last])
        ov = stock_chart_overlays("X", "KOSPI", df, "A1", now=intraday + timedelta(days=1))
        self.assertEqual([o.provisional_from for o in ov], [None, None])
        self.assertEqual(stock_chart_overlays("X", "KOSPI", None), [])

    def test_index_regimes_judge_each_market_on_its_own_index(self):
        days = business_days(date(2025, 1, 2), 420)
        up = 2000 * np.cumprod(np.full(len(days), 1.001))
        down = 900 * np.cumprod(np.full(len(days), 0.999))
        hist = {"KS11": up, "KQ11": down}

        def fake_hist(ticker, start):
            s = date.fromisoformat(start)
            keep = [i for i, d in enumerate(days) if d >= s]
            return pl.DataFrame({"Date": [days[i] for i in keep], "Close": hist[ticker][keep]})
        shown = pl.DataFrame({"Date": days[-250:], "Close": up[-250:]})
        results = {"KOSPI": (shown, None), "KOSDAQ": (shown, None), "S&P500": (shown, None)}
        with patch.object(fetch_threads, "get_historical_data", side_effect=fake_hist):
            r = index_regimes(results, "C1", now=datetime(2030, 1, 1))
        self.assertEqual(set(r["spans"]), {"KOSPI", "KOSDAQ"})
        self.assertTrue(r["spans"]["KOSPI"][-1].on)
        self.assertFalse(any(sp.on for sp in r["spans"]["KOSDAQ"]))
        with patch.object(fetch_threads, "get_historical_data", side_effect=fake_hist):
            a0 = index_regimes(results, "A0")
        self.assertFalse(a0["uses"])
        self.assertEqual(a0["spans"], {})


class TestStockChartChannel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from data.indicators import _compute_indicators, add_chart_mas
        raw = _ohlc(n=400, first=date(2025, 1, 2))
        cls.df = add_chart_mas(_compute_indicators(raw.with_columns(pl.col("Date").cast(pl.Datetime)), (20,)))
        cls.ov = breakout_channel(raw["Date"], raw["High"], raw["Low"], P, provisional_last=True)

    def test_toggle_is_off_by_default_and_switches_both_lines(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", self.df, overlays=self.ov)
        cb = d._series_buttons["CH"]
        self.assertEqual(cb.text(), "돌파 채널")
        self.assertFalse(cb.isChecked())
        self.assertIn("당일 제외", cb.toolTip())
        self.assertFalse(d._lines["CH_HI"].get_visible() or d._lines["CH_LO"].get_visible())
        cb.click()
        self.assertTrue(d._lines["CH_HI"].get_visible() and d._lines["CH_LO"].get_visible())
        self.assertEqual(d._lines["CH_HI"].get_drawstyle(), "steps-post")
        np.testing.assert_array_equal(d._lines["CH_HI"].get_ydata(), _arr(self.ov[0]))   # NaN gaps kept
        self.assertTrue({"CH_HI", "CH_LO"} <= set(d._end_labels))
        self.assertEqual(d._end_labels["CH_HI"].get_text(), f"20일 최고 {_compact_v(d, 'CH_HI')}")
        self.assertTrue(d._end_labels["CH_LO"].get_text().startswith("10일 최저 "))
        d._set_cursor(len(d._x) - 1)
        self.assertTrue(d._hover["CH_LO"][1].get_text().endswith("잠정(장중)"))
        d._set_cursor(200)
        self.assertEqual(d._hover["CH_LO"][1].get_text(), f"직전 10일 최저가 {self.ov[1].values[200]:,.0f}")
        lo, hi = d._panel_axes["price"].get_ylim()                        # autoscale holds the channel
        vis = d._visible_mask()
        self.assertLessEqual(lo, np.nanmin(d._lines["CH_LO"].get_ydata()[vis]))
        self.assertGreaterEqual(hi, np.nanmax(d._lines["CH_HI"].get_ydata()[vis]))
        cb.click()
        self.assertNotIn("CH_HI", d._end_labels)
        self.assertFalse(any(a.get_visible() for a in d._overlay_prov_artists))
        d.close()

    def test_no_overlays_no_toggle(self):
        d = StockMaDialog("AAPL", "Apple", "NASDAQ 100", self.df, overlays=[])
        self.assertNotIn("CH", d._series_buttons)
        self.assertNotIn("CH_HI", d._lines)
        d.close()

    def test_english(self):
        i18n.set_language("en")
        try:
            ov = breakout_channel(self.ov[0].dates, self.df["High"], self.df["Low"], P)
            # (the menu caption is a module constant: the app's language switch re-imports it)
            d = StockMaDialog("005930", "Samsung", "KOSPI", self.df, overlays=ov)
            d._set_series("CH", True)
            self.assertTrue(d._end_labels["CH_HI"].get_text().startswith("20d high "))
            d._set_cursor(len(d._x) - 1)
            self.assertTrue(d._hover["CH_HI"][1].get_text().startswith("Prior 20-day high "))
            d.close()
        finally:
            i18n.set_language("ko")


class TestIndexWindowShading(unittest.TestCase):

    def _frame(self, days):
        n = len(days)
        close = np.linspace(2000, 2600, n)
        return pl.DataFrame({"Date": days, "Close": close, "SMA60": close, "SMA200": close})

    def test_kospi_kosdaq_shaded_from_their_spans_overseas_not(self):
        days = business_days(date(2025, 11, 3), 250)
        df = self._frame(days)
        spans = {"KOSPI": [RegimeSpan(days[0], days[99], True), RegimeSpan(days[100], days[-1], False)],
                 "KOSDAQ": [RegimeSpan(days[0], days[9], False), RegimeSpan(days[10], days[49], True),
                            RegimeSpan(days[50], days[-2], False), RegimeSpan(days[-1], days[-1], True, True)]}
        regimes = {"variant": "C1", "uses": True, "legend": signal_api.regime_legend("C1"), "spans": spans}
        dlg = IndexMaDialog({"KOSPI": (df, None), "KOSDAQ": (df, None), "S&P500": (df, None)}, regimes)
        fig = dlg.findChild(type(dlg.layout().itemAt(0).widget())).figure
        by_title = {ax.get_title(): ax for ax in fig.axes}
        self.assertEqual(len(by_title["KOSPI"].patches), 1)
        self.assertEqual(len(by_title["KOSDAQ"].patches), 2)
        self.assertEqual(len(by_title["S&P500"].patches), 0)
        alphas = sorted(p.get_alpha() for p in by_title["KOSDAQ"].patches)
        self.assertLess(alphas[0], alphas[1])                             # the provisional span is paler
        legend = [t.get_text() for t in by_title["KOSPI"].get_legend().get_texts()]
        self.assertEqual(legend[-1], "L1 Risk-on(종가 판단)")
        self.assertEqual([t.get_text() for t in by_title["S&P500"].get_legend().get_texts()],
                         ["종가", "60일 이평", "200일 이평"])
        self.assertIn("C1", dlg.regime_note.text())
        self.assertIn("다음 거래일", dlg.regime_note.toolTip())
        dlg.close()

    def test_variant_without_regime_says_so(self):
        days = business_days(date(2025, 11, 3), 250)
        df = self._frame(days)
        regimes = {"variant": "A0", "uses": False, "legend": signal_api.regime_legend("A0"), "spans": {}}
        dlg = IndexMaDialog({"KOSPI": (df, None)}, regimes)
        ax = dlg.findChild(type(dlg.layout().itemAt(0).widget())).figure.axes[0]
        self.assertEqual(len(ax.patches), 0)
        self.assertEqual(ax.get_legend().get_texts()[-1].get_text(), "A0: 시장 조건 없음")
        self.assertIn("A0", dlg.regime_note.text())
        dlg.close()


if __name__ == "__main__":
    unittest.main()
