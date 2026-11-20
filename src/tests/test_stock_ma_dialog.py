"""tests/test_stock_ma_dialog.py — StockMaDialog after the docs/ui.md "MA Chart
Redesign" (2026-09-19): PROFIT/LOSS volume, accent MA ramp, right-edge value
labels instead of a legend, independent series toggles (2026-10-15), period
buttons, panel toggles and the shared crosshair with on-chart value and date
badges. Synthetic data only."""
import unittest
from datetime import date, timedelta

import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication, QPushButton
from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QPixmap

app = QApplication.instance() or QApplication([])

from data.indicators import _compute_indicators, add_chart_mas  # noqa: E402
from ui.dialogs.stock_ma import StockMaDialog  # noqa: E402
from ui.colors import PROFIT, LOSS, MA_RAMP  # noqa: E402
from ui.dialogs.stock_ma_consts import _MA_KEYS  # noqa: E402


def _df(n=400, ew=False, volume=True, index=False):
    rng = np.random.default_rng(1)
    close = 100000 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    df = pl.DataFrame({
        "Date": dates, "Open": close * (1 + rng.normal(0, 0.005, n)), "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": (rng.random(n) * 5e6) if volume else np.zeros(n),
    }).with_columns(pl.col("Date").cast(pl.Datetime))
    df = add_chart_mas(_compute_indicators(df, windows=(20,)))
    if ew:
        df = df.with_columns((pl.col("Close") * 0.5 + 1000).alias("EqualWeight"))
    if index:
        df = df.with_columns((pl.Series(np.linspace(2000.0, 3000.0, n))).alias("MarketIndex"))
    return df


class _Ev:
    """The few MouseEvent attributes the handlers read."""
    def __init__(self, ax, xdata, x=0, button=None):
        self.inaxes, self.xdata, self.x, self.button = ax, xdata, x, button


def _rgb(hex_color):
    return tuple(round(int(hex_color[i:i + 2], 16) / 255, 2) for i in (1, 3, 5))


def _show(d, keys):
    """Set every series toggle so exactly `keys` are on."""
    for key in d._series_buttons:
        d._set_series(key, key in keys)


