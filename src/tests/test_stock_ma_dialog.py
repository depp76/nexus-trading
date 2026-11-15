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

from data.indicators import _compute_indicators  # noqa: E402
from ui.dialogs.stock_ma import StockMaDialog  # noqa: E402
from ui.colors import PROFIT, LOSS, MA_RAMP  # noqa: E402


def _df(n=400, ew=False, volume=True):
    rng = np.random.default_rng(1)
    close = 100000 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    df = pl.DataFrame({
        "Date": dates, "Open": close * (1 + rng.normal(0, 0.005, n)), "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": (rng.random(n) * 5e6) if volume else np.zeros(n),
    }).with_columns(pl.col("Date").cast(pl.Datetime))
    df = _compute_indicators(df, windows=(5, 10, 20, 60))
    if ew:
        df = df.with_columns((pl.col("Close") * 0.5 + 1000).alias("EqualWeight"))
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
        self.assertEqual(d._panel_on, {"vol": True, "div": True, "rsi": False})
        # Opens with Close + Divergence (user direction 2026-09-29): no MA
        # line visible. A fresh dialog, since the shared one is left in
        # whatever state the other tests chose last.
        fresh = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        self.assertEqual({k for k, b in fresh._series_buttons.items() if b.isChecked()}, {"Close", "Div"})
        self.assertEqual({k: fresh._lines[k].get_visible() for k in MA_RAMP},
                         {"MA5": False, "MA10": False, "MA20": False, "MA50": False})
        self.assertTrue(fresh._lines["Close"].get_visible())
        self.assertEqual(set(fresh._end_labels), {"Close"})
        _show(d, {"Close", "MA5", "MA10", "MA20"})
        self.assertEqual({k: d._lines[k].get_visible() for k in MA_RAMP},
                         {"MA5": True, "MA10": True, "MA20": True, "MA50": False})
        self.assertIsNone(d._panel_axes["price"].get_legend())      # issue #3: no legend
        for line in d._lines.values():
            self.assertEqual(line.get_marker(), "None")             # issue #8: no markers

    def test_volume_bars_use_profit_loss(self):
        cols = [tuple(c.get_facecolor()[0][:3].round(2)) for c in self.dlg._panel_axes["vol"].collections]
        self.assertEqual(cols, [_rgb(PROFIT), _rgb(LOSS)])

    def test_ma_lines_are_the_accent_ramp(self):
        for key, color in MA_RAMP.items():
            self.assertEqual(self.dlg._lines[key].get_color(), color)

    def test_end_labels_follow_the_visible_lines(self):
        d = self.dlg
        _show(d, {"Close", "MA5", "MA10", "MA20"})
        self.assertEqual(set(d._end_labels), {"Close", "MA5", "MA10", "MA20"})
        _show(d, {"Close", "MA20", "MA50"})
        self.assertEqual(set(d._end_labels), {"Close", "MA20", "MA50"})
        _show(d, {"Close"})
        self.assertEqual(set(d._end_labels), {"Close"})
        _show(d, {"Close", "EW"})
        self.assertTrue(d._lines["EW"].get_visible())
        self.assertEqual(set(d._end_labels), {"Close", "EW"})
        _show(d, {"MA5", "MA50"})                       # Close can be hidden too
        self.assertEqual(set(d._end_labels), {"MA5", "MA50"})
        _show(d, {"Close", "Div"})

    def test_series_toggles_are_independent(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        self.assertEqual(list(d._series_buttons), ["Close", "MA5", "MA10", "MA20", "MA50", "Div"])
        d._series_buttons["MA10"].click()
        d._series_buttons["MA50"].click()
        self.assertEqual({k for k in MA_RAMP if d._lines[k].get_visible()}, {"MA10", "MA50"})
        self.assertTrue(d._lines["Close"].get_visible())
        d._series_buttons["Div"].click()                # Divergence off -> panel hidden
        self.assertFalse(d._panel_on["div"])
        self.assertFalse(d._panel_axes["div"].get_visible())
        d._set_cursor(100)
        self.assertNotIn("Div(20)", {k for k, (m, _a) in d._hover.items() if m.get_visible()})
        d._series_buttons["Div"].click()
        self.assertTrue(d._panel_axes["div"].get_visible())

    def test_no_price_line_hides_the_price_panel(self):
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        _show(d, {"Div"})
        self.assertEqual(d._visible_panels(), [("vol", 1), ("div", 1)])
        self.assertFalse(d._panel_axes["price"].get_visible())
        self.assertEqual(d._end_labels, {})
        d._set_panel("vol", False)
        self.assertEqual(d._visible_panels(), [("div", 1)])          # fixed slot, not enlarged
        _show(d, set())                                 # nothing at all: empty price panel
        self.assertEqual(d._visible_panels(), [("price", 3)])
        d.render(QPixmap(800, 500))

    def test_period_buttons_set_the_window(self):
        d, ax = self.dlg, self.dlg._panel_axes["price"]
        d._apply_period("1M")
        self.assertAlmostEqual(ax.get_xlim()[1] - ax.get_xlim()[0], 31, delta=1)
        self.assertTrue(d._period_buttons["1M"].isChecked())
        d._apply_period("All")
        self.assertGreater(ax.get_xlim()[1] - ax.get_xlim()[0], 399)
        d._apply_period("1Y")

    def test_period_segment_shares_the_wrapping_view_row(self):
        """2026-11-15: period buttons moved from the header into the series /
        panel row, a FlowLayout of three unsplittable groups."""
        from ui.common import FlowLayout
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True))
        row = next(iter(d._period_buttons.values())).parentWidget().parentWidget()
        self.assertIsInstance(row.layout(), FlowLayout)
        items = [row.layout().itemAt(i).widget() for i in range(row.layout().count())]
        groups = items[:3]
        self.assertEqual(groups, [next(iter(d._series_buttons.values())).parentWidget(),
                                  next(iter(d._period_buttons.values())).parentWidget(),
                                  next(iter(d._panel_buttons.values())).parentWidget()])
        self.assertEqual(items[3:], [d._as_of_lbl])                   # the as-of date ends the row
        # wide: one line; narrow: groups move down whole
        self.assertEqual(len(row.layout().lines(4000)), 1)
        narrow = row.layout().lines(groups[0].sizeHint().width() + 10)
        self.assertEqual([item.widget() for line in narrow for item in line][:3], groups)
        self.assertEqual(len(narrow[0]), 1)
        self.assertEqual(len(narrow[1]), 1)
        # one width for the segment, wide enough for every label ("3개월" was cut at 52 px)
        widths = {b.width() for b in d._period_buttons.values()}
        self.assertEqual(len(widths), 1)
        for b in d._period_buttons.values():
            self.assertGreaterEqual(b.width(), b.sizeHint().width())

    def test_axis_and_end_labels_fit_a_narrow_chart(self):
        """2026-11-15: fixed 7.5% / 10% margins cut "100,000" and the axis
        label once the dialog could be narrowed; margins now come from the
        measured labels."""
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df(ew=True))
        d.showNormal()
        d.resize(700, 600)
        d.show()
        QApplication.processEvents()
        for k in ("MA5", "MA20", "EW"):
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

        for key in ("MA5", "MA20", "EW", "MA50", "Div"):
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
            row = next(iter(d._period_buttons.values())).parentWidget().parentWidget()
            self.assertIs(d._as_of_lbl.parentWidget(), row)
            self.assertTrue(d._as_of_lbl.text())

            def gy(w, local=0):
                return w.mapToGlobal(QPoint(0, local)).y()

            def mid(w):
                return gy(w, w.height() // 2)

            first_btn = next(iter(d._series_buttons.values()))
            self.assertLessEqual(abs(mid(d._right_title) - mid(first_btn)), 1, width)
            self.assertLessEqual(abs(gy(d._right_table) - gy(d._canvas)), 1, width)
            self.assertGreater(gy(d.trend_section), gy(d._right_table))
            # the as-of date ends whichever line the panel buttons are on
            self.assertLessEqual(abs(mid(d._as_of_lbl) - mid(d._panel_buttons["rsi"])), 2)
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
        title = d.trend_section.layout().itemAt(0).widget()
        btn = next(iter(d._series_buttons.values()))
        mid = lambda w: w.mapToGlobal(QPoint(0, w.height() // 2)).y()   # noqa: E731
        self.assertLessEqual(abs(mid(title) - mid(btn)), 2)
        d.close()

    def test_simple_chart_row_has_no_series_group(self):
        d = StockMaDialog("KR3YT", "KR 3Y", "Index", _df(volume=False), change_mode="bp")
        row = next(iter(d._period_buttons.values())).parentWidget().parentWidget()
        self.assertEqual(row.layout().count(), 3)                 # periods + panels + as-of

    def test_panel_toggles_regrid(self):
        d = self.dlg
        _show(d, {"Close", "Div"})
        self.assertEqual(d._visible_panels(), [("price", 3), ("vol", 1), ("div", 1)])
        d._set_panel("rsi", True)
        self.assertEqual([k for k, _ in d._visible_panels()], ["price", "vol", "div", "rsi"])
        self.assertTrue(d._panel_axes["rsi"].get_visible())
        d._set_panel("vol", False)
        self.assertFalse(d._panel_axes["vol"].get_visible())
        d._set_panel("vol", True)
        d._set_panel("rsi", False)

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


class TestTrendScoreSection(unittest.TestCase):
    """docs/ui.md 8.10: the MA chart dialog carries the last Trend Score
    run's breakdown for a KR stock (moved out of the Universe table)."""

    @staticmethod
    def _rec():
        from strategy.trend_following.config import StrategyParams
        from strategy.trend_following.scoring import compute_scores, weekly_recommendation
        from tests.strategy.trend_following.test_scoring import score_book
        book, _ = score_book()
        p = StrategyParams()
        return weekly_recommendation(compute_scores(book, p), book, p)

    def test_scored_name_shows_percentiles_rank_and_gate(self):
        rec = self._rec()
        top = rec.top[0]
        d = StockMaDialog(top.ticker, "X", "KOSPI", _df(), trend_rec=rec)
        sec = d.trend_section
        self.assertEqual(sec.cards["ma50"].value.text(), f"{top.pct_ma50:.2f}")
        self.assertEqual(sec.cards["range52"].value.text(), f"{top.pct_range52:.2f}")
        self.assertEqual(sec.total_lbl.text(), f"{top.week_avg:+.2f}")
        self.assertIn(f"{top.latest_total:+.2f}", sec.hint_lbl.text())
        self.assertIn("Top 1", sec.hint_lbl.text())
        self.assertEqual({b.text() for b in sec.gate_badges.values()}, {"통과"})
        d.close()

    def test_overheated_name_fails_that_gate(self):
        rec = self._rec()
        d = StockMaDialog("SPIKE", "X", "KOSPI", _df(), trend_rec=rec)
        self.assertEqual(d.trend_section.gate_badges["heat"].objectName(), "badgeFail")
        self.assertIn("Bottom", d.trend_section.hint_lbl.text())
        d.close()

    def test_no_score_keeps_every_row_with_dashes(self):
        """2026-11-15: before a run, or for an unscored name, the block keeps
        its rows ("-" values, neutral "-" badges); the "run Trend Score in
        Trading Universe" line is gone, an unscored name says why in a tooltip."""
        from ui.trend_score_section import NOT_SCORED_SHORT, NOT_SCORED_TEXT
        d = StockMaDialog("005930", "Samsung", "KOSPI", _df())
        sec = d.trend_section
        self.assertEqual(sec.total_lbl.text(), "-")
        self.assertEqual(sec.hint_lbl.text(), "")
        self.assertEqual({c.value.text() for c in sec.cards.values()}, {"-"})
        self.assertEqual(set(sec.cards), {"ma50", "range52", "r3"})
        self.assertEqual({(b.objectName(), b.text()) for b in sec.gate_badges.values()}, {("badgeNa", "-")})
        d.close()
        d = StockMaDialog("THIN", "X", "KOSPI", _df(), trend_rec=self._rec())
        sec = d.trend_section
        self.assertEqual(sec.hint_lbl.text(), NOT_SCORED_SHORT)
        self.assertEqual(sec.hint_lbl.toolTip(), NOT_SCORED_TEXT)
        self.assertEqual(len(sec.gate_badges), 4)
        d.close()

    def test_block_height_and_type_sizes_do_not_depend_on_the_score(self):
        from ui.common import FONT_BODY, FONT_HEADING, FONT_KPI
        rec = self._rec()
        heights = []
        for ticker, r in ((rec.top[0].ticker, rec), ("005930", None), ("THIN", rec)):
            d = StockMaDialog(ticker, "X", "KOSPI", _df(), trend_rec=r)
            sec = d.trend_section
            sec.ensurePolished()
            heights.append(sec.sizeHint().height())
            self.assertEqual(sec.title_lbl.font().pointSize(), FONT_BODY)
            self.assertTrue(sec.title_lbl.font().bold())
            self.assertEqual(sec.total_lbl.font().pointSize(), FONT_KPI)
            card = sec.cards["ma50"].value
            card.ensurePolished()
            self.assertEqual(card.font().pointSize(), FONT_HEADING)
            d.close()
        self.assertEqual(len(set(heights)), 1, heights)

    def test_no_section_outside_kr_stocks(self):
        d = StockMaDialog("AAPL", "Apple", "NASDAQ 100", _df(), trend_rec=self._rec())
        self.assertIsNone(d.trend_section)
        d.close()

    def test_universe_passes_its_last_run_to_the_dialog(self):
        from unittest.mock import patch
        from ui.universe_tab import UniverseTab
        with patch("ui.universe_tab.safe_load_json", side_effect=lambda _p, default=None: default):
            tab = UniverseTab()
        rec = self._rec()
        tab._on_trend_score_finished(rec, "")      # a run is remembered for the dialog
        with patch("ui.ma_chart.StockMaDialog") as dlg:
            tab._on_stock_ma_loaded("UP", "Up", _df(), "", [], "KOSPI", "pct")
        self.assertIs(dlg.call_args.kwargs["trend_rec"], rec)
