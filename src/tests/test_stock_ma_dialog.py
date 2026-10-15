"""tests/test_stock_ma_dialog.py — StockMaDialog after the docs/ui.md "MA Chart
Redesign" (2026-09-19): PROFIT/LOSS volume, accent MA ramp, right-edge value
labels instead of a legend, independent series toggles (2026-10-15), period
buttons, panel toggles and the shared crosshair with on-chart value and date
badges. Synthetic data only."""
import unittest
from datetime import date, timedelta

import numpy as np
import polars as pl
from PyQt6.QtWidgets import QApplication
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
        self.assertEqual(d._visible_panels(), [("div", 3)])
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