class TestStockChart(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dlg = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True),
                                investor_data=[{"Date": "2026-09-19", "Close": 1000, "Foreigner": 5,
                                                "Institution": -3, "Retail": 0}])

    def test_panels_and_default_view(self):
        d = self.dlg
        self.assertEqual(list(d._panel_axes), ["price", "vol", "div", "rsi"])
        self.assertEqual(d._panel_on, {"vol": True, "div": True, "rsi": True})     # RSI on by default (2026-11-16)
        self.assertTrue(d._panel_buttons["rsi"].isChecked())
        # Opens with Close + Divergence (user direction 2026-09-29): no MA
        # line visible. A fresh dialog, since the shared one is left in
        # whatever state the other tests chose last.
        fresh = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        self.assertEqual({k for k, b in fresh._series_buttons.items() if b.isChecked()}, {"Div"})
        self.assertEqual({k: fresh._lines[k].get_visible() for k in _MA_KEYS},
                         {"MA20": False, "MA60": False, "MA200": False})
        self.assertNotIn("MA5", fresh._lines)                          # dropped 2026-11-16
        self.assertNotIn("Close", fresh._series_buttons)               # Close is always drawn
        self.assertTrue(fresh._lines["Close"].get_visible())
        self.assertEqual(set(fresh._end_labels), {"Close"})
        _show(d, {"MA60", "MA20"})
        self.assertEqual({k: d._lines[k].get_visible() for k in _MA_KEYS},
                         {"MA20": True, "MA60": True, "MA200": False})
        self.assertIsNone(d._panel_axes["price"].get_legend())      # issue #3: no legend
        for line in d._lines.values():
            self.assertEqual(line.get_marker(), "None")             # issue #8: no markers

    def test_volume_bars_use_profit_loss(self):
        cols = [tuple(c.get_facecolor()[0][:3].round(2)) for c in self.dlg._panel_axes["vol"].collections]
        self.assertEqual(cols, [_rgb(PROFIT), _rgb(LOSS)])

    def test_ma_lines_are_the_accent_ramp(self):
        for key in _MA_KEYS:
            self.assertEqual(self.dlg._lines[key].get_color(), MA_RAMP[key])

    def test_end_labels_follow_the_visible_lines(self):
        d = self.dlg
        _show(d, {"MA60", "MA200"})
        self.assertEqual(set(d._end_labels), {"Close", "MA60", "MA200"})
        _show(d, {"Close", "MA20", "MA60"})
        self.assertEqual(set(d._end_labels), {"Close", "MA20", "MA60"})
        _show(d, {"Close"})
        self.assertEqual(set(d._end_labels), {"Close"})
        _show(d, {"Close", "EW"})
        self.assertTrue(d._lines["EW"].get_visible())
        self.assertEqual(set(d._end_labels), {"Close", "EW"})
        _show(d, {"Close", "Div"})

    def test_series_toggles_are_independent(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        self.assertEqual(list(d._series_buttons), ["MA20", "MA60", "MA200", "Div"])     # no MA50 (v07)
        d._series_buttons["MA60"].click()
        d._series_buttons["MA200"].click()
        self.assertEqual({k for k in _MA_KEYS if d._lines[k].get_visible()}, {"MA60", "MA200"})
        self.assertTrue(d._lines["Close"].get_visible())
        d._series_buttons["Div"].click()                # Divergence off -> panel hidden
        self.assertFalse(d._panel_on["div"])
        self.assertFalse(d._panel_axes["div"].get_visible())
        d._set_cursor(100)
        self.assertNotIn("Div(20)", {k for k, (m, _a) in d._hover.items() if m.get_visible()})
        d._series_buttons["Div"].click()
        self.assertTrue(d._panel_axes["div"].get_visible())

    def test_market_index_toggle_sits_after_the_mas(self):
        """2026-11-17 (user direction): a 시장지수 button after the MAs overlays
        the stock's market index, rebased to the close at the view's first
        bar; its end label and hover show the index level itself."""
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(index=True))
        self.assertEqual(list(d._series_buttons), ["MA20", "MA60", "MA200", "IDX", "Div"])
        self.assertEqual(d._series_buttons["IDX"].text(), "시장지수")
        self.assertFalse(d._lines["IDX"].get_visible())              # off by default
        d._series_buttons["IDX"].click()
        self.assertTrue(d._lines["IDX"].get_visible())
        close = d._col("Close")
        first = int(np.argmax(d._visible_mask()))
        self.assertAlmostEqual(d._lines["IDX"].get_ydata()[first], close[first], places=6)
        d._apply_period("All")                                        # rebased to the new first bar
        self.assertAlmostEqual(d._lines["IDX"].get_ydata()[0], close[0], places=6)
        self.assertTrue(d._end_labels["IDX"].get_text().startswith("KOSPI 3,000.00"))
        d._set_cursor(0)
        self.assertEqual(d._hover["IDX"][1].get_text(), "KOSPI 2,000.00")
        d._series_buttons["IDX"].click()
        self.assertNotIn("IDX", d._end_labels)
        # no index column -> no button
        self.assertNotIn("IDX", StockMaDialog("005930", "Samsung", "KOSPI", _df())._series_buttons)

    def test_close_is_always_drawn_so_the_price_panel_stays(self):
        """2026-11-16: no 종가 button -- Close is always on, so the price panel
        never hides (it used to when no price line was selected)."""
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        _show(d, {"Div"})
        self.assertEqual(d._visible_panels(), [("price", 3), ("vol", 1), ("div", 1), ("rsi", 1)])
        self.assertTrue(d._panel_axes["price"].get_visible())
        self.assertEqual(set(d._end_labels), {"Close"})
        _show(d, set())
        self.assertTrue(d._lines["Close"].get_visible())

    def test_period_buttons_set_the_window(self):
        d, ax = self.dlg, self.dlg._panel_axes["price"]
        d._apply_period("1M")
        self.assertAlmostEqual(ax.get_xlim()[1] - ax.get_xlim()[0], 31, delta=1)
        self.assertEqual(d._period_combo.currentData(), "1M")
        d._period_combo.setCurrentIndex(d._period_combo.findData("3M"))   # picking from the combo
        self.assertAlmostEqual(ax.get_xlim()[1] - ax.get_xlim()[0], 92, delta=1)
        d._apply_period("All")
        self.assertGreater(ax.get_xlim()[1] - ax.get_xlim()[0], 399)
        d._apply_period("1Y")

    def test_view_row_is_one_line_with_drop_downs(self):
        """2026-11-18 (user direction): one line -- [이동평균 ▾] a check-box
        drop-down (several at once), [보조지표 이격도 · 거래량 · RSI] buttons
        of one width, [기간 ▾] a combo box, then the as-of date."""
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True))
        row = d._period_combo.parentWidget()
        self.assertIsInstance(row.layout(), QHBoxLayout)
        ws = [row.layout().itemAt(i).widget() for i in range(row.layout().count())]
        ws = [w for w in ws if w is not None]

        def text(w):
            return w.currentText() if isinstance(w, QComboBox) else w.text()
        self.assertEqual([text(w) for w in ws[:-1]],
                         ["이동평균", "없음", "보조지표", "이격도", "거래량", "RSI", "기간", "1년"])
        self.assertIs(ws[1], d._ma_btn)
        self.assertIs(ws[-1], d._as_of_lbl)                           # the as-of date ends the row
        self.assertTrue(all(isinstance(w, QLabel) for w in d._group_captions))
        # MA drop-down: a check box per line, several on at once, the button names them
        self.assertIs(d._ma_btn.menu(), d._ma_menu)
        self.assertEqual([k for k, w in d._series_buttons.items() if isinstance(w, QCheckBox)],
                         ["MA20", "MA60", "MA200", "EW"])
        width = d._ma_btn.width()
        d._series_buttons["MA60"].click()
        d._series_buttons["MA200"].click()
        self.assertEqual(d._ma_btn.text(), "MA60 외 1")
        self.assertEqual({k for k in _MA_KEYS if d._lines[k].get_visible()}, {"MA60", "MA200"})
        d._set_series("EW", True)
        self.assertEqual(d._ma_btn.text(), "MA60 외 2")
        self.assertEqual(d._ma_btn.width(), width)                    # fixed: ticking never moves the row
        self.assertEqual(d._period_combo.width(), width)              # 이동평균 and 기간 share one width
        self.assertGreaterEqual(width, d._period_combo.sizeHint().width())
        # period combo: every period, single choice
        self.assertEqual([d._period_combo.itemText(i) for i in range(d._period_combo.count())],
                         ["1개월", "3개월", "6개월", "1년", "3년", "5년", "전체"])
        buttons = [d._series_buttons["Div"], *d._panel_buttons.values()]
        self.assertEqual(len({x.width() for x in buttons}), 1)
        self.assertTrue(all(x.width() >= x.sizeHint().width() for x in buttons))

    def test_axis_and_end_labels_fit_a_narrow_chart(self):
        """2026-11-15: fixed 7.5% / 10% margins cut "100,000" and the axis
        label once the dialog could be narrowed; margins now come from the
        measured labels."""
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True))
        d.showNormal()
        d.resize(700, 600)
        d.show()
        QApplication.processEvents()
        for k in ("MA60", "MA200", "EW"):
            d._set_series(k, True)
        d._apply_period("All")
        d._canvas.draw()
        r = d._canvas.get_renderer()
        fig_w = d._fig.bbox.width
        for ax in d._panel_axes.values():
            if ax.get_visible():
                self.assertGreaterEqual(ax.yaxis.get_tightbbox(r).x0, 0)
        self.assertTrue(d._end_labels)
        for ann in d._end_labels.values():
            self.assertLessEqual(ann.get_window_extent(r).x1, fig_w)
        d.close()

    def test_toggles_do_not_move_the_price_chart(self):
        """2026-11-15 (user direction): series and panel toggles and the
        period buttons keep the price panel where it is; every panel has a
        fixed slot and one switched off leaves its slot empty."""
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True))
        d.showNormal()
        d.resize(1400, 800)
        d.show()
        QApplication.processEvents()
        price = d._panel_axes["price"]
        slots = {k: tuple(ax.get_position().bounds) for k, ax in d._panel_axes.items()}
        margins = d._margins

        def check(tag):
            d._canvas.draw()
            self.assertEqual(tuple(price.get_position().bounds), slots["price"], tag)
            for k, ax in d._panel_axes.items():
                self.assertEqual(tuple(ax.get_position().bounds), slots[k], tag)
            self.assertEqual(d._margins, margins, tag)

        for key in ("MA60", "MA200", "MA20", "EW", "Div"):
            d._series_buttons[key].click()
            check(key)
        for key in ("vol", "rsi"):
            d._panel_buttons[key].click()
            check(key)
        self.assertFalse(d._panel_axes["vol"].get_visible())         # its slot stays empty
        for period in ("1M", "3M", "All", "1Y"):
            d._apply_period(period)
            check(period)
        d.close()

    def test_right_panel_lines_up_with_the_buttons_and_the_chart(self):
        """2026-11-15: the as-of date ends the button row; the right panel's
        table title is level with the first button line, the investor table
        starts level with the chart's top edge, Trend Score sits under the
        table; the investor groups share one column width; no Close button."""
        rows = [{"Date": f"2025-04-{i:02d}", "Close": 59000, "Foreigner": 1234567 * (i % 3 - 1),
                 "Institution": -500 * i, "Retail": -3 * i} for i in range(1, 11)]
        for width in (1900, 1100):          # one and two button lines
            d = StockMaDialog("005930", "Samsung", "KOSPI", _df(), investor_data=rows)
            d.showNormal()
            d.resize(width, 800)
            d.show()
            for _ in range(3):
                QApplication.processEvents()
            row = d._period_combo.parentWidget()
            self.assertIs(d._as_of_lbl.parentWidget(), row)
            self.assertTrue(d._as_of_lbl.text())

            def gy(w, local=0):
                return w.mapToGlobal(QPoint(0, local)).y()

            def mid(w):
                return gy(w, w.height() // 2)

            self.assertLessEqual(abs(mid(d._right_title) - mid(d._ma_btn)), 1, width)
            self.assertLessEqual(abs(gy(d._right_table) - gy(d._canvas)), 1, width)
            self.assertGreater(gy(d.strategy_section), gy(d._right_table))
            # one line: the as-of date sits level with the period combo
            self.assertLessEqual(abs(mid(d._as_of_lbl) - mid(d._period_combo)), 2)
            widths = [d._right_table.columnWidth(c) for c in (2, 3, 4)]   # Foreigner, Institution, Retail
            self.assertLessEqual(max(widths) - min(widths), 1, widths)
            self.assertFalse([b for b in d.findChildren(QPushButton) if b.text() in ("닫기", "Close")])
            d.close()

    def test_without_a_table_the_trend_title_takes_the_button_line(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        d.showNormal()
        d.resize(1900, 800)
        d.show()
        for _ in range(3):
            QApplication.processEvents()
        self.assertIsNone(d._right_table)
        title = d.strategy_section.title_lbl                 # "전략 점검" + strategy button line (2026-11-16)
        btn = d._ma_btn
        mid = lambda w: w.mapToGlobal(QPoint(0, w.height() // 2)).y()   # noqa: E731
        self.assertLessEqual(abs(mid(title) - mid(btn)), 2)
        d.close()

    def test_simple_chart_row_has_no_series_group(self):
        d = StockMaDialog("KR3YT", "KR 3Y", "Index", _df(volume=False), change_mode="bp")
        self.assertIsNone(d._ma_btn)                              # no 이동평균 drop-down
        self.assertEqual(d._group_captions[0].text(), "보조지표")

    def test_panel_toggles_regrid(self):
        d = self.dlg
        _show(d, {"Close", "Div"})
        d._set_panel("rsi", False)
        self.assertEqual(d._visible_panels(), [("price", 3), ("vol", 1), ("div", 1)])
        d._set_panel("rsi", True)
        self.assertEqual([k for k, _ in d._visible_panels()], ["price", "vol", "div", "rsi"])
        self.assertTrue(d._panel_axes["rsi"].get_visible())
        d._set_panel("vol", False)
        self.assertFalse(d._panel_axes["vol"].get_visible())
        d._set_panel("vol", True)

    def test_crosshair_and_badges(self):
        d, ax = self.dlg, self.dlg._panel_axes["price"]
        d._on_motion(_Ev(ax, d._x[100]))
        self.assertEqual(d._cursor_idx, 100)
        self.assertTrue(all(c.get_visible() for c in d._crosshairs))
        # The date sits on the x axis of the bottom visible panel only.
        bottom = d._visible_panels()[-1][0]
        self.assertEqual([k for k, a in d._date_badges.items() if a.get_visible()], [bottom])
        self.assertEqual(d._date_badges[bottom].get_text(), "2025-04-11")
        self.assertEqual(d._date_badges[bottom].xy[0], d._x[100])
        # A dot + value label on every visible series at the pointer's bar.
        shown = {k for k, (m, a) in d._hover.items() if m.get_visible() and a.get_visible()}
        expected = {k for k, line in d._lines.items() if line.get_visible() and line.axes.get_visible()}
        self.assertEqual(shown, expected | {"Vol"})
        marker, label = d._hover["Close"]
        self.assertEqual(list(marker.get_xdata()), [d._x[100]])
        self.assertAlmostEqual(marker.get_ydata()[0], d._lines["Close"].get_ydata()[100])
        self.assertEqual(label.get_text(), d._fmt_str.format(d._lines["Close"].get_ydata()[100]))
        d._on_leave(None)
        self.assertIsNone(d._cursor_idx)
        self.assertFalse(any(c.get_visible() for c in d._crosshairs))
        self.assertFalse(any(m.get_visible() or a.get_visible() for m, a in d._hover.values()))
        self.assertFalse(any(a.get_visible() for a in d._date_badges.values()))

    def test_header_and_render(self):
        d = self.dlg
        self.assertEqual(d._price_lbl.text(), "59,260")
        self.assertTrue(d._chg_lbl.text().endswith("%"))
        pm = QPixmap(1200, 720)
        d.render(pm)


class TestInvestorTableFitsItsNumbers(unittest.TestCase):

    def test_columns_are_wide_enough_for_share_counts(self):
        from PyQt6.QtGui import QFontMetrics
        from PyQt6.QtWidgets import QTableWidget
        rows = [{"Date": "2026.09.19", "Close": 1849000, "Foreigner": -12345678, "Institution": 9876543, "Retail": 2468135}]
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(), investor_data=rows)
        table = d.findChild(QTableWidget)
        fm = QFontMetrics(table.font())
        # 8px padding a side (theme) + 3px style text margin a side, plus slack.
        needed = [fm.horizontalAdvance(table.item(0, c).text()) + 28 for c in range(table.columnCount())]
        for c, need in enumerate(needed):
            self.assertGreaterEqual(table.columnWidth(c), need, table.item(0, c).text())
        # The splitter cannot shrink the table below what every column needs.
        # (Column widths themselves can exceed this before the dialog is
        # shown: the last section stretches into the widget's default size.)
        self.assertGreaterEqual(table.minimumWidth(), sum(needed))


class TestSimpleAndEmptyCharts(unittest.TestCase):

    def test_bond_yield_is_a_single_panel_without_views(self):
        d = StockMaDialog("KR3YT", "KR 3Y", "Index", _df(volume=False), change_mode="bp")
        self.assertEqual(list(d._panel_axes), ["price"])
        self.assertEqual(d._series_buttons, {})
        self.assertEqual(list(d._lines), ["Close"])
        self.assertTrue(d._chg_lbl.text().endswith("bp"))
        d._zoom_y_in()
        d.render(QPixmap(800, 500))

    def test_missing_data_renders_a_notice(self):
        d = StockMaDialog("X", "X", "KOSPI", None)
        self.assertFalse(d._has_data)
        self.assertIsNone(d.scrollbar)
        d.render(QPixmap(800, 500))


if __name__ == "__main__":
    unittest.main()


def _c_picks(vid="C1"):
    from strategy.trend_following.signal_provider import pick_signals
    from tests.strategy.trend_following.test_scoring import score_book
    book, _ = score_book()
    return pick_signals(vid, book, None)


def _ctx(picks=None, items=None, default=""):
    from strategy.signal_api import SignalCache
    from ui.strategy_check_section import SignalContext
    cache = SignalCache()
    if picks is not None:
        cache.put(picks)
    return SignalContext(items=items or [], cache=cache, default_variant=default)


class TestStrategyCheckSection(unittest.TestCase):
    """docs/ui.md 8.22 (2026-11-16): the right panel's "전략 점검" -- a
    strategy check-box menu and this stock's check under it, drawn from the generic
    signal_api.StockCheck (the C rows' score breakdown included)."""

    def test_menu_lists_the_markets_variants_and_defaults(self):
        from unittest.mock import patch
        from strategy import signal_api
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        sec = d.strategy_section
        self.assertEqual(list(sec.variant_checks), signal_api.variants("KOSPI"))
        self.assertIs(sec.variant_btn.menu(), sec.variant_menu)
        self.assertEqual([v for v, cb in sec.variant_checks.items() if cb.isChecked()], ["C1"])   # exactly one
        self.assertTrue(sec.variant_btn.text().startswith("C1"))
        self.assertEqual(sec.variant(), "C1")
        self.assertFalse(sec.calc_btn.isEnabled())                   # no Universe rows passed
        d.close()
        with patch("ui.strategy_check_section.StrategyCheckSection._run_stock"):
            d = StockMaDialog("005930", "Samsung", "KOSPI", _df(), signal_ctx=_ctx(default="A3"))
        self.assertEqual(d.strategy_section.variant(), "A3")         # the pinned Universe variant
        d.close()

    def test_no_section_where_no_strategy_applies(self):
        d = StockMaDialog("AAPL", "Apple", "NASDAQ 100", _df(), signal_ctx=_ctx(_c_picks()))
        self.assertIsNone(d.strategy_section)
        d.close()

    def test_scored_name_shows_score_metrics_and_gates(self):
        picks = _c_picks()
        top = picks.detail.top[0]
        d = StockMaDialog(top.ticker, "X", "KOSPI", _df(), signal_ctx=_ctx(picks))
        sec = d.strategy_section
        self.assertEqual(sec.cards["MA60Div"].value.text(), f"{top.pct_ma50:.2f}")      # v07 trend line
        self.assertEqual(sec.cards["Range52"].value.text(), f"{top.pct_range52:.2f}")
        self.assertEqual(sec.score_lbl.text(), f"{top.week_avg:+.2f}")
        self.assertIn(f"{top.latest_total:+.2f}", sec.score_hint_lbl.text())
        self.assertIn("상위 1", sec.score_hint_lbl.text())
        self.assertEqual({b.text() for b in sec.badges.values()}, {"통과"})
        self.assertIsNone(sec.calc_btn)                               # a run exists: no "계산"
        self.assertIn("기준", sec.asof_lbl.text())
        d.close()

    def test_overheated_name_fails_that_gate(self):
        d = StockMaDialog("SPIKE", "X", "KOSPI", _df(), signal_ctx=_ctx(_c_picks()))
        sec = d.strategy_section
        self.assertEqual(sec.badges["과열 아님"].objectName(), "badgeFail")
        self.assertIn("하위", sec.score_hint_lbl.text())
        d.close()

    def test_no_run_keeps_every_row_with_dashes(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        sec = d.strategy_section
        self.assertEqual(sec.score_lbl.text(), "-")
        self.assertEqual({c.value.text() for c in sec.cards.values()}, {"-"})
        self.assertEqual(set(sec.cards), {"MA60Div", "Range52", "R3"})
        self.assertEqual({(b.objectName(), b.text()) for b in sec.badges.values()}, {("badgeNa", "-")})
        self.assertEqual(len(sec.badges), 4)
        d.close()
        d = StockMaDialog("THIN", "X", "KOSPI", _df(), signal_ctx=_ctx(_c_picks()))
        self.assertEqual(d.strategy_section.verdict_lbl.text(), "채점 제외")
        d.close()

    def test_stock_variant_runs_a_check_and_renders_it(self):
        from unittest.mock import patch
        from strategy.signal_api import CheckRow, StockCheck
        check = StockCheck("A0", "005930", None, "entry", "진입 신호",
                           [CheckRow("추세", True)], [CheckRow("추세 이탈", False)])
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        sec = d.strategy_section
        with patch("ui.strategy_check_section.StrategyCheckSection._run_stock") as run:
            sec.variant_checks["A0"].click()
        self.assertEqual([v for v, cb in sec.variant_checks.items() if cb.isChecked()], ["A0"])
        self.assertTrue(sec.variant_btn.text().startswith("A0"))
        run.assert_called_once_with("A0")
        self.assertEqual(sec.status_lbl.text(), "계산 중...")
        sec._on_check_done(check, "", "A0")
        self.assertEqual(sec.verdict_lbl.text(), "진입 신호")
        self.assertEqual(sec.badges["추세"].text(), "통과")
        self.assertEqual(sec.badges["추세 이탈"].text(), "없음")
        self.assertIsNone(sec.score_lbl)                              # no score line for A rows
        d.close()

    def test_calc_runs_the_universe_and_shares_the_result(self):
        from unittest.mock import patch
        picks = _c_picks("C2")
        ctx = _ctx(items=[{"ticker": "X", "name": "X", "market": "KOSPI"},
                          {"ticker": "AAPL", "name": "A", "market": "NASDAQ 100"}], default="C2")
        d = StockMaDialog(picks.detail.top[0].ticker, "X", "KOSPI", _df(), signal_ctx=ctx)
        sec = d.strategy_section
        self.assertEqual(sec.variant(), "C2")
        self.assertTrue(sec.calc_btn.isEnabled())
        with patch("ui.strategy_check_section.SignalPicksThread") as th:
            sec.calc_btn.click()
        self.assertEqual(th.call_args.args[1], "C2")
        self.assertFalse(sec.calc_btn.isEnabled())                    # running
        th.return_value.variant_id = "C2"
        with patch.object(sec, "sender", return_value=th.return_value):
            sec._on_universe_done(picks, "")
        self.assertIs(ctx.cache.get("C2"), picks)
        self.assertEqual(sec.verdict_lbl.text(), "매수 후보 (상위 1)")
        d.close()

    def test_universe_passes_its_context_to_the_dialog(self):
        from unittest.mock import patch
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=lambda _p, default=None: default):
            tab = UniverseTab()
        picks = _c_picks("C3")
        tab._on_signals_finished(picks, "")      # a pinned run is shared with the dialog
        with patch("ui.ma_chart.StockMaDialog") as dlg:
            tab._on_stock_ma_loaded("UP", "Up", _df(), "", [], "KOSPI", "pct")
        ctx = dlg.call_args.kwargs["signal_ctx"]
        self.assertIs(ctx.cache, tab._signal_cache)
        self.assertIs(ctx.cache.get("C3"), picks)
        self.assertEqual(ctx.default_variant, "C3")


class TestStrategyCheckKeepsItsSize(unittest.TestCase):
    """2026-11-16: picking another strategy never resizes the block (height
    from the tallest layout template, width from the widest)."""

    def test_every_variant_has_the_same_size_and_title_row(self):
        """With the investor table above it (the usual KR layout), the
        "전략 점검" title row stays put while the strategy changes, while a
        stock check is computing and after it arrives."""
        from unittest.mock import patch
        from PyQt6.QtWidgets import QApplication
        from strategy.signal_api import SignalCache
        from strategy.trend_following.signal_provider import check_stock
        from tests.strategy.trend_following.helpers import synthetic_dataset
        from ui.strategy_check_section import SignalContext
        ds = synthetic_dataset(with_flows=True)
        cache = SignalCache()
        cache.put(_c_picks("C2"))
        ctx = SignalContext(items=[{"ticker": "X", "market": "KOSPI"}], cache=cache)
        with patch("ui.strategy_check_section.StrategyCheckSection._run_stock"):
            rows = [{"Date": f"2026-11-{i:02d}", "Close": 59000, "Foreigner": 1234567 * (i % 3 - 1),
                     "Institution": -500 * i, "Retail": -3 * i} for i in range(1, 21)]
            d = StockMaDialog("UP", "X", "KOSPI", _df(), investor_data=rows, signal_ctx=ctx)
            d.resize(1400, 800)
            d.show()
            QApplication.processEvents()
            sec = d.strategy_section
            sizes = set()
            title_y = lambda: sec.title_lbl.mapToGlobal(QPoint(0, 0)).y()   # noqa: E731
            for vid in list(sec.variant_checks):
                sec.set_variant(vid)
                QApplication.processEvents()
                sizes.add((sec.width(), sec.height(), d._right_panel.width(), title_y()))   # computing / C rows
                if vid[0] in "AB":
                    sec._on_check_done(check_stock(vid, "UP", ds.stocks, ds.index_close, ds.flow_fi), "", vid)
                QApplication.processEvents()
                box = sec._body_box
                self.assertLessEqual(box.totalHeightForWidth(sec.body.width()), sec.body.height(), vid)   # nothing squeezed
                sizes.add((sec.width(), sec.height(), d._right_panel.width(), title_y()))
            self.assertEqual(len(sizes), 1, sizes)
            d.close()
