"""tests/test_assets_graph.py — TotalAssetsGraphDialog after the 2026-11-16
label review (items 1-7): no per-point labels, right-end labels pushed apart,
one peak mark, the hover line with a one-line readout, month ticks, amounts in
억, and the Total Assets vs KOSPI gap shaded."""
import datetime as dt
import unittest
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from ui.dialogs.assets_graph import (  # noqa: E402
    TotalAssetsGraphDialog, fmt_krw, month_ticks, spread_labels,
)


def _series(n=46):
    start = dt.date(2026, 1, 2)
    dates = [(start + dt.timedelta(weeks=i)).isoformat() for i in range(n)]
    asset = [i * 1.5 for i in range(n)]
    asset[20] = 97.1                                   # the peak
    usd = [a + 3 for a in asset]
    kospi = [a - 1 for a in asset]                     # close to the others at the end
    totals = [390_800_000 * (1 + a / 100) for a in asset]
    usd_totals = [270_674 * (1 + u / 100) for u in usd]
    return dates, kospi, asset, usd, totals, usd_totals


class TestHelpers(unittest.TestCase):
    def test_amount_in_eok(self):
        self.assertEqual(fmt_krw(633_711_856), "6.34억")

    def test_labels_are_pushed_apart_but_stay_centred(self):
        out = spread_labels([62.2, 62.0, 61.0], 5.0)
        ordered = sorted(out)
        self.assertGreaterEqual(ordered[1] - ordered[0], 5.0 - 1e-9)
        self.assertGreaterEqual(ordered[2] - ordered[1], 5.0 - 1e-9)
        self.assertAlmostEqual(sum(out) / 3, (62.2 + 62.0 + 61.0) / 3)
        # the order of the values is kept
        self.assertEqual(sorted(range(3), key=lambda i: out[i]), [2, 1, 0])
        # far apart: untouched
        self.assertEqual(spread_labels([0.0, 50.0, 100.0], 5.0), [0.0, 50.0, 100.0])

    def test_month_ticks_inside_the_range_only(self):
        import matplotlib.dates as mdates
        x0, x1 = mdates.date2num(dt.date(2026, 1, 2)), mdates.date2num(dt.date(2026, 11, 13))
        ticks = [mdates.num2date(t).strftime("%Y-%m-%d") for t in month_ticks(x0, x1)]
        self.assertEqual(ticks[0], "2026-02-01")
        self.assertEqual(ticks[-1], "2026-11-01")
        self.assertEqual(len(ticks), 10)


class TestDialog(unittest.TestCase):
    def setUp(self):
        self.data = _series()
        self.dlg = TotalAssetsGraphDialog(*self.data)
        self.addCleanup(self.dlg.close)

    def test_no_per_point_labels_one_peak_and_three_end_labels(self):
        ax = self.dlg.ax
        annotations = [t for t in ax.texts if type(t).__name__ == "Annotation"]
        self.assertEqual(len(annotations), 1)                          # the peak only
        self.assertIn("최고 +97.1%", annotations[0].get_text())
        self.assertIn("2026-05-22", annotations[0].get_text())
        ends = [t.get_text() for t in self.dlg._end_label_artists]
        self.assertEqual(len(ends), 3)
        self.assertTrue(ends[0].startswith("총자산 +"))
        self.assertIn("억", ends[0])
        self.assertIn(r"\$", ends[1])                                    # not read as math
        self.assertTrue(ends[2].startswith("KOSPI "))
        ys = sorted(t.get_position()[1] for t in self.dlg._end_label_artists)
        lo, hi = ax.get_ylim()
        self.assertGreater(min(b - a for a, b in zip(ys, ys[1:])), (hi - lo) * 0.03)

    def test_month_ticks_and_shading(self):
        import matplotlib.dates as mdates
        ticks = self.dlg.ax.get_xticks()
        self.assertEqual(len(ticks), 10)
        self.assertTrue(all(mdates.num2date(t).day == 1 for t in ticks))
        from matplotlib.collections import PolyCollection
        fills = [c for c in self.dlg.ax.collections if isinstance(c, PolyCollection)]
        self.assertEqual(len(fills), 2)                                # ahead / behind

    def test_hover_moves_the_line_and_the_readout(self):
        dlg = self.dlg
        self.assertIn(self.data[0][-1], dlg.readout_lbl.text())        # the latest week by default
        dlg._on_motion(SimpleNamespace(inaxes=dlg.ax, xdata=dlg._x[10] + 0.4))
        self.assertTrue(dlg._crosshair.get_visible())
        self.assertEqual(dlg._crosshair.get_xdata()[0], dlg._x[10])
        text = dlg.readout_lbl.text()
        self.assertIn(self.data[0][10], text)
        for part in ("총자산 +", "총자산 ($)", "KOSPI ", "억"):
            self.assertIn(part, text)
        dlg._on_leave(None)
        self.assertFalse(dlg._crosshair.get_visible())
        self.assertIn(self.data[0][-1], dlg.readout_lbl.text())

    def test_one_week_and_no_week(self):
        one = TotalAssetsGraphDialog(*[s[:1] for s in self.data])
        self.addCleanup(one.close)
        self.assertEqual(len(one._end_label_artists), 3)
        none = TotalAssetsGraphDialog([], [], [], [], [], [])
        self.addCleanup(none.close)
        self.assertEqual(none.readout_lbl.text(), "기록 없음")


if __name__ == "__main__":
    unittest.main()
