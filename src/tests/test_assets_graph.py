"""tests/test_assets_graph.py — TotalAssetsGraphDialog after the 2026-11-16
label review (items 1-7): no per-point labels, right-end labels pushed apart,
one peak mark, month ticks, amounts in 억, the Total Assets vs KOSPI gap
shaded, and a marker on every week: resting on any of a week's three markers
shows all three values of that week."""
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
        annotations = [t for t in ax.texts if type(t).__name__ == "Annotation" and t.get_visible()]
        self.assertEqual(len(annotations), 1)                          # the peak only (the hover box is hidden)
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

    def test_every_week_has_a_marker(self):
        lines = {ln.get_label(): ln for ln in self.dlg.ax.get_lines()}
        for label in ("KOSPI", "총자산", r"총자산 (\$)"):
            self.assertEqual(lines[label].get_marker(), "o", label)
            self.assertEqual(len(lines[label].get_xdata()), 46)
        self.assertNotEqual(lines[r"총자산 (\$)"].get_markerfacecolor(), lines["총자산"].get_markerfacecolor())

    def test_month_ticks_and_shading(self):
        import matplotlib.dates as mdates
        ticks = self.dlg.ax.get_xticks()
        self.assertEqual(len(ticks), 10)
        self.assertTrue(all(mdates.num2date(t).day == 1 for t in ticks))
        from matplotlib.collections import PolyCollection
        fills = [c for c in self.dlg.ax.collections if isinstance(c, PolyCollection)]
        self.assertEqual(len(fills), 2)                                # ahead / behind

    def _px(self, values, i):
        self.dlg.canvas.draw()                    # final layout, so data -> pixel is exact
        return self.dlg.ax.transData.transform((self.dlg._x[i], values[i]))

    def _move(self, px, py):
        self.dlg._on_motion(SimpleNamespace(inaxes=self.dlg.ax, x=px, y=py))

    def _box_lines(self):
        box = self.dlg._tooltip.offsetbox
        return [(c._text.get_text(), c._text.get_color()) for c in box.get_children()]

    def test_any_marker_shows_the_whole_week(self):
        dlg = self.dlg
        dates, kospi, asset, usd, totals, usd_totals = self.data
        self.assertFalse(hasattr(dlg, "_crosshair"))
        self.assertFalse(hasattr(dlg, "readout_lbl"))
        for s_idx, values in ((0, asset), (1, usd), (2, kospi)):          # the week's box from any of its three
            px, py = self._px(values, 10)
            self._move(px + 2, py - 2)
            self.assertEqual(dlg.hovered, (s_idx, 10))
            self.assertIsNotNone(dlg._tooltip)
            lines = self._box_lines()
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[0][0], dates[10])
            self.assertIn(f"{asset[10]:+.1f}%", lines[1][0])
            self.assertIn("억", lines[1][0])
            self.assertIn(f"{usd[10]:+.1f}%", lines[2][0])
            self.assertIn(f"{usd_totals[10]:,.0f}", lines[2][0])
            self.assertIn(r"\$", lines[2][0])                         # not read as math
            self.assertIn(f"{kospi[10]:+.1f}%", lines[3][0])
            self.assertEqual(len({c for _, c in lines[1:]}), 3)          # one color per series
            for m, v in zip(dlg._hover_markers, (asset, usd, kospi)):
                self.assertTrue(m.get_visible())
                self.assertEqual((m.get_xdata()[0], m.get_ydata()[0]), (dlg._x[10], v[10]))
            self.assertEqual(sum(1 for a in dlg.ax.artists if a is dlg._tooltip), 1)

        mid_x = (self._px(asset, 10)[0] + self._px(asset, 11)[0]) / 2   # between two weeks, off the lines
        self._move(mid_x, self._px(asset, 10)[1] + 200)
        self.assertIsNone(dlg.hovered)
        self.assertIsNone(dlg._tooltip)
        self.assertFalse(any(m.get_visible() for m in dlg._hover_markers))
        self.assertEqual(len(dlg.ax.artists), 0)                         # the old box was removed

    def test_box_stays_inside_toward_the_middle(self):
        dlg = self.dlg
        asset = self.data[2]
        self._move(*self._px(asset, 45))                                # the last week, right edge
        self.assertEqual(dlg.tooltip_align[0], 1.0)
        self._move(*self._px(asset, 0))                                 # the first week, left edge, bottom
        self.assertEqual(dlg.tooltip_align, (0.0, 0.0))
        dlg._on_leave(None)
        self.assertIsNone(dlg.hovered)
        self.assertIsNone(dlg._tooltip)

    def test_one_week_and_no_week(self):
        one = TotalAssetsGraphDialog(*[s[:1] for s in self.data])
        self.addCleanup(one.close)
        self.assertEqual(len(one._end_label_artists), 3)
        none = TotalAssetsGraphDialog([], [], [], [], [], [])
        self.addCleanup(none.close)
        self.assertIsNone(none._tooltip)
        none._on_leave(None)                                           # nothing to hide, no error


if __name__ == "__main__":
    unittest.main()
